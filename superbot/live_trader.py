"""
SuperLiveTrader — boucle d'exécution SuperBot (SPEC §6), DRY-RUN par défaut.

Patron repris de simplebot/live_trader.py avec les généralisations SuperBot :
  - multi-sleeves : B/C viennent de best_params.json (params/TF par symbole),
    la sleeve A (momentum) balaie l'univers en 4h à params figés ;
  - UNE position par symbole (sur l'exchange les positions par coin fusionnent —
    deux sleeves ne peuvent pas se marcher dessus) ;
  - toutes les entrées passent la DOUBLE GATE (orchestrateur) + filtres live
    sleeve + caps corrélation + budget d'allocation par sleeve ;
  - une décision par bougie clôturée et par (symbole, sleeve) — persisté ;
  - cooldown post-flip (FLIP_COOLDOWN_BARS du TF de la sleeve) ;
  - dry-run : positions papier, sorties SL/TP/time rejouées sur bougies
    clôturées (SL prioritaire), equity papier compoundée ;
  - live : wallet HL3 OBLIGATOIREMENT distinct de HL_* et HL2_*, entrées
    maker-first, TP/SL natifs posés dès l'entrée (pas de TP pour momentum),
    réconciliation au démarrage.

Sécurités héritées de tout l'historique du repo : valeur de compte
totale-ou-erreur, kill-switch à hystérésis (risk.py), verrou single-instance
(run.py), timeout SDK global (hyperliquid_client).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Dict, Optional

from superbot import config
from superbot.data import closed_candles, fetch_funding_rates, fetch_ohlcv
from superbot.orchestrator import Orchestrator, sleeve_alloc
from superbot.regime import RegimeFacade
from superbot.risk import KillSwitch, direction_caps_ok, dynamic_margin_pct
from superbot.sleeves import get_sleeve
from superbot.symbol_filter import quality_score

logger = logging.getLogger("sdm.superbot.live")

MARKET_REFRESH_SEC = 300      # régime marché BTC 4h : rafraîchi toutes les 5 min


# ── Wallet HL3 (troisième wallet, jamais HL_* ni HL2_*) ──────────────────────

def make_third_wallet_client():
    from hyperliquid_client import HyperliquidClient

    key = os.environ.get(config.ENV_PRIVATE_KEY)
    if not key:
        raise RuntimeError(f"{config.ENV_PRIVATE_KEY} manquant — SuperBot exige "
                           "un wallet dédié (3ᵉ wallet).")
    addr = os.environ.get(config.ENV_ACCOUNT_ADDRESS) or None
    _assert_third_wallet(key, addr)

    client = HyperliquidClient(wallet_key=key)
    if client.exchange is None:
        raise RuntimeError("Échec d'initialisation du wallet SuperBot (clé invalide ?)")
    if addr and addr.lower() != (client.wallet_address or "").lower():
        client._init_exchange(key, account_address=addr)
    _assert_third_wallet(key, client.wallet_address or "")
    logger.info("Wallet SuperBot: %s...%s", client.wallet_address[:6],
                client.wallet_address[-4:])
    return client


def _assert_third_wallet(key: str, address: Optional[str]) -> None:
    """Refus de démarrer si le wallet HL3 recoupe la V6 (HL_*) ou SimpleBot (HL2_*)."""
    for env_key, env_addr in config.FORBIDDEN_WALLET_ENVS:
        other_key = os.environ.get(env_key, "")
        if other_key and key and other_key.lower() == key.lower():
            raise RuntimeError(f"HL3_PRIVATE_KEY identique à {env_key} — refus. "
                               "SuperBot exige un 3ᵉ wallet distinct.")
        other_addr = os.environ.get(env_addr, "")
        if other_addr and address and other_addr.lower() == address.lower():
            raise RuntimeError(f"HL3_ACCOUNT_ADDRESS identique à {env_addr} — refus.")


# ── Params publiés par l'optimiseur ──────────────────────────────────────────

class ParamStore:
    def __init__(self, path=None):
        self.path = path or config.BEST_PARAMS_FILE
        self._mtime = 0.0
        self._state: dict = {}
        self.maybe_reload()

    def maybe_reload(self) -> bool:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        if mtime == self._mtime:
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._state = json.load(f)
            self._mtime = mtime
            logger.info("Paramètres rechargés (updated_at=%s)",
                        self._state.get("updated_at"))
            return True
        except Exception as e:
            logger.warning("Lecture %s échouée: %r", self.path, e)
            return False

    def actives(self) -> Dict[str, dict]:
        """{symbol: entry} des symboles actifs (sleeves B/C)."""
        return {s: e for s, e in self._state.get("symbols", {}).items()
                if e.get("active")}

    def quality_scores(self) -> Dict[str, float]:
        return {s: quality_score(e) for s, e in self.actives().items()}


# ── Trader ───────────────────────────────────────────────────────────────────

class SuperLiveTrader:

    def __init__(self, client=None, store: ParamStore = None, dry_run: bool = None,
                 fetch=None, regime: RegimeFacade = None,
                 orchestrator: Orchestrator = None, funding_fetch=None):
        self.dry_run = config.DRY_RUN if dry_run is None else dry_run
        self.client = client
        self.store = store or ParamStore()
        self._fetch = fetch or fetch_ohlcv
        self._funding_fetch = funding_fetch or fetch_funding_rates
        self.regime = regime or RegimeFacade()
        self.orchestrator = orchestrator or Orchestrator()
        self.kill = KillSwitch()
        self.state = self._load_state()
        self._market = {}                 # dernier régime marché publié
        self._market_ts = 0.0
        self._funding: Dict[str, float] = {}
        self._funding_ts = 0.0

    # ── état persistant ──────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        try:
            with open(config.LIVE_STATE_FILE, "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            st = {}
        st.setdefault("equity", 1000.0 if self.dry_run else 0.0)   # papier : base 1000
        st.setdefault("equity_history", [])
        st.setdefault("positions", {})        # {symbol: {...}} — UNE par symbole
        st.setdefault("trades", [])
        st.setdefault("last_ts", {})          # {"SYM:sleeve": ts}
        st.setdefault("last_flip_ts", {})
        st.setdefault("paused_until", 0)
        st.setdefault("exec_stats", {"maker": 0, "taker": 0, "mixed": 0})
        st.setdefault("gate_stats", {})
        return st

    def _save_state(self) -> None:
        try:
            config.LIVE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = config.LIVE_STATE_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
            os.replace(tmp, config.LIVE_STATE_FILE)
        except Exception as e:
            logger.warning("Sauvegarde live_state échouée: %r", e)

    # ── boucle ───────────────────────────────────────────────────────────────

    def run_forever(self) -> None:
        mode = "DRY-RUN (papier)" if self.dry_run else "LIVE ⚠️ ordres réels"
        logger.info("SuperLiveTrader démarré — %s — sleeves A/B/C, double gate HMM", mode)
        self.state["dry_run"] = self.dry_run
        self._save_state()
        if not self.dry_run:
            self.reconcile_positions()
        while True:
            try:
                self.tick()
            except Exception as e:
                logger.error("Tick en erreur: %r", e, exc_info=True)
            time.sleep(config.LOOP_SEC)

    def tick(self) -> None:
        self.store.maybe_reload()
        now = time.time()

        # kill-switch (equity papier en dry-run ; compte réel en live)
        equity = self._account_equity()
        if equity is not None and equity > 0:
            hist = self.state["equity_history"]
            if not hist or now - hist[-1][0] >= 300:
                hist.append([now, round(equity, 4)])
                hist[:] = hist[-5000:]
            decision = self.kill.check(equity, now)
            if decision is not None:
                logger.critical("KILL-SWITCH: %s — flatten + pause %dh",
                                decision["reason"], decision["pause_sec"] // 3600)
                self._flatten_all()
                self.state["paused_until"] = now + decision["pause_sec"]
                self._save_state()
        if now < float(self.state.get("paused_until", 0)):
            if now - getattr(self, "_last_pause_log", 0) > 600:
                self._last_pause_log = now
                logger.warning("Kill-switch actif — reprise dans %.0f min",
                               (self.state["paused_until"] - now) / 60)
            return

        # régime marché (BTC 4h) — rafraîchi toutes les 5 min
        if now - self._market_ts >= MARKET_REFRESH_SEC:
            try:
                btc = closed_candles(self._fetch("BTC", "4h", 60),
                                     config.INTERVAL_MS["4h"])
                self._market = self.regime.market_regime(btc)
                self._market_ts = now
            except Exception as e:
                logger.warning("Régime marché indisponible (%r)", e)
        if now - self._funding_ts >= 600:
            try:
                self._funding = self._funding_fetch()
                self._funding_ts = now
            except Exception as e:
                logger.debug("Funding indisponible (%r)", e)

        # candidats : sleeves B/C (best_params) puis A (univers 4h)
        candidates = []
        scores = self.store.quality_scores()
        for symbol, entry in self.store.actives().items():
            c = self._process_symbol(symbol, entry["sleeve"], entry["timeframe"],
                                     entry["params"], scores.get(symbol, 0.0))
            if c:
                candidates.append(c)
        for symbol in config.SYMBOLS:
            c = self._process_symbol(symbol, "momentum", "4h", None, 0.5)
            if c:
                candidates.append(c)

        if candidates:
            open_by_sleeve: Dict[str, int] = {}
            for p in self.state["positions"].values():
                open_by_sleeve[p["sleeve"]] = open_by_sleeve.get(p["sleeve"], 0) + 1
            accepted = self.orchestrator.filter_entries(
                candidates, market=self._market, open_by_sleeve=open_by_sleeve)
            for _sym, _slv, gate in self.orchestrator.last_decisions:
                self.state["gate_stats"][gate] = \
                    self.state["gate_stats"].get(gate, 0) + 1
            for c in accepted:
                self._open_position(c)
        self._save_state()

    # ── traitement d'un (symbole, sleeve) ────────────────────────────────────

    def _process_symbol(self, symbol: str, sleeve_name: str, timeframe: str,
                        params_dict: Optional[dict], score: float) -> Optional[dict]:
        key = f"{symbol}:{sleeve_name}"
        interval_ms = config.INTERVAL_MS[timeframe]
        last_seen = self.state["last_ts"].get(key, 0)
        now_ms = int(time.time() * 1000)
        if now_ms < last_seen + interval_ms:      # aucune nouvelle bougie possible
            return None

        sleeve = get_sleeve(sleeve_name)
        params = (sleeve.params_from_dict(params_dict) if params_dict
                  else sleeve.grid()[0])
        days = max(2.0, sleeve.warmup_bars(params) * 3 * interval_ms / 86_400_000)
        try:
            candles = closed_candles(self._fetch(symbol, timeframe, days), interval_ms)
        except Exception as e:
            logger.warning("%s %s: fetch échoué (%r)", symbol, timeframe, e)
            return None
        if config.FETCH_THROTTLE_SEC > 0:
            time.sleep(config.FETCH_THROTTLE_SEC)
        if len(candles) < sleeve.warmup_bars(params) + 1:
            return None
        last_ts = candles[-1]["ts"]
        if last_ts == last_seen:
            return None

        # sorties papier rejouées AVANT toute décision
        if self.dry_run:
            self._paper_check_exits(symbol, candles, interval_ms)

        self.state["last_ts"][key] = last_ts       # une décision par bougie
        sig = sleeve.signals(candles, params)[-1]
        if sig == 0:
            return None
        current = self.state["positions"].get(symbol)
        if current is not None:
            if current["dir"] == sig:
                return None
            if current["sleeve"] != sleeve_name:
                return None                        # une position par symbole
            last_flip = self.state["last_flip_ts"].get(symbol, 0)
            if last_ts - last_flip < config.FLIP_COOLDOWN_BARS * interval_ms:
                logger.info("%s: flip ignoré — cooldown", symbol)
                return None
            self._close_position(symbol, candles[-1]["close"], "FLIP", last_ts)
            self.state["last_flip_ts"][symbol] = last_ts

        ok, why = direction_caps_ok(symbol, sig, self.state["positions"])
        if not ok:
            logger.info("%s: %+d refusé — corrélation %s", symbol, sig, why)
            return None

        from simplebot.strategy import atr
        pol = sleeve.exit_policy(params)
        return {
            "symbol": symbol, "sleeve": sleeve_name, "signal": sig,
            "timeframe": timeframe, "quality_score": score,
            "close": candles[-1]["close"], "atr": atr(candles, pol.atr_len)[-1],
            "policy": pol, "bar_ts": last_ts,
            "funding_hourly": self._funding.get(symbol),
            "symbol_regime": self.regime.symbol_regime(symbol, candles, timeframe),
        }

    # ── ouverture / fermeture ────────────────────────────────────────────────

    def _sleeve_budget_left(self, sleeve_name: str, equity: float) -> float:
        used = sum(p.get("margin", 0.0) for p in self.state["positions"].values()
                   if p["sleeve"] == sleeve_name)
        return max(0.0, equity * sleeve_alloc(sleeve_name) - used)

    def _open_position(self, c: dict) -> None:
        symbol, sleeve_name = c["symbol"], c["sleeve"]
        if symbol in self.state["positions"]:
            return
        equity = self._account_equity() or 0.0
        if equity <= 0:
            return
        scores = self.store.quality_scores()
        margin_pct = dynamic_margin_pct(symbol, scores) * c.get("margin_mult", 1.0)
        margin = min(equity * margin_pct, self._sleeve_budget_left(sleeve_name, equity))
        notional = margin * config.LEVERAGE
        if notional < config.MIN_NOTIONAL_USD:
            logger.info("%s: notional %.2f$ trop petit (budget %s épuisé ?)",
                        symbol, notional, sleeve_name)
            return
        pol = c["policy"]
        entry = c["close"]
        direction = c["signal"]
        sl = entry - direction * pol.sl_atr * c["atr"]
        tp = (entry + direction * pol.tp_atr * c["atr"]
              if pol.tp_atr is not None else None)

        if self.dry_run:
            self.state["positions"][symbol] = {
                "sleeve": sleeve_name, "dir": direction, "entry": entry,
                "sl": sl, "tp": tp, "entry_ts": c["bar_ts"],
                "checked_ts": c["bar_ts"], "margin": margin,
                "notional": notional, "timeframe": c["timeframe"],
                "time_exit_bars": pol.time_exit_bars,
            }
            logger.info("[PAPER] %s: OPEN %s %s @~%.6g | SL=%.6g TP=%s "
                        "(marge %.2f$, %s)", symbol,
                        "LONG" if direction == 1 else "SHORT", sleeve_name,
                        entry, sl, f"{tp:.6g}" if tp else "aucun", margin,
                        c["timeframe"])
            return

        # LIVE : maker-first + TP/SL natifs immédiats
        from superbot.execution import enter_position
        qty = notional / entry
        self.client.update_leverage(symbol, config.LEVERAGE, is_cross=False)
        result = enter_position(self.client, symbol, direction == 1, qty, entry)
        stats = self.state["exec_stats"]
        stats[result["mode"]] = stats.get(result["mode"], 0) + 1
        fill_px = result["avg_px"]
        fill_sz = result["total_sz"]
        sl = fill_px - direction * pol.sl_atr * c["atr"]
        tp = (fill_px + direction * pol.tp_atr * c["atr"]
              if pol.tp_atr is not None else None)
        try:
            self.client.place_position_tpsl(coin=symbol, is_long=direction == 1,
                                            sz=fill_sz, tp_price=tp, sl_price=sl)
        except Exception as e:
            logger.error("%s: pose TP/SL échouée (%r) → fermeture de sécurité",
                         symbol, e)
            try:
                self.client.market_close(symbol)
            except Exception as e2:
                logger.critical("%s: FERMETURE DE SÉCURITÉ ÉCHOUÉE: %r — SANS SL !",
                                symbol, e2)
            return
        self.state["positions"][symbol] = {
            "sleeve": sleeve_name, "dir": direction, "entry": fill_px,
            "sl": sl, "tp": tp, "entry_ts": c["bar_ts"],
            "checked_ts": c["bar_ts"], "margin": margin, "notional": fill_sz * fill_px,
            "timeframe": c["timeframe"], "time_exit_bars": pol.time_exit_bars,
        }
        logger.info("%s: OPEN %s %s sz=%.6f @ %.6g (exec=%s)", symbol,
                    "LONG" if direction == 1 else "SHORT", sleeve_name,
                    fill_sz, fill_px, result["mode"])

    def _close_position(self, symbol: str, px: float, reason: str, ts) -> None:
        pos = self.state["positions"].pop(symbol, None)
        if pos is None:
            return
        if not self.dry_run:
            try:
                self.client.cancel_all_orders(symbol)
                self.client.market_close(symbol)
            except Exception as e:
                logger.error("%s: fermeture live échouée: %r", symbol, e)
        cost = 2.0 * (config.FEE_PCT + config.SLIPPAGE_PCT)
        pnl_pct = pos["dir"] * (px - pos["entry"]) / pos["entry"] - cost
        pnl_usd = pos["notional"] * pnl_pct
        if self.dry_run:
            self.state["equity"] += pnl_usd
        self.state["trades"].append({
            "symbol": symbol, "sleeve": pos["sleeve"], "dir": pos["dir"],
            "entry": pos["entry"], "exit": px, "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd, "reason": reason,
            "entry_ts": pos["entry_ts"], "exit_ts": ts,
        })
        trades = self.state["trades"]
        wins = len([t for t in trades if t["pnl_pct"] > 0])
        logger.info("[%s] %s: EXIT %s @ %.6g (%s) pnl=%+.2f%% | %d trades WR %.0f%%",
                    "PAPER" if self.dry_run else "LIVE", symbol,
                    "LONG" if pos["dir"] == 1 else "SHORT", px, reason,
                    pnl_pct * 100, len(trades), 100 * wins / len(trades))

    def _paper_check_exits(self, symbol: str, candles: list, interval_ms: int) -> None:
        pos = self.state["positions"].get(symbol)
        if pos is None:
            return
        # ne rejouer que sur les bougies du TF de la position (une position par
        # symbole, mais A et B/C peuvent scanner le même coin sur 2 TFs)
        if pos.get("timeframe") and config.INTERVAL_MS.get(pos["timeframe"]) != interval_ms:
            return
        for c in candles:
            if c["ts"] <= pos["checked_ts"]:
                continue
            d = pos["dir"]
            if (d == 1 and c["low"] <= pos["sl"]) or (d == -1 and c["high"] >= pos["sl"]):
                self._close_position(symbol, pos["sl"], "SL", c["ts"])
                return
            if pos["tp"] is not None and (
                    (d == 1 and c["high"] >= pos["tp"])
                    or (d == -1 and c["low"] <= pos["tp"])):
                self._close_position(symbol, pos["tp"], "TP", c["ts"])
                return
            if pos.get("time_exit_bars"):
                bars = (c["ts"] - pos["entry_ts"]) / interval_ms
                if bars >= pos["time_exit_bars"]:
                    self._close_position(symbol, c["close"], "TIME", c["ts"])
                    return
            pos["checked_ts"] = c["ts"]

    # ── divers ───────────────────────────────────────────────────────────────

    def _account_equity(self) -> Optional[float]:
        if self.dry_run:
            return float(self.state.get("equity", 0.0))
        try:
            return self.client.get_portfolio_value()   # canonique — leçon simplebot
        except Exception as e:
            logger.warning("Equity illisible (%r)", e)
            return None

    def _flatten_all(self) -> None:
        for symbol in list(self.state["positions"]):
            pos = self.state["positions"][symbol]
            px = pos["entry"]      # papier : approximation dernier connu
            self._close_position(symbol, px, "KILL", time.time() * 1000)
        if not self.dry_run:
            try:
                self.client.cancel_all_orders()
            except Exception as e:
                logger.warning("cancel_all_orders: %r", e)

    def reconcile_positions(self) -> None:
        """Live : toute position sans SL natif est re-protégée ou fermée
        (patron simplebot — crash entre entrée et pose TP/SL)."""
        try:
            positions = self.client.get_positions()
            orders = self.client.get_open_orders()
        except Exception as e:
            logger.error("Réconciliation impossible: %r", e)
            return
        protected = {o.get("coin") for o in orders
                     if o.get("reduce_only") or o.get("isTrigger")}
        for p in positions:
            coin = p.get("coin")
            szi = float(p.get("szi", 0))
            if abs(szi) <= 0:
                continue
            if coin in protected:
                logger.info("Réconciliation %s: SL natif présent — OK", coin)
                continue
            logger.warning("Réconciliation %s: position SANS SL → fermeture de "
                           "sécurité", coin)
            try:
                self.client.market_close(coin)
            except Exception as e:
                logger.critical("Réconciliation %s: fermeture échouée: %r", coin, e)
