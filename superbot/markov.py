"""
Pseudo-HMM Markov (SPEC §4 niveau 2) — adapté de agents/regime_engine.py (V6).

Pas un vrai HMM : compte les transitions discrètes sur l'historique des états
OBSERVÉS et lisse l'état courant avec inertie. Sert de filet de sécurité et
enrichit les métriques (transition_risk affiché au dashboard) quel que soit
le mode (HMM gaussien ou fallback ADX).
"""

from __future__ import annotations

from typing import Dict, List, Optional


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def markov_transition_stats(states: List[str], current_state: str) -> Dict:
    """Probabilités de transition empiriques (lissage de Laplace) depuis
    l'historique des états observés — patron regime_engine V6."""
    if not states or len(states) < 3:
        return {"stay_probability": 0.5, "switch_probability": 0.5, "next_probs": {}}

    all_states = sorted(set(states) | {current_state})
    counts: Dict[str, Dict[str, int]] = {
        s: {t: 1 for t in all_states} for s in all_states     # Laplace
    }
    for i in range(1, len(states)):
        counts.setdefault(states[i - 1], {})
        counts[states[i - 1]][states[i]] = counts[states[i - 1]].get(states[i], 1) + 1

    row = counts.get(current_state, {})
    total = sum(row.values())
    if total <= 0:
        return {"stay_probability": 0.5, "switch_probability": 0.5, "next_probs": {}}

    next_probs = {k: v / total for k, v in row.items()}
    stay = next_probs.get(current_state, 0.0)
    return {
        "stay_probability": stay,
        "switch_probability": 1.0 - stay,
        "next_probs": next_probs,
    }


def state_stability(states: List[str], current_state: str, lookback: int = 20) -> float:
    """Part du lookback passée dans l'état courant (0..1)."""
    if not states:
        return 0.0
    recent = states[-lookback:]
    return recent.count(current_state) / len(recent)


def compute_latent_state(
    observed_state: str,
    observed_confidence: float,
    states_history: List[str],
    previous_latent: Optional[str] = None,
    min_confidence: float = 0.45,
) -> Dict:
    """Lissage à inertie (pseudo-HMM) : on ne quitte l'état latent précédent
    que si la confiance composite (observation + stabilité + Markov-stay) est
    suffisante. Retourne aussi un transition_risk composite."""
    markov = markov_transition_stats(states_history, observed_state)
    stability = state_stability(states_history, observed_state)

    composite_conf = _clip(
        observed_confidence * 0.45 + stability * 0.30
        + markov["stay_probability"] * 0.25,
        0.0, 1.0,
    )

    latent = observed_state
    if composite_conf < min_confidence and previous_latent:
        latent = previous_latent

    transition_risk = _clip(
        (1.0 - markov["stay_probability"]) * 0.55
        + (1.0 - stability) * 0.30
        + (1.0 - observed_confidence) * 0.15,
        0.0, 1.0,
    )
    return {
        "latent_state": latent,
        "latent_confidence": composite_conf,
        "transition_risk": transition_risk,
        "markov": markov,
        "stability": stability,
    }
