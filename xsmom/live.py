"""
XSMomentumLiveTrader — exécuteur LIVE du momentum cross-sectionnel.

⚠️ STATUT (2026-08-07) : préparation d'infra UNIQUEMENT. Le juge de la
stratégie reste xsmom/paper.py (verdict mi-septembre 2026). NE PAS passer
en live réel avant un verdict paper positif. Ce module existe pour que, si
le verdict est GO, la machinerie soit prête et éprouvée en dry-run.

Décision : identique au paper (mêmes fonctions de scoring importées de
xsmom.paper — paramètres FIGÉS, aucun optimiseur). Ce module n'ajoute QUE
l'exécution et les sécurités.

Infra portée de SimpleBot (simplebot/VERDICT_2026-08-07.md : « l'infra live
est saine et réutilisable ») :
  - DRY-RUN PAR DÉFAUT : le live réel exige XSMOM_DRY_RUN=0 explicitement
    ET un wallet dédié (HL4_*), refusé s'il réutilise un wallet existant ;
  - kill-switch à hystérésis (KILL_CONFIRMATIONS lectures consécutives sous
    le seuil), fenêtre glissante, pause, fail-safe lecture (gel des entrées
    après N échecs plutôt que décision sur un chiffre faux) ;
  - equity = valeur CANONIQUE HL (get_portfolio_value) pour le kill ET le
    sizing — leçon des incidents perp fantôme / spot illisible de SimpleBot :
    plus simple et strictement plus sûr que la somme perp+spot clampée ;
  - rebase sur retrait : une chute d'equity expliquée par un flux sortant au
    ledger n'est PAS une perte (incident 11-07) ;
  - exécution maker-first : entrées via simplebot.execution.smart_entry
    (skip si non rempli — pas de taker forcé), sorties via smart_close
    (limit Alo reduce-only, fallback market : un skip de sortie laisserait
    de l'exposition) ;
  - écriture d'état atomique (tmp+rename), verrou single-instance ;
  - réconciliation au boot : positions exchange ≠ état ⇒ GEL du trading
    (jamais d'ordre sur un état incohérent).

Contrainte de taille : 7 tranches × 16 positions = 112 slots ; avec le
minimum HL de ~10 $/ordre, l'equity minimale praticable est ~112 × 11 $
≈ 1250 $. En dessous, les positions sous le minimum sont SKIPPÉES (loggé)
— le portefeuille devient partiel et le tracking-error vs paper croît.

Usage :
    python -m xsmom.live --once      # un cycle (dry-run par défaut)
    python -m xsmom.live             # boucle quotidienne
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from simplebot.data import (fetch_funding_rates, fetch_ledger_updates,
                            fetch_ohlcv, fetch_perp_universe, net_transfer_flow)
from simplebot.execution import smart_close, smart_entry
from xsmom.paper import (DAY_MS, FEE_SIDE, MIN_HISTORY_DAYS, N_LEG, N_TRANCHES,
                         RET_DAYS, TOP_N_UNIVERSE, VOL_DAYS, daily_closes,
                         score_symbol)

logger = logging.getLogger("sdm.xsmom.live")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ── Config (env XSMOM_*) ─────────────────────────────────────────────────────
# DRY-RUN par défaut — même convention que SimpleBot.
DRY_RUN = os.environ.get("XSMOM_DRY_RUN", "1") not in ("0", "false", "False")

# Wallet DÉDIÉ (jamais celui de la V6 / SimpleBot / SuperBot).
ENV_PRIVATE_KEY = "HL4_PRIVATE_KEY"
ENV_ACCOUNT_ADDRESS = "HL4_ACCOUNT_ADDRESS"
_FORBIDDEN_KEY_ENVS = ("HL_PRIVATE_KEY", "HL2_PRIVATE_KEY", "HL3_PRIVATE_KEY")

MIN_NOTIONAL_USD = 11.0          # min HL 10 $ + marge d'arrondi
FETCH_THROTTLE_SEC = _env_float("XSMOM_FETCH_THROTTLE_SEC", 0.35)

KILL_LOSS_PCT = _env_float("XSMOM_KILL_LOSS_PCT", 0.05)
KILL_WINDOW_SEC = _env_int("XSMOM_KILL_WINDOW_SEC", 24 * 3600)
KILL_PAUSE_SEC = _env_int("XSMOM_KILL_PAUSE_SEC", 24 * 3600)
KILL_CONFIRMATIONS = _env_int("XSMOM_KILL_CONFIRMATIONS", 2)
KILL_MAX_READ_FAILURES = _env_int("XSMOM_KILL_MAX_READ_FAILURES", 3)

DRY_EQUITY0 = _env_float("XSMOM_DRY_EQUITY", 2000.0)   # ≥ ~1250 $ praticable

STATE_FILE = Path(os.environ.get(
    "XSMOM_LIVE_STATE_FILE",
    str(Path(__file__).resolve().parent / "state" / "xsmom_live_state.json"),
))


def make_live_client():
    """Client HL sur wallet dédié. Refuse un wallet déjà utilisé ailleurs."""
    key = (os.environ.get(ENV_PRIVATE_KEY) or "").strip()
    if not key:
        raise RuntimeError(
            f"{ENV_PRIVATE_KEY} manquant — le live xsmom exige un wallet DÉDIÉ "
            "(et un verdict paper positif, cf. docstring)"
        )
    for other in _FORBIDDEN_KEY_ENVS:
        if key == (os.environ.get(other) or "").strip():
            raise RuntimeError(
                f"{ENV_PRIVATE_KEY} identique à {other} — wallet partagé interdit"
            )
    from hyperliquid_client import HyperliquidClient
    return HyperliquidClient(wallet_key=key)


class XSMomentumLiveTrader:
    """
    Une décision par jour UTC (même cadence que le paper) ; exécution des
    deltas maker-first ; kill-switch et sécurités à chaque cycle.

    En dry-run : AUCUN appel d'ordre — les fills sont simulés au dernier
    close avec frais maker, mais tout le chemin de code décision→cibles→
    deltas→comptabilité est le chemin live.
    """

    def __init__(
        self,
        client=None,
        dry_run: Optional[bool] = None,
        fetch: Optional[Callable[..., List[dict]]] = None,
        funding_fetch: Optional[Callable[[], Dict[str, float]]] = None,
        universe_fetch: Optional[Callable[..., List[str]]] = None,
        ledger_fetch: Optional[Callable[..., List[dict]]] = None,
        state_file: Optional[Path] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.dry_run = DRY_RUN if dry_run is None else dry_run
        self.client = client
        if not self.dry_run and self.client is None:
            self.client = make_live_client()
        self._fetch = fetch or fetch_ohlcv
        self._funding_fetch = funding_fetch or fetch_funding_rates
        self._universe_fetch = universe_fetch or fetch_perp_universe
        self._ledger_fetch = ledger_fetch or fetch_ledger_updates
        self._sleep = sleep
        self.state_file = Path(state_file) if state_file else STATE_FILE
        self.state = self._load_state()
        self._acct_read_failures = 0
        self._kill_breach_count = 0
        self.frozen_reason: Optional[str] = None   # gel réconciliation/lectures

        if not self.dry_run:
            self._reconcile_boot()

    # ── État (atomique) ─────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            st = {}
        st.setdefault("started_at", time.time())
        st.setdefault("dry_equity", DRY_EQUITY0)
        st.setdefault("equity_history", [])          # [[ts, valeur]]
        st.setdefault("tranches", [{} for _ in range(N_TRANCHES)])
        st.setdefault("last_rebalance_day", None)
        st.setdefault("paused_until", 0.0)
        st.setdefault("exec_stats", {"maker": 0, "taker": 0, "mixed": 0, "skip": 0})
        st.setdefault("skipped_min_notional", 0)
        st.setdefault("rebalances", [])
        return st

    def _save_state(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=1)
            os.replace(tmp, self.state_file)
        except Exception as e:
            logger.warning("Sauvegarde xsmom_live_state échouée: %r", e)

    # ── Réconciliation au boot (live only) ──────────────────────────────────

    def _state_net_positions(self) -> Dict[str, float]:
        """Position nette signée par symbole d'après l'état (tranches agrégées)."""
        net: Dict[str, float] = {}
        for tr in self.state["tranches"]:
            for sym, pos in tr.items():
                net[sym] = net.get(sym, 0.0) + pos["dir"] * pos["sz"]
        return {s: v for s, v in net.items() if abs(v) > 1e-12}

    def _reconcile_boot(self) -> None:
        """Positions exchange ≠ état ⇒ GEL (jamais d'ordre sur état incohérent)."""
        try:
            on_ex = {p["coin"]: float(p.get("szi", 0.0))
                     for p in self.client.get_positions()
                     if abs(float(p.get("szi", 0.0))) > 1e-12}
        except Exception as e:
            self.frozen_reason = f"positions exchange illisibles au boot ({e!r})"
            logger.critical("Réconciliation impossible — %s — GEL du trading", self.frozen_reason)
            return
        local = self._state_net_positions()
        diffs = []
        for sym in set(on_ex) | set(local):
            a, b = on_ex.get(sym, 0.0), local.get(sym, 0.0)
            if abs(a - b) > max(1e-9, 0.02 * max(abs(a), abs(b))):
                diffs.append(f"{sym}: exchange={a} état={b}")
        if diffs:
            self.frozen_reason = "état ≠ exchange: " + "; ".join(sorted(diffs)[:10])
            logger.critical(
                "Réconciliation: %d divergence(s) — GEL du trading (résolution "
                "manuelle requise) : %s", len(diffs), self.frozen_reason,
            )
        else:
            logger.info("Réconciliation boot OK (%d positions)", len(on_ex))

    # ── Equity / kill-switch (port SimpleBot, source canonique) ─────────────

    def _account_value(self) -> float:
        """Valeur CANONIQUE HL — sizing ET kill-switch. Peut lever (fail-safe)."""
        if self.dry_run:
            return self._dry_equity_mark()
        return self.client.get_portfolio_value()

    def _dry_equity_mark(self) -> float:
        unreal = 0.0
        for tr in self.state["tranches"]:
            for pos in tr.values():
                px = pos.get("mark", pos["entry"])
                unreal += pos["notional"] * pos["dir"] * (px - pos["entry"]) / pos["entry"]
        return self.state["dry_equity"] + unreal

    def _external_outflow_since(self, since_ts_sec: float) -> float:
        addr = getattr(self.client, "wallet_address", "") or ""
        if not addr:
            return 0.0
        updates = self._ledger_fetch(addr, int(since_ts_sec * 1000))
        return -min(0.0, net_transfer_flow(updates, addr))

    def _emergency_flatten(self) -> None:
        for sym in sorted(self._state_net_positions()):
            try:
                self.client.cancel_all_orders(sym)
            except Exception as e:
                logger.warning("%s: cancel_all_orders: %r", sym, e)
            try:
                self.client.market_close(sym)
                logger.info("%s: position fermée (kill-switch)", sym)
            except Exception as e:
                logger.critical("%s: FERMETURE D'URGENCE ÉCHOUÉE: %r", sym, e)
        self.state["tranches"] = [{} for _ in range(N_TRANCHES)]

    def kill_switch_engaged(self, now: Optional[float] = None) -> bool:
        """
        True ⇒ pas de trading ce cycle. Hystérésis KILL_CONFIRMATIONS,
        fail-safe lecture (gel après N échecs), rebase sur retrait ledger.
        """
        now = now or time.time()
        paused = now < float(self.state.get("paused_until", 0))

        try:
            account_value = self._account_value()
        except Exception as e:
            self._acct_read_failures += 1
            if paused:
                return True
            if self._acct_read_failures >= KILL_MAX_READ_FAILURES:
                logger.critical(
                    "Kill-switch: account value illisible %d cycles (%r) — GEL des entrées",
                    self._acct_read_failures, e,
                )
                return True
            logger.warning("Kill-switch: account value illisible (%r) — échec %d/%d",
                           e, self._acct_read_failures, KILL_MAX_READ_FAILURES)
            return False
        self._acct_read_failures = 0

        hist = self.state["equity_history"]
        if account_value > 0 and (not hist or now - hist[-1][0] >= 300):
            hist.append([now, account_value])
            cutoff = now - KILL_WINDOW_SEC
            hist[:] = [pt for pt in hist if pt[0] >= cutoff]
            self._save_state()

        if paused:
            return True
        if account_value <= 0:
            logger.warning("Account value nulle — wallet vide ou API dégradée, "
                           "aucune entrée possible")
            return False

        peak = max(v for _, v in hist) if hist else account_value
        if peak > 0 and account_value <= peak * (1 - KILL_LOSS_PCT):
            # Un retrait n'est pas une perte (incident SimpleBot 11-07).
            if not self.dry_run:
                peak_ts = max(hist, key=lambda p: p[1])[0] if hist else now
                try:
                    outflow = self._external_outflow_since(peak_ts - 60)
                except Exception as e:
                    logger.warning("Kill-switch: ledger illisible (%r)", e)
                    outflow = 0.0
                drop = peak - account_value
                if outflow > 0 and outflow >= drop * 0.8:
                    logger.warning(
                        "Kill-switch: chute %.2f expliquée par un RETRAIT %.2f — "
                        "rebase, pas de fermeture", drop, outflow,
                    )
                    self.state["equity_history"] = [[now, account_value]]
                    self._kill_breach_count = 0
                    self._save_state()
                    return False
            self._kill_breach_count += 1
            if self._kill_breach_count < KILL_CONFIRMATIONS:
                logger.warning(
                    "Kill-switch: %.2f ≤ pic %.2f ×(1-%.1f%%) — confirmation %d/%d",
                    account_value, peak, KILL_LOSS_PCT * 100,
                    self._kill_breach_count, KILL_CONFIRMATIONS,
                )
                self._save_state()
                return False
            self._kill_breach_count = 0
            logger.critical(
                "KILL-SWITCH: %.2f ≤ pic %.2f ×(1-%.1f%%) — fermeture + pause %dh",
                account_value, peak, KILL_LOSS_PCT * 100, KILL_PAUSE_SEC // 3600,
            )
            if not self.dry_run:
                self._emergency_flatten()
            self.state["paused_until"] = now + KILL_PAUSE_SEC
            self._save_state()
            return True

        self._kill_breach_count = 0
        return False

    # ── Exécution d'un delta (maker-first, dry-run simulé) ──────────────────

    def _exec_close(self, sym: str, pos: dict, ref_px: float) -> float:
        """Ferme une position ; retourne le PnL $ réalisé (frais inclus)."""
        if self.dry_run:
            fill_px, fee_pct = ref_px, FEE_SIDE
            self.state["exec_stats"]["maker"] += 1
        else:
            res = smart_close(self.client, sym, is_buy=(pos["dir"] == -1),
                              sz=pos["sz"], ref_price=ref_px)
            self.state["exec_stats"][res["mode"]] = (
                self.state["exec_stats"].get(res["mode"], 0) + 1)
            if res["mode"] == "skip" or res["total_sz"] <= 0:
                logger.warning("%s: sortie non exécutée — position conservée", sym)
                return 0.0
            fill_px = res["avg_px"]
            fee_pct = FEE_SIDE if res["mode"] == "maker" else 0.00075
        pnl_pct = pos["dir"] * (fill_px - pos["entry"]) / pos["entry"] - fee_pct
        return pos["notional"] * pnl_pct

    def _exec_open(self, sym: str, direction: int, notional: float,
                   ref_px: float) -> Optional[dict]:
        """Ouvre une position ; retourne l'entrée d'état (None si skip)."""
        if notional < MIN_NOTIONAL_USD:
            self.state["skipped_min_notional"] += 1
            logger.warning("%s: notionnel %.2f$ < min %.0f$ — position SKIPPÉE "
                           "(equity insuffisante pour 112 slots)", sym, notional,
                           MIN_NOTIONAL_USD)
            return None
        sz = notional / ref_px
        if self.dry_run:
            fill_px = ref_px
            self.state["exec_stats"]["maker"] += 1
        else:
            res = smart_entry(self.client, sym, is_buy=(direction == 1),
                              sz=sz, ref_price=ref_px)
            self.state["exec_stats"][res["mode"]] = (
                self.state["exec_stats"].get(res["mode"], 0) + 1)
            if res["mode"] == "skip" or res["total_sz"] <= 0:
                return None
            fill_px, sz = res["avg_px"], res["total_sz"]
        return {"dir": direction, "entry": fill_px, "sz": sz,
                "notional": sz * fill_px, "mark": fill_px}

    # ── Cœur : rebalance quotidien d'UNE tranche (cadence du paper) ─────────

    def rebalance_if_due(self, now: Optional[float] = None) -> bool:
        now = now or time.time()
        day = int(now // 86_400)
        if self.state["last_rebalance_day"] == day:
            return False
        if self.frozen_reason:
            logger.critical("Trading GELÉ (%s) — rebalance sauté", self.frozen_reason)
            return False
        if self.kill_switch_engaged(now):
            return False

        # Scores — même logique/paramètres FIGÉS que le paper.
        try:
            universe = list(self._universe_fetch(top_n=TOP_N_UNIVERSE))
        except Exception as e:
            logger.warning("Univers illisible (%r) — rebalance sauté", e)
            return False
        closes_map: Dict[str, List[dict]] = {}
        scores: Dict[str, float] = {}
        for i, sym in enumerate(universe):
            if i and FETCH_THROTTLE_SEC:
                self._sleep(FETCH_THROTTLE_SEC)
            try:
                cs = daily_closes(self._fetch(sym, "1d", MIN_HISTORY_DAYS + 3))
            except Exception as e:
                logger.warning("fetch 1d %s: %r", sym, e)
                continue
            if len(cs) < MIN_HISTORY_DAYS:
                continue
            closes_map[sym] = cs
            sc = score_symbol([c["close"] for c in cs])
            if sc is not None:
                scores[sym] = sc
        if len(scores) < N_LEG * 3:
            logger.warning("Rebalance sauté — univers scoreable trop petit (%d)", len(scores))
            self.state["last_rebalance_day"] = day
            self._save_state()
            return False

        px_now = {s: closes_map[s][-1]["close"] for s in closes_map}
        # Mark des positions ouvertes (equity dry-run vivante).
        for tr in self.state["tranches"]:
            for sym, pos in tr.items():
                if sym in px_now:
                    pos["mark"] = px_now[sym]

        equity = self._account_value()
        k = day % N_TRANCHES

        # 1) Fermer la tranche du jour (sorties maker-first reduce-only).
        realized = 0.0
        for sym, pos in list(self.state["tranches"][k].items()):
            realized += self._exec_close(sym, pos, px_now.get(sym, pos["entry"]))
        self.state["tranches"][k] = {}

        # 2) Rouvrir la tranche sur le classement du jour.
        ranked = sorted(scores, key=scores.get)
        longs, shorts = ranked[-N_LEG:], ranked[:N_LEG]
        per_pos = (equity / N_TRANCHES) / (2 * N_LEG)
        new_tr: Dict[str, dict] = {}
        entry_fees = 0.0
        for sym, d in [(s, 1) for s in longs] + [(s, -1) for s in shorts]:
            entry = self._exec_open(sym, d, per_pos, px_now[sym])
            if entry is not None:
                new_tr[sym] = entry
                entry_fees += entry["notional"] * FEE_SIDE

        if self.dry_run:
            self.state["dry_equity"] += realized - entry_fees
        self.state["tranches"][k] = new_tr
        self.state["last_rebalance_day"] = day
        self.state["rebalances"].append({
            "day": day, "tranche": k, "longs": longs, "shorts": shorts,
            "n_opened": len(new_tr), "realized_usd": round(realized, 4),
            "equity": round(equity, 4), "dry_run": self.dry_run,
        })
        if len(self.state["rebalances"]) > 400:
            del self.state["rebalances"][:-400]
        logger.info(
            "[XSMOM-%s] rebalance j=%d tranche=%d | %d/%d ouvertes | realized %+0.2f$ "
            "| equity %.2f$ | L=%s | S=%s",
            "DRY" if self.dry_run else "LIVE", day, k, len(new_tr),
            2 * N_LEG, realized, equity, ",".join(longs), ",".join(shorts),
        )
        self._save_state()
        return True


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    import fcntl

    parser = argparse.ArgumentParser(description="XSMom — exécuteur live (dry-run défaut)")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

    lock_file = Path(__file__).resolve().parent / "state" / "xsmom_live.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_file, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.critical("Une instance xsmom.live tourne déjà — refus.")
        return 1
    fh.truncate(0)
    fh.write(str(os.getpid()))
    fh.flush()

    trader = XSMomentumLiveTrader()
    mode = "DRY-RUN (aucun ordre)" if trader.dry_run else "⚠️ LIVE RÉEL"
    logger.info("[XSMOM-LIVE] démarré — %s | equity min praticable ~%.0f$ "
                "(112 slots × %.0f$)", mode, 112 * MIN_NOTIONAL_USD, MIN_NOTIONAL_USD)
    if args.once:
        trader.rebalance_if_due()
        return 0
    while True:
        try:
            now = time.time()
            if now % 86_400 >= 600:   # après 00:10 UTC, comme le paper
                trader.rebalance_if_due(now)
        except Exception as e:
            logger.error("[XSMOM-LIVE] tick en erreur: %r", e, exc_info=True)
        time.sleep(600)


if __name__ == "__main__":
    import sys
    sys.exit(main())
