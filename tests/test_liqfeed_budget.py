"""Budget global des sondes — protège le rate-limiter partagé en cascade."""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def collector(monkeypatch):
    tmp = Path(tempfile.mkdtemp()) / "liq.db"
    monkeypatch.setenv("LIQFEED_DB", str(tmp))
    from rsimr import liqfeed
    liqfeed.DB_PATH = tmp
    c = liqfeed.Collector(["AAA"])
    yield c, liqfeed
    c.con.close()


def test_probe_budget_caps_requests(collector, monkeypatch):
    c, liqfeed = collector
    monkeypatch.setattr(liqfeed, "PROBE_BUDGET_PER_MIN", 3)
    taken = sum(1 for _ in range(10) if c._take_probe_token())
    assert taken == 3


def test_probe_budget_releases_after_window(collector, monkeypatch):
    c, liqfeed = collector
    monkeypatch.setattr(liqfeed, "PROBE_BUDGET_PER_MIN", 2)
    assert c._take_probe_token() and c._take_probe_token()
    assert not c._take_probe_token()
    # jetons vieillis de plus de 60 s → budget rendu
    c.probe_times = type(c.probe_times)(
        [t - 61.0 for t in c.probe_times], maxlen=c.probe_times.maxlen)
    assert c._take_probe_token()


def test_verify_burst_stops_when_budget_exhausted(collector, monkeypatch):
    c, liqfeed = collector
    monkeypatch.setattr(liqfeed, "PROBE_BUDGET_PER_MIN", 0)
    calls = []
    monkeypatch.setattr(liqfeed, "post_info",
                        lambda *a, **k: calls.append(a) or [])
    c.verify_burst("AAA", -0.02, 0.9, ["0x1", "0x2", "0x3"])
    assert calls == []                 # aucune requête réseau émise
    assert c.probe_skipped == 1
    row = c.con.execute("SELECT coin, n_liq FROM probe").fetchone()
    assert row == ("AAA", 0)           # la rafale est tout de même journalisée


# ── Détecteur calibré v2 ────────────────────────────────────────────────────

def test_probe_records_addresses_actually_probed(collector, monkeypatch):
    """n_addr doit refléter les sondes RÉELLES, sinon la précision est fausse."""
    c, liqfeed = collector
    monkeypatch.setattr(liqfeed, "PROBE_BUDGET_PER_MIN", 2)
    monkeypatch.setattr(liqfeed, "post_info", lambda *a, **k: [])
    c.verify_burst("AAA", -0.02, 0.5, ["0x1", "0x2", "0x3", "0x4"])
    n_addr = c.con.execute("SELECT n_addr FROM probe").fetchone()[0]
    assert n_addr == 2          # budget épuisé après 2, pas 4


def test_probe_keeps_burst_features(collector, monkeypatch):
    c, liqfeed = collector
    monkeypatch.setattr(liqfeed, "post_info", lambda *a, **k: [])
    c.verify_burst("AAA", -0.031, 0.42, ["0x1"], d_px=-0.012, max_ntl=2500.0)
    row = c.con.execute(
        "SELECT d_oi_pct, sell_ratio, d_px_pct, max_ntl FROM probe").fetchone()
    assert row == pytest.approx((-0.031, 0.42, -0.012, 2500.0))


def test_largest_counterparties_are_probed_first(collector, monkeypatch):
    """Les fills de liquidation sont gros : sonder les grosses adresses d'abord."""
    c, liqfeed = collector
    monkeypatch.setattr(liqfeed, "PROBE_BUDGET_PER_MIN", 2)
    seen = []
    monkeypatch.setattr(liqfeed, "post_info",
                        lambda body, **k: seen.append(body["user"]) or [])
    # verify_burst reçoit déjà la liste triée par taille décroissante
    c.verify_burst("AAA", -0.02, 0.5, ["0xBIG", "0xMID", "0xSMALL"])
    assert seen == ["0xBIG", "0xMID"]


def test_sell_ratio_no_longer_gates_detection(collector):
    """Le ratio de vente ne sépare pas (0.37 vs 0.42) : il ne doit plus filtrer."""
    from rsimr import liqfeed
    import inspect
    src = inspect.getsource(liqfeed.Collector.flush_loop)
    assert "sell_ratio >= 0.6" not in src
    assert "BURST_MIN_MAX_NTL" in src
