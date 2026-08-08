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
