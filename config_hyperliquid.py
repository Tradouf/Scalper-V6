"""
Configuration Hyperliquid pour le projet SalleDesMarches.

Ce module regroupe :
- les URLs API mainnet / testnet,
- le chemin du fichier de config wallet,
- le choix du réseau,
- les frais de base (tier 0) utiles pour les calculs PnL.

NOTE : Le réseau effectif est lu depuis hl_config.json ou la variable
d'environnement HL_NETWORK par exchanges/hyperliquid.py.
Ce fichier ne sert qu'aux modules legacy qui importent ces constantes.
"""

import os

# ============================================================
# Hyperliquid API
# ============================================================

# URL REST de base (réseau principal)
HL_MAINNET_URL = "https://api.hyperliquid.xyz"

# URL REST de base (testnet)
HL_TESTNET_URL = "https://api.hyperliquid-testnet.xyz"

# Fichier de configuration du wallet
HL_WALLET_CONFIG = "hl_config.json"

# Réseau : cohérent avec hl_config.json / env HL_NETWORK
# En production, hl_config.json fait autorité ; cette variable est un fallback.
USE_TESTNET = os.environ.get("HL_NETWORK", "mainnet").lower() == "testnet"

# ============================================================
# Frais Hyperliquid (Tier 0, < $5M 14d volume)
# ============================================================

# Maker: 0.010%
MAKER_FEE = 0.0001

# Taker: 0.035%
TAKER_FEE = 0.00035

