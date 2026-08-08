"""Tests de l'exécuteur live RSI-MR — sécurités, fenêtre de tir, cycle complet.

Aucun réseau, aucun ordre : le client est un faux, et le dry-run est vérifié
séparément du chemin live (où l'on contrôle que smart_entry/smart_close sont
bien appelés).
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsimr import live as L  # noqa: E402
from rsimr.paper import HOUR_MS  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "STATE_FILE", tmp_path / "live_state.json")
    monkeypatch.setattr(L, "FETCH_THROTTLE_SEC", 0.0)
    yield


NOW0 = 1_700_000_000.0    # base temporelle réaliste pour les sweeps


class Feed:
    """Faux fetch : produit des bougies 1h qui CLÔTURENT juste avant `now`.

    Indispensable — `closed_candles` écarte toute bougie dont la fenêtre n'est
    pas terminée à l'instant du sweep.
    """

    def __init__(self, closes, now=NOW0):
        self.closes = list(closes)
        self.now = now

    def __call__(self, sym, tf, days=None):
        n = len(self.closes)
        end = int(self.now * 1000)
        return [{"ts": end - (n - i) * HOUR_MS, "open": c, "high": c,
                 "low": c, "close": c, "volume": 1.0}
                for i, c in enumerate(self.closes)]


def series_crossing_up(n=260):
    """Série qui descend (RSI ≤30) puis remonte d'un coup → croisement 30↑."""
        # baisse régulière : RSI s'effondre bien sous 30
    closes = [100.0 * (0.995 ** i) for i in range(n - 1)]
    closes.append(closes[-1] * 1.06)   # rebond final → RSI repasse au-dessus
    return closes


class FakeClient:
    def __init__(self, equity=200.0):
        self.equity = equity
        self.calls = []

    def get_portfolio_value(self):
        return self.equity

    def cancel_all_orders(self, sym):
        self.calls.append(("cancel", sym))

    def market_close(self, sym):
        self.calls.append(("market_close", sym))


# ── Sécurité wallet ─────────────────────────────────────────────────────────

def test_live_refuses_missing_key(monkeypatch):
    monkeypatch.delenv(L.ENV_PRIVATE_KEY, raising=False)
    with pytest.raises(RuntimeError, match="manquant"):
        L.make_live_client()


def test_live_refuses_wallet_shared_with_active_bot(monkeypatch):
    monkeypatch.setenv(L.ENV_PRIVATE_KEY, "0xdead")
    monkeypatch.setenv("HL4_PRIVATE_KEY", "0xdead")   # xsmom = bot actif
    with pytest.raises(RuntimeError, match="partage interdit"):
        L.make_live_client()


def test_dry_run_is_the_default(monkeypatch):
    monkeypatch.delenv("RSIMR_DRY_RUN", raising=False)
    assert L.RSIMRLiveTrader(client=FakeClient()).dry_run is True


# ── Fenêtre de tir : le régime calme ne doit jamais être tradé ───────────────

def test_filtered_regime_detects_relative_calm():
    """Le régime est relatif au symbole : agité puis calmé → état calme."""
    import random
    rng = random.Random(4)
    closes = [100.0]
    for _ in range(200):                      # phase agitée
        closes.append(closes[-1] * (1 + rng.gauss(0, 0.02)))
    for _ in range(60):                       # puis nettement plus calme
        closes.append(closes[-1] * (1 + rng.gauss(0, 0.0015)))
    assert L.filtered_regime(closes) == 0


def test_filtered_regime_uniform_series_is_not_calm():
    """Une série régulière n'est pas « calme » : le régime est relatif, pas absolu."""
    closes = [100.0 + 0.01 * ((-1) ** i) for i in range(200)]
    assert L.filtered_regime(closes) == 1


def test_filtered_regime_is_causal_and_robust():
    assert L.filtered_regime([100.0] * 10) == 1        # trop court → neutre
    assert L.filtered_regime([100.0] * 200) == 1       # sigma nul → neutre


def test_calm_regime_signal_is_skipped(monkeypatch):
    t = L.RSIMRLiveTrader(client=FakeClient(), dry_run=True, symbols=["AAA"])
    monkeypatch.setattr(L, "filtered_regime", lambda closes: 0)
    t._fetch = Feed(series_crossing_up())
    assert t.sweep_if_due(now=NOW0) is True
    assert t.state["positions"] == {}
    assert t.state["skipped"]["regime_calm"] == 1


def test_storm_regime_gets_reduced_size(monkeypatch):
    t = L.RSIMRLiveTrader(client=FakeClient(), dry_run=True, symbols=["AAA"])
    t.state["dry_equity"] = 200.0
    monkeypatch.setattr(L, "filtered_regime", lambda closes: 2)
    t._fetch = Feed(series_crossing_up())
    t.sweep_if_due(now=NOW0)
    pos = t.state["positions"]["AAA"]
    expected = 200.0 * L.NOTIONAL_PCT * L.REGIME_SIZE[2]
    assert pos["notional"] == pytest.approx(expected, rel=1e-6)
    assert pos["regime"] == 2


# ── Cycle entrée → sortie temporelle ────────────────────────────────────────

def test_entry_then_timed_exit_after_h_bars(monkeypatch):
    t = L.RSIMRLiveTrader(client=FakeClient(), dry_run=True, symbols=["AAA"])
    monkeypatch.setattr(L, "filtered_regime", lambda closes: 1)
    feed = Feed(series_crossing_up())
    t._fetch = feed
    t0 = NOW0
    t.sweep_if_due(now=t0)
    assert "AAA" in t.state["positions"]
    entry_px = t.state["positions"]["AAA"]["entry"]

    # 4 h plus tard, prix +2 % → sortie due, PnL positif net des frais.
    # Série montante : le RSI reste haut, donc AUCUN nouveau signal ne vient
    # brouiller la vérification de la sortie.
    up = [entry_px * (1 + 0.0002 * i) for i in range(259)]
    up.append(entry_px * 1.02)
    later = t0 + L.H_BARS * 3600 + 1
    feed.closes, feed.now = up, later
    t.sweep_if_due(now=later)
    assert t.state["positions"] == {}
    assert t.state["n_trades"] == 1
    assert t.state["realized_usd"] > 0


def test_position_not_closed_before_h_bars(monkeypatch):
    t = L.RSIMRLiveTrader(client=FakeClient(), dry_run=True, symbols=["AAA"])
    monkeypatch.setattr(L, "filtered_regime", lambda closes: 1)
    feed = Feed(series_crossing_up())
    t._fetch = feed
    t.sweep_if_due(now=NOW0)
    feed.now = NOW0 + 2 * 3600
    t.sweep_if_due(now=NOW0 + 2 * 3600)        # 2 h seulement
    assert "AAA" in t.state["positions"]
    assert t.state["n_trades"] == 0


def test_sweep_runs_once_per_hour(monkeypatch):
    t = L.RSIMRLiveTrader(client=FakeClient(), dry_run=True, symbols=["AAA"])
    monkeypatch.setattr(L, "filtered_regime", lambda closes: 1)
    t._fetch = Feed(series_crossing_up())
    assert t.sweep_if_due(now=NOW0) is True
    assert t.sweep_if_due(now=NOW0 + 10) is False


# ── Plafonds ────────────────────────────────────────────────────────────────

def test_min_notional_skips_signal(monkeypatch):
    t = L.RSIMRLiveTrader(client=FakeClient(), dry_run=True, symbols=["AAA"])
    t.state["dry_equity"] = 20.0        # 20 × 0.12 = 2.4 $ < 11 $
    monkeypatch.setattr(L, "filtered_regime", lambda closes: 1)
    t._fetch = Feed(series_crossing_up())
    t.sweep_if_due(now=NOW0)
    assert t.state["positions"] == {}
    assert t.state["skipped"]["min_notional"] == 1


def test_max_concurrent_slots_enforced(monkeypatch):
    syms = [f"S{i}" for i in range(5)]
    t = L.RSIMRLiveTrader(client=FakeClient(), dry_run=True, symbols=syms)
    t.state["dry_equity"] = 1000.0
    monkeypatch.setattr(L, "MAX_CONCURRENT", 2)
    monkeypatch.setattr(L, "filtered_regime", lambda closes: 1)
    t._fetch = Feed(series_crossing_up())
    t.sweep_if_due(now=NOW0)
    assert len(t.state["positions"]) == 2
    assert t.state["skipped"]["slots"] == 3


def test_gross_exposure_cap_enforced(monkeypatch):
    syms = [f"S{i}" for i in range(6)]
    t = L.RSIMRLiveTrader(client=FakeClient(), dry_run=True, symbols=syms)
    t.state["dry_equity"] = 100.0
    monkeypatch.setattr(L, "MAX_GROSS_PCT", 0.30)   # 30 $ max ; 12 $/trade
    monkeypatch.setattr(L, "MAX_CONCURRENT", 99)
    monkeypatch.setattr(L, "filtered_regime", lambda closes: 1)
    t._fetch = Feed(series_crossing_up())
    t.sweep_if_due(now=NOW0)
    gross = sum(p["notional"] for p in t.state["positions"].values())
    assert gross <= 30.0 + 1e-9
    assert t.state["skipped"]["gross_cap"] >= 1


# ── Kill-switch ─────────────────────────────────────────────────────────────

def test_kill_switch_needs_confirmations_then_flattens(monkeypatch):
    c = FakeClient(equity=100.0)
    t = L.RSIMRLiveTrader(client=c, dry_run=False, symbols=["AAA"])
    monkeypatch.setattr(L, "KILL_CONFIRMATIONS", 2)
    monkeypatch.setattr(L, "KILL_LOSS_PCT", 0.05)
    now = time.time()
    assert t.kill_switch_engaged(now) is False        # pic établi à 100
    t.state["positions"]["AAA"] = {"dir": 1, "entry": 1.0, "sz": 5.0,
                                   "notional": 5.0, "opened_ms": 0, "regime": 1}
    c.equity = 90.0                                   # −10 % : franchit le seuil
    assert t.kill_switch_engaged(now + 1) is True     # 1re confirmation
    assert t.state["positions"]                       # pas encore fermé
    assert t.kill_switch_engaged(now + 2) is True     # 2e → fermeture
    assert ("market_close", "AAA") in c.calls
    assert t.state["positions"] == {}
    assert t.state["paused_until"] > now


def test_freeze_after_repeated_equity_read_failures(monkeypatch):
    class Broken(FakeClient):
        def get_portfolio_value(self):
            raise RuntimeError("API muette")

    # démarrage sain, puis l'API devient muette en cours de route
    t = L.RSIMRLiveTrader(client=FakeClient(equity=200.0), dry_run=False)
    t.client = Broken()
    monkeypatch.setattr(L, "KILL_MAX_READ_FAILURES", 2)
    assert t.kill_switch_engaged(time.time()) is True
    assert t.frozen_reason is None
    assert t.kill_switch_engaged(time.time()) is True
    assert t.frozen_reason == "equity illisible"
    # une fois gelé, plus aucun trading
    assert t.kill_switch_engaged(time.time()) is True


def test_pause_blocks_trading(monkeypatch):
    t = L.RSIMRLiveTrader(client=FakeClient(), dry_run=True, symbols=["AAA"])
    t.state["paused_until"] = time.time() + 3600
    monkeypatch.setattr(L, "filtered_regime", lambda closes: 1)
    t._fetch = Feed(series_crossing_up(), now=time.time())
    t.sweep_if_due(now=time.time())
    assert t.state["positions"] == {}


# ── Chemin live : les ordres passent bien par l'exécution maker-first ────────

def test_live_path_uses_smart_entry_and_close(monkeypatch):
    t = L.RSIMRLiveTrader(client=FakeClient(equity=500.0), dry_run=False,
                          symbols=["AAA"])
    seen = []
    monkeypatch.setattr(L, "smart_entry", lambda *a, **k: (
        seen.append(("entry", k.get("is_buy"))) or
        {"mode": "maker", "avg_px": 10.0, "total_sz": 2.0}))
    monkeypatch.setattr(L, "smart_close", lambda *a, **k: (
        seen.append(("close", k.get("is_buy"))) or
        {"mode": "maker", "avg_px": 11.0, "total_sz": 2.0}))
    monkeypatch.setattr(L, "filtered_regime", lambda closes: 1)
    feed = Feed(series_crossing_up())
    t._fetch = feed
    t0 = NOW0
    t.sweep_if_due(now=t0)
    assert seen == [("entry", True)]                  # long only
    assert t.state["positions"]["AAA"]["entry"] == 10.0
    later = t0 + L.H_BARS * 3600 + 1
    feed.now = later
    t.sweep_if_due(now=later)
    assert ("close", False) in seen                   # sortie = vente
    assert t.state["realized_usd"] > 0


# ── Persistance ─────────────────────────────────────────────────────────────

def test_state_survives_restart(monkeypatch):
    t = L.RSIMRLiveTrader(client=FakeClient(), dry_run=True, symbols=["AAA"])
    monkeypatch.setattr(L, "filtered_regime", lambda closes: 1)
    t._fetch = Feed(series_crossing_up())
    t.sweep_if_due(now=NOW0)
    assert L.STATE_FILE.exists()
    saved = json.loads(L.STATE_FILE.read_text())
    assert "AAA" in saved["positions"]
    # un nouveau trader relit l'état : la position reste connue et sortira
    t2 = L.RSIMRLiveTrader(client=FakeClient(), dry_run=True, symbols=["AAA"])
    assert "AAA" in t2.state["positions"]


# ── API wallet vs compte maître (piège « equity fantôme ») ──────────────────

def test_live_requires_master_account_address(monkeypatch):
    """Sans compte maître, l'equity lue serait celle de l'agent (0 $)."""
    monkeypatch.setenv(L.ENV_PRIVATE_KEY, "0xkey")
    monkeypatch.delenv(L.ENV_ACCOUNT_ADDRESS, raising=False)
    with pytest.raises(RuntimeError, match="compte maître"):
        L.make_live_client()


def test_live_refuses_to_start_on_zero_equity():
    """Equity nulle = presque toujours une lecture sur la mauvaise adresse."""
    class Empty(FakeClient):
        def get_portfolio_value(self):
            return 0.0

    with pytest.raises(RuntimeError, match="refus de démarrer"):
        L.RSIMRLiveTrader(client=Empty(), dry_run=False)


def test_live_starts_when_equity_is_readable():
    t = L.RSIMRLiveTrader(client=FakeClient(equity=207.9), dry_run=False)
    assert t.dry_run is False


def test_client_passes_master_address(monkeypatch):
    """Le compte maître doit atteindre HyperliquidClient, sinon lecture agent."""
    seen = {}

    class FakeHL:
        def __init__(self, wallet_key=None, account_address=None, **kw):
            seen["key"] = wallet_key
            seen["account_address"] = account_address

    import hyperliquid_client
    monkeypatch.setattr(hyperliquid_client, "HyperliquidClient", FakeHL)
    monkeypatch.setenv(L.ENV_PRIVATE_KEY, "0xkey")
    monkeypatch.setenv(L.ENV_ACCOUNT_ADDRESS, "0xMASTER")
    L.make_live_client()
    assert seen == {"key": "0xkey", "account_address": "0xMASTER"}
