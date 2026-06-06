"""
BanditPolicy — politique d'exécution apprise par le bandit shadow (2026-06-06).

Lit l'état entraîné par scripts/exec_bandit_shadow.py (ridge par bras) et
choisit, pour un ordre donné, le mode d'exécution :
  taker_now           → market (comportement historique du bot)
  limit_mid           → limit GTC au mid, timeout TIMEOUT_S → fallback market
  limit_passif_1bps   → limit à mid ∓ 1 bps (côté passif)
  limit_passif_3bps   → limit à mid ∓ 3 bps

SÉCURITÉ — fail-open vers taker (= comportement actuel) dans TOUS les cas
douteux : état absent, < MIN_OBS observations, données HF périmées (> 10 s),
ordre reduce_only (sorties/urgences : l'immédiateté prime), toute exception.
Le bandit ne peut donc jamais dégrader la réactivité des sorties.

Constantes (ARMS, offsets, timeout) : MÊMES valeurs que exec_bandit_shadow.py
— l'état JSON est partagé, toute divergence fausserait les prédictions.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger("v7.bandit_policy")

REPO = Path(__file__).resolve().parent.parent
STATE_PATH = REPO / "memory" / "exec_bandit_state.json"
HF_DB = REPO / "data" / "orderflow_hf.db"

ARMS = ["taker_now", "limit_mid", "limit_passif_1bps", "limit_passif_3bps"]
ARM_OFFSETS_BPS = {1: 0.0, 2: 1.0, 3: 3.0}
TIMEOUT_S = 30
MIN_OBS = 500            # gate : politique inactive avant 500 fills appris
HF_STALE_MS = 10_000     # données HF plus vieilles que 10 s → taker
STATE_TTL_S = 60         # relecture de l'état au plus toutes les 60 s


class BanditPolicy:
    """Choix du bras d'exécution. Thread-safe en lecture (état immuable rechargé)."""

    def __init__(self) -> None:
        self._theta: Optional[list[np.ndarray]] = None   # coefficients par bras
        self._n_obs = 0
        self._loaded_at = 0.0
        self._state_mtime = 0.0

    # ── État appris ──────────────────────────────────────────────────────────
    def _refresh_state(self) -> bool:
        now = time.time()
        if self._theta is not None and now - self._loaded_at < STATE_TTL_S:
            return True
        try:
            mtime = STATE_PATH.stat().st_mtime
        except FileNotFoundError:
            return False
        if self._theta is not None and mtime == self._state_mtime:
            self._loaded_at = now
            return True
        try:
            d = json.loads(STATE_PATH.read_text())["bandit"]
            A = [np.array(a) for a in d["A"]]
            b = [np.array(v) for v in d["b"]]
            self._theta = [np.linalg.solve(A[k], b[k]) for k in range(len(ARMS))]
            self._n_obs = int(d["n"])
            self._state_mtime = mtime
            self._loaded_at = now
            return True
        except Exception as e:
            logger.warning("état bandit illisible (%r) → taker", e)
            return False

    # ── Contexte temps réel (HF db, lecture seule) ───────────────────────────
    def _context(self, coin: str, side: str, notional: float) -> Optional[tuple[np.ndarray, float]]:
        """→ (x[6], mid) ou None si données absentes/périmées."""
        now_ms = int(time.time() * 1000)
        conn = sqlite3.connect(f"file:{HF_DB}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT ts_ms, mid_px, spread_bps, imb5 FROM l2_1s "
                "WHERE coin=? ORDER BY ts_ms DESC LIMIT 1", (coin,)).fetchone()
            if not row or not row[1] or now_ms - row[0] > HF_STALE_MS:
                return None
            mids = [r[0] for r in conn.execute(
                "SELECT mid_px FROM l2_1s WHERE coin=? AND ts_ms >= ? ORDER BY ts_ms",
                (coin, now_ms - 60_000))]
            if len(mids) < 10:
                return None
            rets = np.diff(np.log(np.array(mids, dtype=float))) * 1e4
            vol_1m = float(np.std(rets)) if len(rets) > 2 else 0.0
            n_tr = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE coin=? AND ts_ms >= ?",
                (coin, now_ms - 60_000)).fetchone()[0]
            sgn = 1.0 if side == "buy" else -1.0
            x = np.array([1.0, row[2], vol_1m, sgn * (row[3] or 0.0),
                          n_tr / 60.0, np.log10(max(notional, 1.0))], dtype=float)
            return x, float(row[1])
        finally:
            conn.close()

    # ── API ──────────────────────────────────────────────────────────────────
    def choose(self, coin: str, side: str, notional: float,
               reduce_only: bool) -> tuple[str, Optional[float]]:
        """→ (nom du bras, prix limit ou None pour market). Fail-open taker."""
        try:
            if reduce_only:
                return "taker_now", None
            if not self._refresh_state() or self._n_obs < MIN_OBS:
                return "taker_now", None
            ctx = self._context(coin, side, notional)
            if ctx is None:
                return "taker_now", None
            x, mid = ctx
            costs = np.array([float(x @ self._theta[k]) for k in range(len(ARMS))])
            best = int(np.argmin(costs))
            if best == 0:
                return "taker_now", None
            sgn = 1.0 if side == "buy" else -1.0
            off = ARM_OFFSETS_BPS[best]
            limit_px = mid * (1 - sgn * off / 1e4)
            logger.info("bandit %s %s: bras=%s px=%.6g (coûts prédits %s)",
                        coin, side, ARMS[best], limit_px,
                        np.round(costs, 2).tolist())
            return ARMS[best], limit_px
        except Exception as e:
            logger.warning("bandit choose(%s) error: %r → taker", coin, e)
            return "taker_now", None
