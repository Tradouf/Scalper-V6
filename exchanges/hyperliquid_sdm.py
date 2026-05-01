from exchanges.hyperliquid_sdm import HyperliquidSDMClient
from exchanges.base import OrderRequest

if __name__ == "__main__":
    client = HyperliquidSDMClient(
        wallet_key=None,          # ou ta clé privée hex si tu veux forcer
        config_path="hl_config.json",  # ton fichier existant Hydraqueen
        use_testnet=False,        # tu as dit: mainnet only
    )

    print("Markets:", client.get_markets()[:3])
    print("Balances:", client.get_balances())
    print("Positions:", client.get_positions())

    # Exemple d'ordre (NE PAS LANCER sans vérifier la taille/prix)
    """
    req = OrderRequest(
        symbol="BTC",      # coin Hyperliquid
        side="buy",
        qty=0.001,
        order_type="limit",
        price=20000.0,
        leverage=None,
        reduce_only=False,
        client_id="test-sdm-1",
    )
    result = client.place_order(req)
    print("OrderResult:", result)
    """
