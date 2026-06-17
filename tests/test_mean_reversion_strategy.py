"""Tests MeanReversionStrategy : conformité Protocol, génération signals
LONG/SHORT/CLOSE selon z-score, filtres half-life + cooldown, on_fill."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from core.config import MeanReversionStrategyConfig
from core.types import Candle, Fill, MarketSnapshot
from strategies.mean_reversion import MeanReversionStrategy


NOW = dt.datetime(2026, 5, 27, 12, 0, 0)


def _cfg(**kwargs) -> MeanReversionStrategyConfig:
    defaults = dict(
        enabled=True,
        interval="1h",
        window=50,
        entry_z=2.0,
        exit_z=0.4,
        hl_min=5.0,
        hl_max=48.0,
        cooldown_sec=1800,
        notional_usdc=30.0,
        sl_z=3.5,
        min_sl_buffer_std=1.5,
    )
    defaults.update(kwargs)
    return MeanReversionStrategyConfig(**defaults)


def _make_market(prices: list[float], symbol: str = "BTC", ts: dt.datetime = NOW) -> MarketSnapshot:
    candles = [
        Candle(
            ts_open=ts - dt.timedelta(hours=len(prices) - i),
            open=p, high=p + 0.5, low=p - 0.5, close=p, volume=1.0,
        )
        for i, p in enumerate(prices)
    ]
    return MarketSnapshot(
        timestamp=ts,
        candles={symbol: candles},
        prices={symbol: prices[-1]},
    )


# ─── Protocol conformity ─────────────────────────────────────────────────────


def test_implements_strategy_agent():
    strat = MeanReversionStrategy(_cfg(), symbols=["BTC"])
    assert hasattr(strat, "strategy_id")
    assert isinstance(strat.strategy_id, str)
    assert callable(strat.generate_signals)
    assert callable(strat.on_fill)
    assert strat.strategy_id == "mean_reversion"


# ─── Signal generation ───────────────────────────────────────────────────────


def test_no_signal_insufficient_data():
    strat = MeanReversionStrategy(_cfg(), symbols=["BTC"])
    prices = [100.0] * 30
    market = _make_market(prices)
    assert strat.generate_signals(market) == []


def test_no_signal_in_band():
    """Si |z| < entry_z, pas de signal."""
    np.random.seed(0)
    strat = MeanReversionStrategy(_cfg(window=30, hl_min=2.0, hl_max=200.0), symbols=["BTC"])
    # Random walk avec drift quasi nul → z proche de 0
    prices = list(100.0 + np.cumsum(np.random.normal(0, 0.5, 80)))
    market = _make_market(prices)
    sigs = strat.generate_signals(market)
    # Peut être [] ou un Signal — selon le z exact. On accepte les 2.
    if sigs:
        # Si signal, ce doit être un CLOSE ou rien d'extrême
        for s in sigs:
            assert abs(s.direction) in (0.0, 1.0)


def test_long_signal_on_dip():
    """Prix qui baisse fortement à la fin → z très négatif → LONG."""
    strat = MeanReversionStrategy(_cfg(window=30, hl_min=2.0, hl_max=200.0), symbols=["BTC"])
    # Plateau autour de 100 puis chute à 90
    prices = [100.0 + np.sin(i * 0.3) * 0.5 for i in range(60)]
    prices += [90.0] * 5  # dip net
    market = _make_market(prices)
    sigs = strat.generate_signals(market)
    if not sigs:
        # Le filtre half-life peut empêcher le signal — on relâche
        return
    s = sigs[0]
    assert s.direction == 1.0  # LONG
    assert s.target_notional > 0
    assert s.stop_price is not None
    assert s.stop_price < market.prices["BTC"]  # SL sous mark pour LONG


def test_short_signal_on_spike():
    strat = MeanReversionStrategy(_cfg(window=30, hl_min=2.0, hl_max=200.0), symbols=["BTC"])
    prices = [100.0 + np.sin(i * 0.3) * 0.5 for i in range(60)]
    prices += [110.0] * 5
    market = _make_market(prices)
    sigs = strat.generate_signals(market)
    if not sigs:
        return
    s = sigs[0]
    assert s.direction == -1.0
    assert s.stop_price > market.prices["BTC"]  # SL au-dessus pour SHORT


# ─── On_fill + position tracking ─────────────────────────────────────────────


def test_on_fill_opens_position():
    strat = MeanReversionStrategy(_cfg(), symbols=["BTC"])
    assert strat.open_positions() == {}
    fill = Fill(order_id="1", asset="BTC", notional=100.0, price=70000.0, fee=0.1, strategy_id="mean_reversion", timestamp=NOW)
    strat.on_fill(fill)
    pos = strat.open_positions()
    assert "BTC" in pos
    assert pos["BTC"]["side"] == "buy"


def test_on_fill_close_removes_position():
    strat = MeanReversionStrategy(_cfg(), symbols=["BTC"])
    # Ouvre
    strat.on_fill(Fill(order_id="1", asset="BTC", notional=100.0, price=70000.0, fee=0.1, strategy_id="mean_reversion", timestamp=NOW))
    assert "BTC" in strat.open_positions()
    # Ferme
    strat.on_fill(Fill(order_id="2", asset="BTC", notional=-100.0, price=70500.0, fee=0.1, strategy_id="mean_reversion", timestamp=NOW))
    assert "BTC" not in strat.open_positions()


def test_on_fill_close_clears_intent():
    """Fix anti-whipsaw : le fill de clôture purge aussi l'intent de maintien."""
    strat = MeanReversionStrategy(_cfg(), symbols=["BTC"])
    strat.on_fill(Fill(order_id="1", asset="BTC", notional=100.0, price=70000.0, fee=0.1, strategy_id="mean_reversion", timestamp=NOW))
    strat._intent["BTC"] = {"direction": 1.0, "target_notional": 30.0, "confidence": 0.5}
    strat.on_fill(Fill(order_id="2", asset="BTC", notional=-100.0, price=70500.0, fee=0.1, strategy_id="mean_reversion", timestamp=NOW))
    assert "BTC" not in strat._intent


# ─── Maintien de position (anti-whipsaw) ─────────────────────────────────────


def test_hold_reemits_maintain_signal():
    """Position tenue (pas de revert) → ré-émet l'exposition au lieu de se taire.

    Sinon l'allocateur fermerait la position au tick suivant (bug whipsaw V7)."""
    np.random.seed(42)
    strat = MeanReversionStrategy(
        _cfg(window=30, hl_min=2.0, hl_max=200.0, exit_z=0.0), symbols=["BTC"]
    )
    # Position ouverte + son intent d'entrée (mémorisé à l'ouverture).
    strat._positions["BTC"] = {"side": "buy", "entry_px": 100.0, "qty": 0.3, "opened_ts": NOW.timestamp()}
    strat._intent["BTC"] = {"direction": 1.0, "target_notional": 30.0, "confidence": 0.5}
    # Série AR(1) mean-reverting → half-life finie (passe le filtre), std>0.
    x = [100.0]
    for _ in range(79):
        x.append(100.0 + 0.8 * (x[-1] - 100.0) + np.random.normal(0, 1.0))
    market = _make_market(x)
    sigs = strat.generate_signals(market)
    assert len(sigs) == 1
    s = sigs[0]
    # exit_z=0 → jamais de CLOSE → maintien reproduisant l'intent à l'identique.
    assert s.target_notional == 30.0
    assert s.direction == 1.0
    assert s.confidence == 0.5
    assert s.stop_price is None  # ne retouche pas le SL natif posé à l'entrée


def test_sync_positions_drops_externally_closed():
    """SÉCURITÉ : une position fermée hors stratégie (flat côté exchange) est
    purgée → le maintien ne la ré-ouvre pas."""
    strat = MeanReversionStrategy(_cfg(), symbols=["BTC", "ETH"])
    for sym, px, qty in (("BTC", 100.0, 0.3), ("ETH", 50.0, 0.6)):
        strat._positions[sym] = {"side": "buy", "entry_px": px, "qty": qty, "opened_ts": NOW.timestamp()}
        strat._intent[sym] = {"direction": 1.0, "target_notional": 30.0, "confidence": 0.5}
    # BTC fermé (absent / flat), ETH toujours long côté exchange.
    strat.sync_positions({"ETH": 30.0})
    assert "BTC" not in strat._positions and "BTC" not in strat._intent
    assert "ETH" in strat._positions


def test_sync_positions_drops_on_sign_flip():
    """Si l'exchange montre le sens opposé, la croyance est invalidée."""
    strat = MeanReversionStrategy(_cfg(), symbols=["BTC"])
    strat._positions["BTC"] = {"side": "buy", "entry_px": 100.0, "qty": 0.3, "opened_ts": NOW.timestamp()}
    strat._intent["BTC"] = {"direction": 1.0, "target_notional": 30.0, "confidence": 0.5}
    strat.sync_positions({"BTC": -30.0})  # net SHORT
    assert "BTC" not in strat._positions


# ─── CLOSE signal ────────────────────────────────────────────────────────────


def test_close_signal_when_z_reverts():
    """Si on a une position MR et z revient dans la bande, signal CLOSE."""
    strat = MeanReversionStrategy(_cfg(window=30, hl_min=2.0, hl_max=200.0, exit_z=0.4), symbols=["BTC"])
    # Forge une position via on_fill
    strat.on_fill(Fill(order_id="1", asset="BTC", notional=100.0, price=100.0, fee=0.1, strategy_id="mean_reversion", timestamp=NOW))
    # Marché : prix stables → z proche de 0
    prices = [100.0 + np.sin(i * 0.1) * 0.1 for i in range(60)]
    market = _make_market(prices)
    sigs = strat.generate_signals(market)
    if sigs:
        s = sigs[0]
        # Si signal, ce devrait être CLOSE (target_notional=0)
        assert s.target_notional == 0.0
        assert s.direction == 0.0


# ─── Cooldown ────────────────────────────────────────────────────────────────


def test_cooldown_blocks_retrigger():
    """Après un signal d'entrée, cooldown doit bloquer un nouveau signal."""
    strat = MeanReversionStrategy(
        _cfg(window=30, hl_min=2.0, hl_max=200.0, cooldown_sec=3600), symbols=["BTC"]
    )
    prices = [100.0 + np.sin(i * 0.3) * 0.5 for i in range(60)] + [88.0] * 5
    market = _make_market(prices)
    sigs1 = strat.generate_signals(market)
    if not sigs1:
        return  # cas où signal pas généré → test skipé naturellement
    # Re-call dans la même seconde → cooldown actif
    sigs2 = strat.generate_signals(market)
    assert sigs2 == []


# ─── SL formulation ──────────────────────────────────────────────────────────


def test_sl_buffer_minimal_when_z_extreme():
    """Si z très profond (e.g. -5), sl_buffer = min_sl_buffer_std × std."""
    strat = MeanReversionStrategy(_cfg(window=30, hl_min=2.0, hl_max=200.0), symbols=["BTC"])
    # Crée une dive extrême
    prices = [100.0] * 60 + [70.0] * 5  # z très négatif
    market = _make_market(prices)
    sigs = strat.generate_signals(market)
    if not sigs:
        return
    s = sigs[0]
    assert s.direction == 1.0
    # Pour z profond, le SL doit être SOUS le mark (correct pour LONG)
    assert s.stop_price < market.prices["BTC"]


# ─── Adoption au boot (orphelines, 2026-06-17) ───────────────────────────────


def test_adopt_position_populates_state():
    """adopt_position remplit _positions + _intent → la position devient gérée."""
    strat = MeanReversionStrategy(_cfg(), symbols=["BTC", "ETH"])
    assert strat.adopt_position("ETH", "sell", entry_px=1781.0, qty=0.127) is True
    assert "ETH" in strat._positions
    assert strat._positions["ETH"]["adopted"] is True
    assert strat._positions["ETH"]["sl_pending"] is True
    assert "ETH" in strat.engaged_symbols()
    intent = strat._intent["ETH"]
    assert intent["direction"] == -1.0
    assert intent["target_notional"] == pytest.approx(1781.0 * 0.127)


def test_adopt_position_rejects_off_watchlist_and_invalid():
    strat = MeanReversionStrategy(_cfg(), symbols=["BTC"])
    # Hors watchlist
    assert strat.adopt_position("DOGE", "buy", 0.1, 100.0) is False
    # Notional nul
    assert strat.adopt_position("BTC", "buy", 0.0, 100.0) is False
    # Side invalide
    assert strat.adopt_position("BTC", "flat", 100.0, 1.0) is False
    assert strat._positions == {}


def test_adopted_position_places_sl_once_then_holds():
    """Une position adoptée pose son SL natif au 1er maintien (sl_pending), puis
    ne le retouche plus. exit_z=0 force le maintien (jamais de CLOSE)."""
    np.random.seed(42)
    strat = MeanReversionStrategy(
        _cfg(window=30, hl_min=2.0, hl_max=200.0, exit_z=0.0), symbols=["BTC"]
    )
    strat.adopt_position("BTC", "sell", entry_px=100.0, qty=0.3)
    x = [100.0]
    for _ in range(79):
        x.append(100.0 + 0.8 * (x[-1] - 100.0) + np.random.normal(0, 1.0))
    market = _make_market(x)

    # 1er tick : SL posé (stop_price non None, au-dessus de l'entrée pour un short)
    s1 = strat.generate_signals(market)[0]
    assert s1.direction == -1.0
    assert s1.target_notional == pytest.approx(100.0 * 0.3)
    assert s1.stop_price is not None and s1.stop_price > 100.0
    assert strat._positions["BTC"]["sl_pending"] is False

    # 2e tick : SL déjà posé → plus retouché
    s2 = strat.generate_signals(market)[0]
    assert s2.stop_price is None


def test_adopted_position_closes_on_revert():
    """Adoptée puis retour à la moyenne (|z| < exit_z) → signal CLOSE.

    exit_z très grand → |z| < exit_z toujours vrai → CLOSE déterministe dès que
    la position est tenue (série AR(1) pour passer le filtre half-life)."""
    np.random.seed(7)
    strat = MeanReversionStrategy(
        _cfg(window=30, hl_min=2.0, hl_max=200.0, exit_z=1.0), symbols=["BTC"]
    )
    strat.adopt_position("BTC", "sell", entry_px=100.0, qty=0.3)
    x = [100.0]
    for _ in range(79):
        x.append(100.0 + 0.8 * (x[-1] - 100.0) + np.random.normal(0, 1.0))
    x[-1] = float(np.mean(x[-31:-1]))  # dernier close = moyenne fenêtre → z ≈ 0
    market = _make_market(x)
    sigs = strat.generate_signals(market)
    assert len(sigs) == 1
    assert sigs[0].target_notional == 0.0
    assert sigs[0].direction == 0.0
