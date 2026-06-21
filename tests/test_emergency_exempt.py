"""Exemption TSMOM de l'emergency exit (2026-06-20). Un trend-follower 1d traverse
routinièrement -2,2% de ROE pour attraper les gros trends → il DOIT être exempté du
force-close ROE (protégé par caps globaux + kill-switch DD à la place). Vérifie qu'un
symbole exempté en zone rouge est ignoré, et qu'un symbole non exempté est bien fermé."""
from __future__ import annotations

from core.config import RiskConfig
from risk.emergency_exit import EmergencyExitManager


class _FakeRead:
    def __init__(self, positions):
        self._p = positions

    def get_positions_detailed(self):
        return self._p


class _FakeResult:
    status = "filled"


class _FakeWrite:
    def __init__(self):
        self.closed = []

    def place_order(self, req):
        self.closed.append(req.symbol)
        return _FakeResult()


class _FakePortfolio:
    def __init__(self, positions):
        self._positions = positions

    @property
    def positions(self):
        return self._positions


def _pos(roe, szi=1.0, lev=1.0):
    return {"roe": roe, "leverage": lev, "szi": szi,
            "entry_px": 100.0, "mark_px": 97.0, "side": "buy" if szi > 0 else "sell"}


def _mgr(read, write, portfolio, exempt):
    cfg = RiskConfig(emergency_exit_enabled=True, emergency_exit_roe_pct=0.022,
                     manual_symbols=[])
    return EmergencyExitManager(cfg=cfg, read_adapter=read, write_adapter=write,
                                portfolio=portfolio, paper_mode=False,
                                exempt_symbols=exempt)


def test_exempt_symbol_not_force_closed():
    # NEAR (TSMOM, exempté) à -5% ROE → JAMAIS fermé. SOL (non exempté) à -5% → fermé.
    positions = {"NEAR": _pos(-0.05), "SOL": _pos(-0.05)}
    read = _FakeRead(positions)
    write = _FakeWrite()
    pf = _FakePortfolio({"NEAR": 100.0, "SOL": 100.0})  # tracées
    mgr = _mgr(read, write, pf, exempt={"NEAR"})
    out = mgr.check_and_exit()
    assert "NEAR" not in write.closed       # exempté → pas touché
    assert "SOL" in write.closed            # non exempté → force-close
    assert out["tracked_emergency"] == 1


def test_exempt_symbol_in_profit_is_noop_anyway():
    # Symbole exempté hors zone (ROE positif) → rien, comme avant.
    positions = {"NEAR": _pos(+0.10)}
    write = _FakeWrite()
    mgr = _mgr(_FakeRead(positions), write, _FakePortfolio({"NEAR": 100.0}), exempt={"NEAR"})
    out = mgr.check_and_exit()
    assert write.closed == []
    assert out["tracked_emergency"] == 0


def test_no_exempt_preserves_legacy_behavior():
    # Sans exemption, le symbole en zone est fermé (comportement historique inchangé).
    positions = {"SOL": _pos(-0.05)}
    write = _FakeWrite()
    mgr = _mgr(_FakeRead(positions), write, _FakePortfolio({"SOL": 100.0}), exempt=set())
    mgr.check_and_exit()
    assert "SOL" in write.closed
