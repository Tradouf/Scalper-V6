"""Tests RiskTracker (2026-06-27) : drawdown/daily-PnL flow-aware pour ré-activer le kill-switch
sans faux déclenchement sur retrait. Vérifie : fail-safe init, drawdown, daily PnL, neutralisation
des retraits (anti phantom-drawdown), reset journalier UTC, RESET_PEAK, equity nulle."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from risk.risk_tracker import RiskTracker


def _tracker(flow=None, poll=0.0):
    d = Path(tempfile.mkdtemp())
    return RiskTracker(state_path=d / "risk_baseline.json", flow_fn=flow, flow_poll_sec=poll), d


def test_first_update_failsafe():
    t, _ = _tracker()
    dd, daily = t.update(1000.0)
    assert dd == 0.0 and daily == 0.0          # init → jamais de déclenchement
    assert t.peak == 1000.0


def test_drawdown_computed():
    t, _ = _tracker(flow=lambda s: (0.0, s))   # pas de flux
    t.update(1000.0)                            # init, peak=1000
    dd, daily = t.update(900.0)                 # -10%
    assert dd == pytest.approx(0.10)
    assert daily == pytest.approx(-0.10)        # même jour → day_start=1000


def test_peak_tracks_up_then_dd():
    t, _ = _tracker(flow=lambda s: (0.0, s))
    t.update(1000.0)
    t.update(1200.0)                            # nouveau peak
    dd, _ = t.update(1080.0)                    # -10% du peak 1200
    assert dd == pytest.approx(0.10)
    assert t.peak == 1200.0


def test_withdrawal_neutralized_no_phantom_dd():
    """Un retrait ($120 sur $1000) ne doit PAS créer de drawdown (anti faux kill-switch)."""
    calls = {"n": 0}
    def flow(since):
        calls["n"] += 1
        return (-120.0, since + 1) if calls["n"] == 1 else (0.0, since)
    t, _ = _tracker(flow=flow)
    t.update(1000.0)                            # init peak=1000
    dd, daily = t.update(880.0)                 # equity tombée de 120 = le RETRAIT
    assert dd == pytest.approx(0.0, abs=1e-9)   # peak ajusté à 880 → DD=0
    assert daily == pytest.approx(0.0, abs=1e-9)


def test_real_loss_triggers_dd():
    """Une vraie perte (pas un flux) DOIT produire un drawdown."""
    t, _ = _tracker(flow=lambda s: (0.0, s))    # aucun flux
    t.update(1000.0)
    dd, _ = t.update(880.0)                      # -12% sans retrait
    assert dd == pytest.approx(0.12)            # > kill_switch_dd 10% → déclencherait


def test_new_utc_day_resets_day_start(monkeypatch):
    import risk.risk_tracker as rt
    import datetime as dt
    t, _ = _tracker(flow=lambda s: (0.0, s))
    t.update(1000.0)
    t.update(1100.0)                            # day_start=1000, daily +10%
    # force le jour suivant
    class _D(dt.datetime):
        @classmethod
        def now(cls, tz=None): return dt.datetime(2099, 1, 2, tzinfo=tz)
    monkeypatch.setattr(rt.dt, "datetime", _D)
    dd, daily = t.update(1100.0)               # nouveau jour → day_start re-basé sur 1100
    assert daily == pytest.approx(0.0)


def test_reset_sentinel_rebases_peak():
    t, d = _tracker(flow=lambda s: (0.0, s))
    t.update(1000.0)
    t.update(700.0)                            # DD 30%
    (d / "RESET_PEAK").touch()
    dd, daily = t.update(700.0)                # sentinelle → peak=700, DD=0
    assert dd == 0.0 and daily == 0.0
    assert not (d / "RESET_PEAK").exists()     # consommée


def test_zero_equity_failsafe():
    t, _ = _tracker()
    assert t.update(0.0) == (0.0, 0.0)
    assert t.update(-5.0) == (0.0, 0.0)


def test_persistence_across_instances():
    d = Path(tempfile.mkdtemp())
    p = d / "risk_baseline.json"
    t1 = RiskTracker(state_path=p, flow_fn=lambda s: (0.0, s), flow_poll_sec=0.0)
    t1.update(1000.0); t1.update(1500.0)       # peak=1500 persisté
    t2 = RiskTracker(state_path=p, flow_fn=lambda s: (0.0, s), flow_poll_sec=0.0)
    dd, _ = t2.update(1350.0)                  # -10% du peak 1500 chargé
    assert dd == pytest.approx(0.10)


def test_flow_guard_rebases_on_absurd_outflow():
    """Garde anti-corruption (bug 2026-06-27) : un flux qui viderait la référence (re-application
    en boucle) → re-base sur l'equity au lieu de s'effondrer à ~0."""
    t, _ = _tracker(flow=lambda s: (-2000.0, s + 1))   # sortie > tout le capital
    t.update(1000.0)                                    # init peak=day_start=1000
    dd, daily = t.update(1000.0)                        # flux absurde → re-base
    assert daily == pytest.approx(0.0)                  # day_start re-basé sur equity
    assert dd == pytest.approx(0.0)
    assert t.peak >= 990.0                              # peak NON effondré


def test_repeated_same_flow_does_not_compound_to_zero():
    """Même si le curseur ne bouge pas (flux re-renvoyé), day_start ne tombe jamais à ~0."""
    t, _ = _tracker(flow=lambda s: (-60.0, s))          # MÊME flux à chaque appel (curseur figé)
    t.update(1000.0)
    for _ in range(50):
        _, daily = t.update(1000.0)
    assert t.peak > 0.05 * 1000.0                       # jamais effondré au plancher
    assert t._day_start > 0.05 * 1000.0
