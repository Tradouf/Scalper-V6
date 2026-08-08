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


# ── Un événement = un déclenchement (hystérésis) ────────────────────────────

def _drive(c, liqfeed, coin, seq, oi0=1_000_000.0):
    """Injecte une suite de ΔOI (fractions de l'OI) et renvoie les rafales."""
    fired = []
    for d_frac in seq:
        c.hist[coin].clear()
        for i in range(12):
            c.hist[coin].append((i, oi0 * d_frac / 12, oi0, 0.0, 100.0,
                                 10.0, 5000.0))
        h = c.hist[coin]
        oi_now = h[-1][2]
        d_pct = sum(x[1] for x in h) / oi_now
        max_ntl = max(x[6] for x in h)
        if d_pct > -liqfeed.BURST_DOI_PCT * liqfeed.BURST_REARM_FRAC:
            c.armed[coin] = True
        if d_pct <= -liqfeed.BURST_DOI_PCT and max_ntl >= liqfeed.BURST_MIN_MAX_NTL:
            if c.armed[coin]:
                c.armed[coin] = False
                fired.append(d_pct)
    return fired


def test_same_drop_fires_only_once(collector, monkeypatch):
    """Une chute qui persiste dans la fenêtre ne doit pas être recomptée."""
    c, liqfeed = collector
    # la même chute de −0.5 % vue 4 fois de suite (fenêtre glissante)
    fired = _drive(c, liqfeed, "INJ", [-0.005] * 4)
    assert len(fired) == 1


def test_new_drop_after_recovery_fires_again(collector):
    """Après retour à la normale, un nouvel événement doit redéclencher."""
    c, liqfeed = collector
    fired = _drive(c, liqfeed, "INJ", [-0.005, -0.005, 0.0, -0.005])
    assert len(fired) == 2


def test_guard_delay_covers_analysis_window(collector):
    """Le délai de garde doit être ≥ la fenêtre, sinon recomptage garanti."""
    from rsimr import liqfeed
    assert liqfeed.PROBE_MIN_GAP_SEC >= liqfeed.BURST_WINDOW_SEC


# ── Filtre de sens sur le prix ──────────────────────────────────────────────

def test_forced_side_from_price():
    from rsimr import liqfeed
    assert liqfeed.forced_side(-0.006) == "vente"   # prix en baisse
    assert liqfeed.forced_side(+0.010) == "achat"   # prix en hausse
    assert liqfeed.forced_side(0.0) == "vente"      # borne : ≤ 0


def test_wrong_side_is_logged_without_spending_budget(collector, monkeypatch):
    """Une rafale d'achat forcé est enregistrée mais ne consomme aucune sonde."""
    c, liqfeed = collector
    monkeypatch.setattr(liqfeed, "BURST_SIDE", "vente")
    calls = []
    monkeypatch.setattr(liqfeed, "post_info",
                        lambda *a, **k: calls.append(a) or [])
    c.verify_burst("AAA", -0.03, 0.5, ["0x1", "0x2"], d_px=+0.012,
                   max_ntl=2500.0)
    assert calls == []                       # aucune requête réseau
    row = c.con.execute(
        "SELECT n_addr, side, d_px_pct FROM probe").fetchone()
    assert row[0] == 0 and row[1] == "achat"  # journalisée quand même
    assert len(c.probe_times) == 0            # budget intact


def test_right_side_is_probed(collector, monkeypatch):
    c, liqfeed = collector
    monkeypatch.setattr(liqfeed, "BURST_SIDE", "vente")
    calls = []
    monkeypatch.setattr(liqfeed, "post_info",
                        lambda body, **k: calls.append(body["user"]) or [])
    c.verify_burst("AAA", -0.03, 0.5, ["0x1"], d_px=-0.008, max_ntl=2500.0)
    assert calls == ["0x1"]
    assert c.con.execute("SELECT side FROM probe").fetchone()[0] == "vente"


def test_both_sides_mode_probes_everything(collector, monkeypatch):
    c, liqfeed = collector
    monkeypatch.setattr(liqfeed, "BURST_SIDE", "les_deux")
    calls = []
    monkeypatch.setattr(liqfeed, "post_info",
                        lambda body, **k: calls.append(body["user"]) or [])
    c.verify_burst("AAA", -0.03, 0.5, ["0x1"], d_px=+0.02, max_ntl=2500.0)
    assert calls == ["0x1"]
