"""
RiskTracker — calcule le drawdown courant et le PnL du jour, AJUSTÉS DES FLUX DE CAPITAL.

Corrige le bug (2026-06-22) : `RiskStateImpl(equity=...)` laissait current_drawdown / daily_pnl_pct
à 0.0 → kill-switch et daily-loss INERTES (ne déclenchaient jamais). Mais un fix naïf serait
DANGEREUX : un retrait (ex. $120 le 22/06 = −22% d'equity) ressemblerait à une perte → faux
kill-switch → liquidation du book. D'où la neutralisation des flux via le ledger HL.

Mécanique :
  - high-water mark (peak) et equity de début de journée UTC, PERSISTÉS (survivent au restart).
  - à chaque update, on interroge (throttlé) le flux de capital net depuis la dernière fois et on
    AJUSTE peak ET day_start du même montant → un retrait baisse la référence, pas le drawdown.
  - current_drawdown = max(0, (peak − equity)/peak) ; daily_pnl_pct = (equity − day_start)/day_start.
  - FAIL-SAFE : à la 1re init, sur equity ≤ 0, ou baseline incohérente → renvoie (0, 0) (PAS de
    déclenchement). On ne kill JAMAIS sur une donnée douteuse.
  - RESET MANUEL : si le fichier sentinelle `<dir>/RESET_PEAK` existe, on ré-initialise le peak sur
    l'equity courante (pour REPRENDRE après un kill revu par francois) puis on supprime le fichier.

Le kill-switch se « latch » naturellement : une fois flat, l'equity est figée → DD reste ≥ seuil →
RiskManager.project re-vide la cible chaque tick. Reprise = RESET_PEAK (ou recovery de l'equity).
"""
from __future__ import annotations

import json
import logging
import time
import datetime as dt
from pathlib import Path
from typing import Callable, Optional, Tuple

logger = logging.getLogger("v7.risk.tracker")


class RiskTracker:
    def __init__(
        self,
        state_path: Path,
        flow_fn: Optional[Callable[[int], Tuple[float, int]]] = None,
        flow_poll_sec: float = 600.0,
    ) -> None:
        """`flow_fn(since_ms) -> (net_flow_usd, latest_ms)` : lecture du flux de capital net
        (cf. HyperliquidReadAdapter.get_ledger_net_flow). None → pas d'ajustement de flux
        (test/paper). `flow_poll_sec` : cadence de l'appel ledger (réseau)."""
        self._path = Path(state_path)
        self._reset_sentinel = self._path.parent / "RESET_PEAK"
        self._flow_fn = flow_fn
        self._flow_poll_sec = flow_poll_sec
        self._peak: Optional[float] = None
        self._day_start: Optional[float] = None
        self._day: Optional[str] = None
        self._last_ledger_ms: int = 0
        self._last_flow_poll: float = 0.0
        self._load()

    # ─── Persistance ──────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            d = json.loads(self._path.read_text())
            self._peak = d.get("peak")
            self._day_start = d.get("day_start")
            self._day = d.get("day")
            self._last_ledger_ms = int(d.get("last_ledger_ms", 0) or 0)
        except Exception:
            pass  # 1re fois ou corrompu → init paresseuse au 1er update (fail-safe)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({
                "peak": self._peak, "day_start": self._day_start,
                "day": self._day, "last_ledger_ms": self._last_ledger_ms,
            }))
        except Exception as e:
            logger.debug("RiskTracker save: %r", e)

    # ─── API ──────────────────────────────────────────────────────────────────
    def update(self, equity: float) -> Tuple[float, float]:
        """Renvoie (current_drawdown ∈[0,1], daily_pnl_pct). Fail-safe (0,0) si donnée douteuse."""
        now = time.time()
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

        if not (isinstance(equity, (int, float)) and equity > 0):
            return 0.0, 0.0

        # Reset manuel (reprise après kill revu).
        if self._reset_sentinel.exists():
            self._peak = equity
            self._day_start = equity
            self._day = today
            self._last_ledger_ms = int(now * 1000)
            try:
                self._reset_sentinel.unlink()
            except Exception:
                pass
            logger.warning("RiskTracker: RESET_PEAK → baseline ré-initialisée sur equity=%.2f", equity)
            self._save()
            return 0.0, 0.0

        # Init paresseuse (1re fois) → pas de déclenchement.
        if self._peak is None or self._day_start is None or self._day is None:
            self._peak = equity
            self._day_start = equity
            self._day = today
            self._last_ledger_ms = int(now * 1000)
            self._save()
            return 0.0, 0.0

        # Neutralisation des flux de capital (retraits/dépôts), throttlée.
        if self._flow_fn is not None and (now - self._last_flow_poll) >= self._flow_poll_sec:
            self._last_flow_poll = now
            try:
                net, latest = self._flow_fn(self._last_ledger_ms)
                if net != 0.0:
                    # Garde anti-corruption : un flux qui viderait la référence (sous 5% de
                    # l'equity) est suspect (ex. ré-application en boucle) → on re-base sur
                    # l'equity plutôt que de descendre au plancher (bug 2026-06-27).
                    floor = 0.05 * equity
                    new_pk, new_ds = self._peak + net, self._day_start + net
                    self._peak = new_pk if new_pk > floor else max(equity, self._peak)
                    self._day_start = new_ds if new_ds > floor else equity
                    logger.warning(
                        "RiskTracker: flux capital net %+.2f$ neutralisé (peak→%.2f, day_start→%.2f)",
                        net, self._peak, self._day_start,
                    )
                # Avance TOUJOURS le curseur (même si net=0) pour ne jamais re-sommer une entrée.
                self._last_ledger_ms = max(self._last_ledger_ms, int(latest))
            except Exception as e:
                logger.debug("RiskTracker flow_fn: %r", e)

        # Nouveau jour UTC → reset du day_start.
        if today != self._day:
            self._day = today
            self._day_start = equity

        # High-water mark.
        if equity > self._peak:
            self._peak = equity

        dd = max(0.0, (self._peak - equity) / self._peak) if self._peak > 0 else 0.0
        daily = (equity - self._day_start) / self._day_start if self._day_start > 0 else 0.0
        self._save()
        return dd, daily

    # Debug / dashboard
    @property
    def peak(self) -> Optional[float]:
        return self._peak
