"""Boucle live LLMBot — scan quant → LLM → exécution.

Paper (DRY_RUN) : positions simulées avec TP/SL ROE, equity $ trackée,
frais = 2 × (FEE_PCT + SLIPPAGE_PCT) sur le notional (aligné simplebot paper).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from llmbot import config
from llmbot.agent_trader import decide
from llmbot.news import NewsEngine
from llmbot.quant_scanner import rank_candidates, scan_symbol

logger = logging.getLogger("sdm.llmbot.live")


def _fetch_ob(client, symbol: str) -> dict:
    try:
        from agents.agent_orderbook import AgentOrderbook
        return AgentOrderbook(client).analyze(symbol)
    except Exception as e:
        logger.debug("%s orderbook: %r", symbol, e)
        return {"spread_pct": 0.05, "bid_ask_imbalance": 0, "is_liquid_enough": True}


class LLMLiveTrader:
    def __init__(self, client=None, dry_run: Optional[bool] = None):
        self.dry_run = config.DRY_RUN if dry_run is None else dry_run
        self.client = client
        self.news = NewsEngine()
        self.state = self._load_state()
        self._freeze_until: Dict[str, float] = {}

    def _load_state(self) -> dict:
        try:
            with open(config.LIVE_STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = {}
        state.setdefault("trades", [])
        state.setdefault("paper_positions", {})
        state.setdefault("paused_until", 0)
        state.setdefault("equity_history", [])
        if "equity" not in state:
            state["equity"] = float(getattr(config, "PAPER_START_EQUITY", 200.0) or 200.0)
        return state

    def _save_state(self) -> None:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = config.LIVE_STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, config.LIVE_STATE_FILE)

    def _log_decision(self, row: dict) -> None:
        try:
            config.STATE_DIR.mkdir(parents=True, exist_ok=True)
            with open(config.DECISIONS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _is_frozen(self, symbol: str) -> bool:
        return time.time() < self._freeze_until.get(symbol, 0)

    def _open_count(self) -> int:
        if self.dry_run:
            return len(self.state.get("paper_positions", {}))
        if not self.client:
            return 0
        try:
            return len([p for p in self.client.get_positions() if abs(float(p.get("szi", 0))) > 0])
        except Exception:
            return 0

    def _fetch_candles(self, symbol: str) -> List[dict]:
        from simplebot.data import fetch_ohlcv
        return fetch_ohlcv(
            symbol, config.INTERVAL,
            days=max(3, config.CANDLES_LOOKBACK // 96 + 1),
        )[-config.CANDLES_LOOKBACK:]

    def _paper_mark_exits(self, prices: Dict[str, float]) -> None:
        """TP/SL paper en ROE (levier inclus). SL prioritaire si les deux touchés."""
        if not self.dry_run:
            return
        pos_map = self.state.setdefault("paper_positions", {})
        if not pos_map:
            return
        lev = float(config.LEVERAGE) or 1.0
        fee_rt = 2.0 * (float(config.FEE_PCT) + float(config.SLIPPAGE_PCT))
        closed_syms: List[str] = []
        for symbol, pos in list(pos_map.items()):
            px = prices.get(symbol)
            if px is None or px <= 0:
                continue
            entry = float(pos.get("entry") or 0)
            if entry <= 0:
                continue
            side = str(pos.get("side", "long")).lower()
            sign = 1.0 if side == "long" else -1.0
            # ROE ≈ levier × move prix (sans frais) ; frais déduits en $
            move = sign * (px - entry) / entry
            roe = lev * move
            tp_roe = float(pos.get("tp_roe") or config.TP_ROE_PCT)
            sl_roe = float(pos.get("sl_roe") or config.SL_ROE_PCT)
            reason = None
            if roe <= -sl_roe:
                reason = "SL"
                # fill approx au prix SL ROE
                exit_move = -sl_roe / lev
                exit_px = entry * (1 + sign * exit_move)
            elif roe >= tp_roe:
                reason = "TP"
                exit_move = tp_roe / lev
                exit_px = entry * (1 + sign * exit_move)
            else:
                continue
            closed_syms.append(symbol)
            self._paper_close(symbol, pos, float(exit_px), reason, fee_rt)

        if closed_syms:
            self._save_state()

    def _paper_close(
        self, symbol: str, pos: dict, exit_px: float, reason: str, fee_rt: float,
    ) -> None:
        pos_map = self.state.setdefault("paper_positions", {})
        pos_map.pop(symbol, None)
        entry = float(pos["entry"])
        side = str(pos.get("side", "long")).lower()
        sign = 1.0 if side == "long" else -1.0
        pnl_pct = sign * (exit_px - entry) / entry - fee_rt
        eq = float(self.state.get("equity") or config.PAPER_START_EQUITY or 200.0)
        notional = max(10.0, eq * float(config.MARGIN_PCT) * float(config.LEVERAGE))
        pnl_usd = pnl_pct * notional
        eq_new = eq + pnl_usd
        self.state["equity"] = eq_new
        self.state.setdefault("equity_history", []).append([time.time(), eq_new])
        trade = {
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "exit": exit_px,
            "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd,
            "notional": notional,
            "reason": reason,
            "entry_ts": pos.get("ts"),
            "exit_ts": time.time(),
        }
        self.state.setdefault("trades", []).append(trade)
        # freeze après pertes consécutives (même logique live)
        if pnl_usd < 0:
            # simple: freeze symbole sur perte
            self._freeze_until[symbol] = time.time() + float(config.FREEZE_SEC)
        logger.info(
            "[PAPER] %s: EXIT %s @ %.6g (%s) pnl=%+.3f%% (%+.2f$) | equity=%.2f",
            symbol, side.upper(), exit_px, reason, pnl_pct * 100, pnl_usd, eq_new,
        )

    def tick(self) -> None:
        if time.time() < float(self.state.get("paused_until", 0) or 0):
            logger.info("Trading en pause (kill-switch)")
            return

        # 1) Scan prix / quant pour tous les symboles (aussi pour TP/SL paper)
        prices: Dict[str, float] = {}
        candidates = []
        open_syms = set(self.state.get("paper_positions", {}) if self.dry_run else [])

        for symbol in config.SYMBOLS:
            if self._is_frozen(symbol) and symbol not in open_syms:
                continue
            try:
                candles = self._fetch_candles(symbol)
                if candles:
                    prices[symbol] = float(candles[-1]["close"])
                # exits paper avant d'ouvrir autre chose
                if self.dry_run and symbol in open_syms:
                    continue  # géré en bloc ci-dessous
                ob = (
                    _fetch_ob(self.client, symbol)
                    if self.client
                    else {"spread_pct": 0.03, "bid_ask_imbalance": 0, "is_liquid_enough": True}
                )
                scan = scan_symbol(candles, ob)
                scan["symbol"] = symbol
                candidates.append(scan)
            except Exception as e:
                logger.warning("%s scan échoué: %r", symbol, e)

        # 2) Paper exits (prix last close)
        if self.dry_run:
            # pour les positions, re-fetch close si manquant
            for symbol in list(self.state.get("paper_positions", {})):
                if symbol not in prices:
                    try:
                        candles = self._fetch_candles(symbol)
                        if candles:
                            prices[symbol] = float(candles[-1]["close"])
                    except Exception as e:
                        logger.warning("%s prix paper: %r", symbol, e)
            self._paper_mark_exits(prices)

        # 3) Entrées
        macro = self.news.maybe_refresh()
        open_n = self._open_count()
        # ne pas re-entrer un symbole déjà paper-open
        if self.dry_run:
            open_syms = set(self.state.get("paper_positions", {}))
            candidates = [c for c in candidates if c.get("symbol") not in open_syms]

        top = rank_candidates(candidates)
        if not top:
            logger.info(
                "Cycle: aucun setup quant ≥ %.0f (open=%d equity=%.2f)",
                config.MIN_QUANT_SCORE, open_n,
                float(self.state.get("equity") or 0),
            )
            self._save_state()
            return

        logger.info("Cycle: %d candidats quant → %d passent au LLM", len(candidates), len(top))

        for scan in top:
            if open_n >= config.MAX_OPEN_POSITIONS:
                logger.info("MAX_OPEN atteint (%d)", open_n)
                break
            symbol = scan["symbol"]
            decision = decide(symbol, scan, macro, open_n)
            if not decision:
                continue
            self._log_decision({
                "ts": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "quant_score": scan["quant_score"],
                "direction": scan["direction"],
                **decision,
            })
            if decision["action"] == "WAIT":
                continue
            if decision["confidence"] < config.MIN_LLM_CONFIDENCE:
                logger.info(
                    "%s: conf LLM %.2f < %.2f → skip",
                    symbol, decision["confidence"], config.MIN_LLM_CONFIDENCE,
                )
                continue
            ok = self._execute(symbol, scan, decision)
            if ok:
                open_n += 1

        self._save_state()

    def _execute(self, symbol: str, scan: dict, decision: dict) -> bool:
        is_buy = decision["action"] == "ENTER_LONG"
        price = scan["technical"]["price"]
        tp_roe = decision["tp_roe_pct"]
        sl_roe = decision["sl_roe_pct"]
        lev = config.LEVERAGE

        if self.dry_run:
            if symbol in self.state.get("paper_positions", {}):
                return False
            self.state.setdefault("paper_positions", {})[symbol] = {
                "side": "long" if is_buy else "short",
                "entry": price,
                "tp_roe": tp_roe,
                "sl_roe": sl_roe,
                "ts": time.time(),
                "reason": decision["reason"],
            }
            logger.info(
                "[PAPER] %s %s @ %.6g TP=%.1f%% SL=%.1f%% ROE — %s",
                symbol, "LONG" if is_buy else "SHORT", price,
                tp_roe * 100, sl_roe * 100, decision["reason"][:50],
            )
            return True

        if not self.client:
            return False
        try:
            from simplebot.execution import smart_entry
            av = float(self.client.get_account_value() or 0)
            margin = av * config.MARGIN_PCT
            notional = margin * lev
            sz = notional / price if price > 0 else 0
            if sz <= 0:
                return False
            fill = smart_entry(self.client, symbol, is_buy, sz, price)
            entry = float(fill.get("avg_px") or price)
            tp_px = entry * (1 + (tp_roe / lev) * (1 if is_buy else -1))
            sl_px = entry * (1 - (sl_roe / lev) * (1 if is_buy else -1))
            self.client.place_tpsl_native(
                symbol, is_buy=not is_buy, sz=sz, tp_px=tp_px, sl_px=sl_px,
            )
            logger.info(
                "[LIVE] %s %s @ %.6g mode=%s TP=%.6g SL=%.6g",
                symbol, "LONG" if is_buy else "SHORT", entry, fill.get("mode"), tp_px, sl_px,
            )
            return True
        except Exception as e:
            logger.error("%s exécution échouée: %r", symbol, e)
            return False

    def run_forever(self) -> None:
        mode = "DRY-RUN" if self.dry_run else "LIVE"
        logger.info(
            "LLMBot démarré — %s — state=%s — %d symboles — quant≥%.0f — "
            "LLM conf≥%.2f — max %d appels/cycle — equity=%.2f",
            mode, config.STATE_DIR, len(config.SYMBOLS), config.MIN_QUANT_SCORE,
            config.MIN_LLM_CONFIDENCE, config.MAX_LLM_TRADES_PER_CYCLE,
            float(self.state.get("equity") or 0),
        )
        while True:
            try:
                self.tick()
            except Exception as e:
                logger.error("Tick: %r", e, exc_info=True)
            time.sleep(config.LOOP_SEC)
