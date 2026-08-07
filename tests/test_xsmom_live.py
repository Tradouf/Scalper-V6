# -*- coding: utf-8 -*-
"""Tests de l'exécuteur live xsmom (xsmom/live.py) — aucun réseau, état isolé.

Le juge de la stratégie reste xsmom/paper.py : ces tests ne valident QUE
l'infra portée de SimpleBot (dry-run, kill-switch, réconciliation, wallet
dédié, min-notionnel, exécution maker-first).
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import xsmom.live as live
from xsmom.live import XSMomentumLiveTrader, make_live_client
from xsmom.paper import DAY_MS, N_LEG, N_TRANCHES

NOW = time.time()
N_SYMS = 26


# ── Fakes déterministes (aucun réseau) ───────────────────────────────────────

def fake_universe(top_n=None):
    return [f"S{i:02d}" for i in range(N_SYMS)]


def fake_fetch(sym, interval, days):
    """45 j de bougies 1d closes : drift croissant avec l'index du symbole."""
    i = int(sym[1:])
    drift = (i - N_SYMS / 2) * 0.004          # S00 baisse, S25 monte
    out, px = [], 100.0
    n = 45
    for d in range(n):
        px *= 1.0 + drift + (0.003 if d % 2 else -0.003)   # vol > 0
        ts = int((NOW - (n - d + 1) * 86_400) * 1000)
        out.append({"ts": ts, "open": px, "high": px * 1.01,
                    "low": px * 0.99, "close": px, "volume": 1000.0})
    return out


def fake_funding():
    return {}


def make_trader(tmp_path, dry_run=True, client=None, **kw):
    return XSMomentumLiveTrader(
        client=client, dry_run=dry_run,
        fetch=fake_fetch, funding_fetch=fake_funding,
        universe_fetch=fake_universe,
        ledger_fetch=lambda addr, start_ms: [],
        state_file=tmp_path / "xsmom_live_state.json",
        sleep=lambda s: None,
        **kw,
    )


class FakeLiveClient:
    """Client minimal pour les chemins live (réconciliation, equity)."""

    def __init__(self, portfolio=2000.0, positions=None):
        self.portfolio = portfolio
        self.positions = positions or []
        self.wallet_address = "0xFAKE"
        self.closed = []

    def get_portfolio_value(self):
        if isinstance(self.portfolio, Exception):
            raise self.portfolio
        return self.portfolio

    def get_positions(self, coin=None):
        return self.positions

    def cancel_all_orders(self, coin=None):
        return 0

    def market_close(self, coin):
        self.closed.append(coin)
        return {}


# ── Dry-run : chemin complet sans client ─────────────────────────────────────

def test_dry_run_rebalance_opens_full_tranche(tmp_path):
    tr = make_trader(tmp_path)
    assert tr.rebalance_if_due(NOW) is True
    day = int(NOW // 86_400)
    k = day % N_TRANCHES
    tranche = tr.state["tranches"][k]
    assert len(tranche) == 2 * N_LEG                       # 8 longs + 8 shorts
    longs = [s for s, p in tranche.items() if p["dir"] == 1]
    shorts = [s for s, p in tranche.items() if p["dir"] == -1]
    # le score ret/vol suit le drift : les plus hauts index sont longs
    assert all(int(s[1:]) >= N_SYMS - 12 for s in longs)
    assert all(int(s[1:]) <= 11 for s in shorts)
    assert tr.state["exec_stats"]["maker"] == 2 * N_LEG
    assert tr.state["rebalances"][-1]["n_opened"] == 2 * N_LEG
    # même jour → pas de second rebalance
    assert tr.rebalance_if_due(NOW + 60) is False
    # état persisté et rechargeable
    tr2 = make_trader(tmp_path)
    assert tr2.state["last_rebalance_day"] == day


def test_dry_run_needs_no_client(tmp_path):
    tr = make_trader(tmp_path, client=None)
    assert tr.client is None
    assert tr.rebalance_if_due(NOW) is True                # aucun appel d'ordre


def test_min_notional_skips_positions(tmp_path):
    tr = make_trader(tmp_path)
    tr.state["dry_equity"] = 500.0                         # 500/112 ≈ 4.5 $ < 11 $
    assert tr.rebalance_if_due(NOW) is True
    day = int(NOW // 86_400)
    assert tr.state["tranches"][day % N_TRANCHES] == {}
    assert tr.state["skipped_min_notional"] == 2 * N_LEG
    assert tr.state["rebalances"][-1]["n_opened"] == 0


# ── Kill-switch (port SimpleBot : hystérésis, gel lecture, pause) ────────────

def test_kill_switch_hysteresis_two_confirmations(tmp_path):
    tr = make_trader(tmp_path)
    tr.state["equity_history"] = [[NOW - 1000, 2000.0]]    # pic
    tr.state["dry_equity"] = 1800.0                        # −10 % > seuil 5 %
    assert tr.kill_switch_engaged(NOW) is False            # confirmation 1/2
    assert tr.kill_switch_engaged(NOW + 1) is True         # 2/2 → pause
    assert tr.state["paused_until"] > NOW
    assert tr.kill_switch_engaged(NOW + 2) is True         # pause active
    assert tr.rebalance_if_due(NOW + 3) is False           # pas de trading


def test_kill_switch_single_breach_resets(tmp_path):
    tr = make_trader(tmp_path)
    tr.state["equity_history"] = [[NOW - 1000, 2000.0]]
    tr.state["dry_equity"] = 1800.0
    assert tr.kill_switch_engaged(NOW) is False            # 1/2
    tr.state["dry_equity"] = 1990.0                        # retour au-dessus
    assert tr.kill_switch_engaged(NOW + 1) is False
    assert tr._kill_breach_count == 0                      # hystérésis reset


def test_kill_switch_read_failures_freeze_entries(tmp_path):
    client = FakeLiveClient(positions=[])
    client.portfolio = RuntimeError("429")
    tr = make_trader(tmp_path, dry_run=False, client=client)
    assert tr.frozen_reason is None                        # boot OK (0 positions)
    assert tr.kill_switch_engaged(NOW) is False            # échec 1/3
    assert tr.kill_switch_engaged(NOW + 1) is False        # 2/3
    assert tr.kill_switch_engaged(NOW + 2) is True         # 3/3 → gel
    client.portfolio = 2000.0                              # lecture rétablie
    assert tr.kill_switch_engaged(NOW + 3) is False


def test_kill_switch_withdrawal_rebases_instead_of_killing(tmp_path):
    client = FakeLiveClient(portfolio=1000.0, positions=[])
    tr = make_trader(tmp_path, dry_run=False, client=client)
    tr._ledger_fetch = lambda addr, start_ms: [{"delta": {
        "type": "withdraw", "usdc": "1000.0"}, "time": int(NOW * 1000)}]
    # net_transfer_flow lit le format réel du ledger ; on courtcircuite ici
    # en patchant directement le calcul d'outflow.
    tr._external_outflow_since = lambda ts: 1000.0
    tr.state["equity_history"] = [[NOW - 1000, 2000.0]]    # pic avant retrait
    assert tr.kill_switch_engaged(NOW) is False            # rebase, pas de kill
    assert tr.state["paused_until"] == 0.0
    assert [v for _, v in tr.state["equity_history"]] == [1000.0]


# ── Réconciliation au boot & wallet dédié ────────────────────────────────────

def test_reconcile_mismatch_freezes_trading(tmp_path):
    client = FakeLiveClient(positions=[{"coin": "BTC", "szi": 1.5}])
    tr = make_trader(tmp_path, dry_run=False, client=client)
    assert tr.frozen_reason is not None and "BTC" in tr.frozen_reason
    assert tr.rebalance_if_due(NOW) is False               # gelé


def test_reconcile_unreadable_freezes(tmp_path):
    client = FakeLiveClient(positions=[])
    client.get_positions = lambda coin=None: (_ for _ in ()).throw(RuntimeError("down"))
    tr = make_trader(tmp_path, dry_run=False, client=client)
    assert tr.frozen_reason is not None


def test_make_live_client_requires_dedicated_wallet(monkeypatch):
    monkeypatch.delenv("HL4_PRIVATE_KEY", raising=False)
    with pytest.raises(RuntimeError, match="HL4_PRIVATE_KEY"):
        make_live_client()
    monkeypatch.setenv("HL4_PRIVATE_KEY", "0xdeadbeef")
    monkeypatch.setenv("HL2_PRIVATE_KEY", "0xdeadbeef")    # wallet SimpleBot
    with pytest.raises(RuntimeError, match="wallet partagé interdit"):
        make_live_client()


# ── Exécution live : maker-first entrées ET sorties (reduce-only) ────────────

def test_live_rebalance_uses_smart_entry_and_smart_close(tmp_path, monkeypatch):
    calls = {"entry": [], "close": []}

    def fake_entry(client, coin, is_buy, sz, ref_price, **kw):
        calls["entry"].append((coin, is_buy))
        return {"mode": "maker", "avg_px": ref_price, "total_sz": sz}

    def fake_close(client, coin, is_buy, sz, ref_price, **kw):
        calls["close"].append((coin, is_buy))
        return {"mode": "maker", "avg_px": ref_price, "total_sz": sz}

    monkeypatch.setattr(live, "smart_entry", fake_entry)
    monkeypatch.setattr(live, "smart_close", fake_close)

    client = FakeLiveClient(portfolio=2000.0, positions=[])
    tr = make_trader(tmp_path, dry_run=False, client=client)
    # une position pré-existante DANS la tranche du jour → doit être fermée
    day = int(NOW // 86_400)
    k = day % N_TRANCHES
    tr.state["tranches"][k]["OLD"] = {
        "dir": 1, "entry": 10.0, "sz": 2.0, "notional": 20.0, "mark": 10.0}

    assert tr.rebalance_if_due(NOW) is True
    assert calls["close"] == [("OLD", False)]              # ferme le long → sell
    assert len(calls["entry"]) == 2 * N_LEG
    assert tr.state["exec_stats"]["maker"] == 2 * N_LEG + 1


def test_live_skipped_entry_not_in_state(tmp_path, monkeypatch):
    """Une entrée skippée (maker non rempli, pas de fallback) ne doit PAS
    apparaître dans l'état — sinon la réconciliation divergerait."""
    def fake_entry(client, coin, is_buy, sz, ref_price, **kw):
        return {"mode": "skip", "avg_px": 0.0, "total_sz": 0.0}

    monkeypatch.setattr(live, "smart_entry", fake_entry)
    client = FakeLiveClient(portfolio=2000.0, positions=[])
    tr = make_trader(tmp_path, dry_run=False, client=client)
    assert tr.rebalance_if_due(NOW) is True
    day = int(NOW // 86_400)
    assert tr.state["tranches"][day % N_TRANCHES] == {}
    assert tr.state["exec_stats"]["skip"] == 2 * N_LEG
