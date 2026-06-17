"""Tests adoption BootReconciler → MR (orphelines au boot, 2026-06-17).

Vérifie que chaque position HL non-manuelle de la watchlist MR est ré-attribuée
à la stratégie MR (sinon orpheline, coupée seulement au stop -5 %)."""
from __future__ import annotations

from core.config import MeanReversionStrategyConfig
from execution.boot_reconciler import BootReconciler
from strategies.mean_reversion import MeanReversionStrategy


class _FakeRead:
    def __init__(self, detailed):
        self._detailed = detailed

    def get_positions_detailed(self):
        return dict(self._detailed)


def _mr(symbols):
    cfg = MeanReversionStrategyConfig(
        enabled=True, interval="1h", window=50, entry_z=2.0, exit_z=0.4,
        hl_min=5.0, hl_max=48.0, cooldown_sec=1800, notional_usdc=30.0,
        sl_z=3.5, min_sl_buffer_std=1.5,
    )
    return MeanReversionStrategy(cfg, symbols=symbols)


def test_adopt_into_mr_long_and_short():
    mr = _mr(["BTC", "ETH"])
    read = _FakeRead({
        "BTC": {"szi": 0.01, "entry_px": 60000.0},     # long
        "ETH": {"szi": -0.127, "entry_px": 1781.0},    # short
    })
    br = BootReconciler(read, write_adapter=None, portfolio=None,
                        manual_symbols=[], mr_strategy=mr)
    n = br._adopt_into_mr()
    assert n == 2
    assert mr._positions["BTC"]["side"] == "buy"
    assert mr._positions["ETH"]["side"] == "sell"
    assert mr._intent["ETH"]["target_notional"] > 0


def test_adopt_skips_manual_and_off_watchlist():
    mr = _mr(["BTC"])
    read = _FakeRead({
        "BTC": {"szi": 0.01, "entry_px": 60000.0},   # adoptable
        "HYPE": {"szi": 5.0, "entry_px": 30.0},      # manuelle → skip
        "SOL": {"szi": 2.0, "entry_px": 150.0},      # hors watchlist MR → skip
    })
    br = BootReconciler(read, write_adapter=None, portfolio=None,
                        manual_symbols=["HYPE"], mr_strategy=mr)
    n = br._adopt_into_mr()
    assert n == 1
    assert set(mr._positions) == {"BTC"}


def test_adopt_skips_zero_size():
    mr = _mr(["BTC"])
    read = _FakeRead({"BTC": {"szi": 0.0, "entry_px": 60000.0}})
    br = BootReconciler(read, write_adapter=None, portfolio=None,
                        manual_symbols=[], mr_strategy=mr)
    assert br._adopt_into_mr() == 0
    assert mr._positions == {}
