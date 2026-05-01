"""
Tests unitaires — SalleDesMarches V6
Couvre les fonctions critiques : consensus, sizing, recalc TPSL, normalize scalper.
"""

import sys
import os
import unittest

# Ajouter la racine du projet au path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─────────────────────────────────────────────────────────────
# Test de la fonction _consensus (extraite de main_v6)
# ─────────────────────────────────────────────────────────────

def _consensus(bull, bear, technical):
    """Copie de la fonction _consensus de main_v6.py pour test isolé."""
    bconf = float(bull.get("confidence", 0) or 0)
    risk_lvl = str(bear.get("risk_level", "medium") or "medium").lower()
    tech_conf = float(technical.get("confidence", 0) or 0)
    tech_sig = str(technical.get("signal", "wait") or "wait").lower()

    risk_penalty = {"low": 0.0, "medium": 0.1, "high": 0.25}.get(risk_lvl, 0.1)

    if tech_sig == "buy":
        side = "buy"
        conf = max(bconf, tech_conf) - risk_penalty
    elif tech_sig == "sell":
        side = "sell"
        conf = max(bconf, tech_conf) - risk_penalty
    else:
        side = "wait"
        conf = 0.0

    return {
        "side": side,
        "confidence": max(0.0, min(1.0, conf)),
        "reason": f"bull={bconf:.2f} tech={tech_conf:.2f} bear_risk={risk_lvl}",
    }


class TestConsensus(unittest.TestCase):

    def test_buy_high_confidence(self):
        result = _consensus(
            bull={"confidence": 0.85},
            bear={"risk_level": "low"},
            technical={"signal": "buy", "confidence": 0.80},
        )
        self.assertEqual(result["side"], "buy")
        self.assertAlmostEqual(result["confidence"], 0.85, places=2)

    def test_sell_with_high_risk_penalty(self):
        result = _consensus(
            bull={"confidence": 0.70},
            bear={"risk_level": "high"},
            technical={"signal": "sell", "confidence": 0.60},
        )
        self.assertEqual(result["side"], "sell")
        # max(0.70, 0.60) - 0.25 = 0.45
        self.assertAlmostEqual(result["confidence"], 0.45, places=2)

    def test_wait_signal(self):
        result = _consensus(
            bull={"confidence": 0.90},
            bear={"risk_level": "low"},
            technical={"signal": "wait", "confidence": 0.90},
        )
        self.assertEqual(result["side"], "wait")
        self.assertAlmostEqual(result["confidence"], 0.0, places=2)

    def test_null_values_handled(self):
        result = _consensus(
            bull={"confidence": None},
            bear={"risk_level": None},
            technical={"signal": None, "confidence": None},
        )
        self.assertEqual(result["side"], "wait")
        self.assertAlmostEqual(result["confidence"], 0.0)

    def test_confidence_clamped_to_0(self):
        result = _consensus(
            bull={"confidence": 0.10},
            bear={"risk_level": "high"},
            technical={"signal": "buy", "confidence": 0.05},
        )
        self.assertEqual(result["side"], "buy")
        # max(0.10, 0.05) - 0.25 = -0.15 → clampé à 0.0
        self.assertAlmostEqual(result["confidence"], 0.0, places=2)


# ─────────────────────────────────────────────────────────────
# Test du sizing
# ─────────────────────────────────────────────────────────────

class TestPositionSizing(unittest.TestCase):
    """Teste la logique de _compute_position_size sans instancier la classe complète."""

    RISK_PER_TRADE_PCT = 0.008
    MAX_NOTIONAL_PCT = 0.08
    MAX_LEVERAGE = 5.0

    def _compute(self, equity, entry, sl):
        if equity <= 0 or entry <= 0 or sl <= 0 or entry == sl:
            return 0.0

        risk_usdt = max(1.0, equity * self.RISK_PER_TRADE_PCT)
        diff = abs(entry - sl)
        if diff <= 0:
            return 0.0

        qty_raw = risk_usdt / diff
        max_notional = equity * self.MAX_NOTIONAL_PCT
        if qty_raw * entry > max_notional:
            qty_raw = max_notional / entry

        return max(0.0, round(qty_raw, 6))

    def test_normal_case(self):
        # equity=10000, entry=100, sl=99 → risk=80 USDT, diff=1 → qty=80
        # mais notional=80*100=8000 > max_notional(10000*0.08=800) → capé: qty=800/100=8.0
        qty = self._compute(10000, 100.0, 99.0)
        self.assertAlmostEqual(qty, 8.0, places=2)

    def test_notional_cap(self):
        # equity=1000, entry=100, sl=99.99 → risk=8, diff=0.01 → qty=800
        # notional=80000 > max(1000*0.08=80) → capé: qty=80/100=0.8
        qty = self._compute(1000, 100.0, 99.99)
        self.assertAlmostEqual(qty, 0.8, places=2)

    def test_zero_equity(self):
        self.assertEqual(self._compute(0, 100.0, 99.0), 0.0)

    def test_entry_equals_sl(self):
        self.assertEqual(self._compute(10000, 100.0, 100.0), 0.0)

    def test_zero_entry(self):
        self.assertEqual(self._compute(10000, 0, 99.0), 0.0)


# ─────────────────────────────────────────────────────────────
# Test du recalcul TP/SL post-fill
# ─────────────────────────────────────────────────────────────

class TestRecalcTPSL(unittest.TestCase):

    def _recalc(self, side, planned_entry, planned_tp, planned_sl, fill_price):
        side = side.lower()
        if side == "buy":
            tp_dist = max(0.0, planned_tp - planned_entry)
            sl_dist = max(0.0, planned_entry - planned_sl)
            new_tp = fill_price + tp_dist
            new_sl = fill_price - sl_dist
        elif side == "sell":
            tp_dist = max(0.0, planned_entry - planned_tp)
            sl_dist = max(0.0, planned_sl - planned_entry)
            new_tp = fill_price - tp_dist
            new_sl = fill_price + sl_dist
        else:
            return planned_tp, planned_sl
        return round(new_tp, 4), round(new_sl, 4)

    def test_buy_fill_higher(self):
        # Planned: entry=100 tp=102 sl=98, fill at 100.5
        tp, sl = self._recalc("buy", 100.0, 102.0, 98.0, 100.5)
        self.assertAlmostEqual(tp, 102.5, places=4)
        self.assertAlmostEqual(sl, 98.5, places=4)

    def test_sell_fill_lower(self):
        # Planned: entry=100 tp=98 sl=102, fill at 99.5
        tp, sl = self._recalc("sell", 100.0, 98.0, 102.0, 99.5)
        self.assertAlmostEqual(tp, 97.5, places=4)
        self.assertAlmostEqual(sl, 101.5, places=4)

    def test_buy_fill_exact(self):
        tp, sl = self._recalc("buy", 100.0, 103.0, 97.0, 100.0)
        self.assertAlmostEqual(tp, 103.0, places=4)
        self.assertAlmostEqual(sl, 97.0, places=4)


# ─────────────────────────────────────────────────────────────
# Test de _normalize du scalper
# ─────────────────────────────────────────────────────────────

class TestScalperNormalize(unittest.TestCase):
    """Teste la géométrie entry/sl/tp."""

    def test_buy_valid(self):
        # sl < entry < tp → valide
        result = self._normalize("buy", 100.0, 99.0, 101.0, atr=0.5, leverage=3)
        self.assertIsNotNone(result)
        self.assertLess(result["sl"], result["entry"])
        self.assertGreater(result["tp"], result["entry"])

    def test_sell_valid(self):
        # tp < entry < sl → valide
        result = self._normalize("sell", 100.0, 101.0, 99.0, atr=0.5, leverage=3)
        self.assertIsNotNone(result)
        self.assertGreater(result["sl"], result["entry"])
        self.assertLess(result["tp"], result["entry"])

    def test_buy_inverted_geom_gets_corrected(self):
        # sl > entry (inversé) → devrait fallback
        result = self._normalize("buy", 100.0, 101.0, 99.0, atr=0.5, leverage=3)
        if result is not None:
            self.assertLess(result["sl"], result["entry"])
            self.assertGreater(result["tp"], result["entry"])

    def test_zero_entry_returns_none(self):
        result = self._normalize("buy", 0.0, 0.0, 0.0, atr=0.5, leverage=3)
        self.assertIsNone(result)

    def test_invalid_side_returns_none(self):
        result = self._normalize("hold", 100.0, 99.0, 101.0, atr=0.5, leverage=3)
        self.assertIsNone(result)

    # ── Reproduction de la logique _normalize du scalper ──
    def _normalize(self, side, entry, sl, tp, atr, leverage):
        if entry <= 0:
            return None
        side = (side or "").lower()
        if side not in ("buy", "sell"):
            return None

        min_sl_pct = 0.0010
        max_sl_pct = 0.010
        min_tp_pct = 0.0015
        max_tp_pct = 0.012
        ratio_min = 1.10
        ratio_max = 3.50
        tp_pnl_pct = 0.03
        sl_pnl_pct = 0.015

        def target_tp_pct():
            return tp_pnl_pct / max(1.0, leverage)

        def target_sl_pct():
            return sl_pnl_pct / max(1.0, leverage)

        valid_geom = False
        raw_sl_dist = 0.0
        raw_tp_dist = 0.0

        if sl > 0 and tp > 0:
            if side == "buy" and sl < entry < tp:
                valid_geom = True
                raw_sl_dist = entry - sl
                raw_tp_dist = tp - entry
            elif side == "sell" and tp < entry < sl:
                valid_geom = True
                raw_sl_dist = sl - entry
                raw_tp_dist = entry - tp

        if not valid_geom:
            raw_sl_dist = entry * target_sl_pct()
            raw_tp_dist = entry * target_tp_pct()

        if raw_sl_dist <= 0:
            return None

        # Clamp SL
        atr_floor = atr * 0.60 if atr > 0 else entry * min_sl_pct
        sl_dist = max(raw_sl_dist, atr_floor, entry * min_sl_pct)
        sl_dist = min(sl_dist, entry * max_sl_pct)

        # Clamp TP
        target_tp_d = entry * target_tp_pct()
        tp_floor = max(entry * min_tp_pct, sl_dist * ratio_min, target_tp_d)
        tp_cap = min(entry * max_tp_pct, sl_dist * ratio_max)
        tp_dist = tp_floor if tp_cap < tp_floor else min(max(raw_tp_dist, tp_floor), tp_cap)

        if side == "buy":
            return {"entry": round(entry, 8), "sl": round(entry - sl_dist, 8), "tp": round(entry + tp_dist, 8)}
        else:
            return {"entry": round(entry, 8), "sl": round(entry + sl_dist, 8), "tp": round(entry - tp_dist, 8)}


# ─────────────────────────────────────────────────────────────
# Test _normalize_symbol
# ─────────────────────────────────────────────────────────────

class TestNormalizeSymbol(unittest.TestCase):

    def _normalize_symbol(self, symbol):
        s = str(symbol or "").upper().strip()
        for suffix in ("-PERP", "-USD", "/USD", "/USDT", "-USDT"):
            if s.endswith(suffix):
                s = s[: -len(suffix)]
        return s

    def test_plain(self):
        self.assertEqual(self._normalize_symbol("BTC"), "BTC")

    def test_perp_suffix(self):
        self.assertEqual(self._normalize_symbol("ETH-PERP"), "ETH")

    def test_usdt_suffix(self):
        self.assertEqual(self._normalize_symbol("SOL/USDT"), "SOL")
        self.assertEqual(self._normalize_symbol("SOL-USDT"), "SOL")

    def test_lowercase(self):
        self.assertEqual(self._normalize_symbol("btc"), "BTC")

    def test_empty(self):
        self.assertEqual(self._normalize_symbol(""), "")
        self.assertEqual(self._normalize_symbol(None), "")


if __name__ == "__main__":
    unittest.main()
