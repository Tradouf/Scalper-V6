"""
Interfaces et types de base pour les exchanges (Hyperliquid & co)
du projet SalleDesMarches.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol


JsonDict = Dict[str, Any]


@dataclass
class OrderRequest:
    """Description d'un ordre à envoyer à un exchange."""

    symbol: str          # ex: "BTC-USDT", "ETH", "BTC-PERP"
    side: str            # "buy" ou "sell"
    qty: float           # taille en unités du sous-jacent
    order_type: str = "limit"   # "limit" ou "market"
    price: Optional[float] = None
    leverage: Optional[float] = None
    reduce_only: bool = False
    client_id: Optional[str] = None  # pour faire le lien avec ta logique interne


@dataclass
class OrderResult:
    """Résultat standardisé après envoi d'un ordre."""

    order_id: str
    symbol: str
    side: str
    qty: float
    price: Optional[float]
    status: str          # "accepted", "rejected", "filled", etc.
    raw: JsonDict        # réponse brute de l'API pour debug


@dataclass
class CancelResult:
    """Résultat standardisé après annulation d'un ordre."""

    order_id: str
    success: bool
    raw: JsonDict


@dataclass
class Position:
    """Position ouverte sur un instrument."""

    symbol: str
    qty: float
    entry_price: float
    leverage: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    raw: JsonDict | None = None


@dataclass
class Balance:
    """Solde d'un asset sur l'exchange."""

    asset: str
    free: float
    locked: float = 0.0
    raw: JsonDict | None = None


class ExchangeClient(Protocol):
    """
    Interface générique pour un exchange dérivés/spot.

    HyperliquidClient devra implémenter cette interface pour être interchangeable
    avec d'éventuels autres exchanges.
    """

    # --- Données de marché (lecture seule) ---

    def get_markets(self) -> List[JsonDict]:
        """
        Retourne la liste des marchés disponibles.

        Pour Hyperliquid, c'est typiquement un mapping des coins/perps avec
        taille de tick, taille de contrat, etc.
        """
        ...

    def get_orderbook(self, symbol: str, depth: int = 50) -> JsonDict:
        """
        Retourne un orderbook (bids/asks) pour un symbole donné.

        Le format précis peut rester proche de l'API sous-jacente,
        mais idéalement documenté dans la docstring du client concret.
        """
        ...

    def get_trades(self, symbol: str, limit: int = 100) -> List[JsonDict]:
        """
        Retourne une liste des derniers trades pour le symbole.
        """
        ...

    # --- Comptes & positions ---

    def get_positions(self) -> List[Position]:
        """
        Retourne la liste des positions ouvertes sur le compte.
        """
        ...

    def get_balances(self) -> List[Balance]:
        """
        Retourne les soldes par asset sur le compte.
        """
        ...

    # --- Trading ---

    def place_order(self, req: OrderRequest) -> OrderResult:
        """
        Place un ordre sur l'exchange.

        Pour Hyperliquid, cette méthode encapsulera la logique de signature
        et appellera l'endpoint /exchange approprié.
        """
        ...

    def cancel_order(self, order_id: str) -> CancelResult:
        """
        Annule un ordre identifié par order_id.
        """
        ...


class PaperExchangeClient(ExchangeClient, Protocol):
    """
    Variante d'interface pour un exchange "papier" (simulation locale).
    Utile pour tester des bots H24 sans toucher au compte réel.
    """

    def reset(self) -> None:
        """Réinitialise l'état de la simulation (positions, ordres, etc.)."""
        ...

