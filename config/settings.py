"""
Configuration centrale — SalleDesMarches V6
Live prudent + adaptatif — LocalAI + Hyperliquid mainnet

[FIX v6] Paramètres trailing alignés avec main_v6.py
[FIX v6] Commentaires corrigés pour refléter les vraies valeurs
[FIX v6] HL sync explicitement configuré
[FIX v6.1] Secrets migrés vers variables d'environnement
"""

import os

# ── LocalAI ──────────────────────────────────────────────────────────────────
LOCALAI_BASE_URL = os.environ.get("LOCALAI_BASE_URL", "http://localhost:8080/v1")

MODELS = {
    "orchestrator": "qwen3.5-9b",
    "bull": "qwen2.5-7b-trader",
    "bear": "qwen2.5-7b-trader",
    "scalper": "qwen2.5-7b-trader",
    "technical": "qwen2.5-7b-trader",
    "news": "qwen2.5-7b-trader",
    "whales": "qwen2.5-7b-trader",
    "trader": "qwen2.5-7b-trader",
    "watchlist": "qwen2.5-7b-trader",
}

# ── Timing ───────────────────────────────────────────────────────────────────
SCAN_INTERVAL_SEC = 30
NEWS_REFRESH_SEC = 900
WHALES_REFRESH_SEC = 1200
BULL_BEAR_REFRESH_SEC = 300
HL_SYNC_SEC = 2.0

# ── Trailing natif V6 (ROE = brut × levier) ─────────────────────────────────
# main_v6.py attend TP_ARM_PCT par défaut à 0.0060 (0.60% brut),
# puis un trailing par crans de ROE.
TRAIL_CHECK_SEC = 2
TP_ARM_PCT = 0.005          # 0.8%→0.5% : arme le trail plus tôt, réduit les sorties breakeven
TRAIL_DROP_PCT = 0.0025
TRAIL_STEP_ROE = 0.0015
TRAIL_BREAKEVEN_ROE = 0.000  # 0.10%→0.30% : SL post-arm reste +0.30% ROE au lieu de breakeven, élimine les sorties "TRAIL BREAKEVEN" à -0.05%

# ── Watchlist scalping ───────────────────────────────────────────────────────
SCALP_WATCHLIST = [
    "BTC", "ETH", "SOL", "BNB", "LINK", "HYPE", "ZEC", "APE", "DOGE", "XRP", "TAO", "AAVE","PENDLE",
]

SYMBOLS = SCALP_WATCHLIST

FREEZE_LOOKBACK_TRADES = 5
FREEZE_MIN_TRADES = 3
FREEZE_CONSEC_LOSSES = 2
FREEZE_BAD_COUNT = 3
FREEZE_WINRATE_MAX = 0.34
FREEZE_PNL_SUM_MAX = -0.0040
FREEZE_SEC_SHORT = 3600
FREEZE_SEC_LONG = 4 * 3600

DEFENSIVE_CUT_ENABLED = True
DEFENSIVE_REGIME_CUT_ON_TREND_CHANGE = True
DEFENSIVE_REGIME_CUT_ON_RISK_HIGH = True
DEFENSIVE_CUT_MIN_AGE_SEC = 45
DEFENSIVE_CUT_FLAT_PNL_MAX = 0.0015

# ── Séquencement symboles ────────────────────────────────────────────────────
SYMBOLS_PER_CYCLE = 4

# ── Stratégie scalping ───────────────────────────────────────────────────────
SCALP_TARGET_PCT = 0.003
SCALP_BE_BUFFER_PCT = 0.008
SCALP_TRAILING_ATR_MIN = 0.8
SCALP_TRAILING_ATR_MAX = 1.2
SCALP_MAX_DURATION_MIN = 30
SCALP_TP_PNL_PCT = 0.015
SCALP_SL_PNL_PCT = 0.013

# ── Sécurité capital (in-process, hard rules) ───────────────────────────────
# Si une position dépasse ce multiple du SL_PCT en perte (ROE), on la ferme
# de force par ordre market reduce_only. Garde-fou contre :
# - SL natif perdu / écrasé par positionTpsl singleton
# - Position abandonnée par _recover_or_place_sl (SL passé)
# - Bug réseau / cache stale qui empêche le trail de s'armer
EMERGENCY_LOSS_ROE_MULT = 2.0   # ferme à -2× SL_PCT (= -3% ROE par défaut)

# Si au recovery le prix actuel a déjà dépassé le SL théorique calculé sur
# l'entry, on place un SL serré à current_price ± ce buffer au lieu d'abandonner.
# Verrouille la perte au lieu de laisser la position sans protection.
SL_FALLBACK_BUFFER_PCT = 0.005  # 0.5% au-delà du prix actuel

# Override flip d'urgence : si la position perd plus que SCALP_SL_PNL_PCT et
# que le signal de flip est aligné avec le régime global, on autorise le flip
# au seuil normal (MIN_CONFIDENCE) au lieu de FLIP_MIN_CONFIDENCE.
# Évite le blocage en retournement de tendance clair.
FLIP_EMERGENCY_LOSS_PCT = 0.015  # ROE en-dessous duquel l'override s'active

# ── Filtres pré-LLM ──────────────────────────────────────────────────────────
SCALP_MIN_ATR_PCT = 0.003
SCALP_MIN_SR_DIST = 0.004
MAX_SPREAD_PCT = 0.0008

# ── Seuils de confiance ──────────────────────────────────────────────────────
MIN_CONFIDENCE = 0.70         # 0.72→0.70 (audit 2026-05-06 12:00): 0 ENTER en 6h, confs réelles 0.68-0.71 systématiquement sous seuil 0.72

# ── Filtre volume ────────────────────────────────────────────────────────────
MIN_VOLRATIO = 0.003

# ── Filtre flip (#1 du diagnostic analyze_trades_v2) ─────────────────────────
# Les flips à conf < 0.80 sont structurellement perdants (-19% sur l'échantillon
# 29/04→02/05). On exige une conf élevée pour autoriser un changement de
# direction sur un symbole déjà en position.
FLIP_MIN_CONFIDENCE = 0.90    # 0.95→0.90 (audit 2026-05-05): 37 flip refusé + 2 EMERGENCY EXIT sur 6h, on assouplit légèrement pour permettre les retournements clairs.

# ── Filtre horaire (#2 du diagnostic) ────────────────────────────────────────
# Heures UTC où l'EV historique est négative. Pas d'entrée fraîche ni de flip
# durant ces fenêtres. Le management des positions ouvertes reste actif.
# DÉSACTIVÉ TEMPORAIREMENT (2026-05-03) pour tester l'effet du smart limit
# + nouveau seuil MIN_CONFIDENCE=0.70. Réactiver avec {13, 14, 18, 19, 20, 21, 22}.
BLOCKED_HOURS_UTC: set = set()

# ── Risk management ──────────────────────────────────────────────────────────
MAX_OPEN_POSITIONS = 6
MAX_CONCURRENT_TRADES = 4
CONSECUTIVE_STOPS = 2
SYMBOL_COOLDOWN_MIN = 15
DAILY_LOSS_LIMIT_PCT = 0.03
COOLDOWN_SEC = 180            # 360→180s : cooldown entre entrées sur le même symbole
EXIT_COOLDOWN_SEC = 300       # 600→300s : cooldown post-sortie
FLIP_COOLDOWN_SEC = 300

# ── Sizing ───────────────────────────────────────────────────────────────────
MAX_POSITION_PCT = 0.01
RISK_PER_TRADE_PCT = MAX_POSITION_PCT
MAX_LEVERAGE = 6.0
MAX_NOTIONAL_PCT = 0.15
DEFAULT_LEVERAGE = 3

# Sizing pondéré confiance : qty *= floor + (1 - floor) * (conf - MIN_CONF) / (1 - MIN_CONF)
# À conf=MIN_CONFIDENCE → factor=SIZING_CONF_FLOOR. À conf=1.0 → factor=1.0.
SIZING_CONF_FLOOR = 0.40

# ── Compatibilité V4 ─────────────────────────────────────────────────────────
MAX_RISK_PER_TRADE = 0.008
MAX_DAILY_DRAWDOWN = 0.03
VAR_LIMIT_PCT = 0.015
MAX_CONCENTRATION = 0.25

# ── NEWS / RSS sources ───────────────────────────────────────────────────────
NEWS_URL_COINDESK = "https://www.coindesk.com/arc/outboundfeeds/rss/"
NEWS_URL_COINTELEGRAPH = "https://cointelegraph.com/rss"
NEWS_URL_DECRYPT = "https://decrypt.co/feed"
NEWS_URL_THEBLOCK = "https://www.theblock.co/rss"

WHALES_API_KEY = os.environ.get("WHALES_API_KEY", "")
WHALES_MIN_USD = 500_000

NEWS_MAX_ITEMS = 40
NEWS_HTTP_TIMEOUT = 5.0

# ── Mémoire partagée ─────────────────────────────────────────────────────────
SHARED_MEMORY_FILE = "memory/shared_memory.json"
TRADE_HISTORY_FILE = "memory/trade_history.json"
METRICS_FILE = "memory/metrics_v5.json"

# ── Master switches stratégies ──────────────────────────────────────────────
# Permet de tester/isoler une stratégie. À False, le pipeline correspondant
# ne se lance pas (mais les positions existantes restent monitorées par le
# trail loop + emergency exit). Utile pour évaluer la rentabilité réelle de
# chaque brique séparément.
SCALP_ENABLED = True  # bull/bear/consensus → entrées dirigées via scalper (test grid-only 2026-05-06)
# GRID_ENABLED ci-dessous

# Multi-timeframe gate (architecture "trading floor" — strates H1+M15+M1, veto strict).
# Si True : on exige que les 3 strates LLM s'accordent AVANT de déclencher le
# pipeline bull/bear/scalper. Sinon le gate est transparent (comportement V6).
MULTI_TF_GATE_ENABLED = True

# ── Grid bot (range markets) ─────────────────────────────────────────────────
# Actif quand regime.trend == "range". 1 unité par symbole : buy limit sous le
# marché → quand rempli, sell limit (TP) au-dessus. Cycle auto-renouvelant.
GRID_ENABLED = False
GRID_ATR_FACTOR = 0.7       # spacing = ATR × factor (0.5 = demi-ATR par côté)
GRID_LEVELS = 3              # grille active tant que price dans ±(LEVELS+1)×spacing
GRID_NOTIONAL = 20.0         # USDT par unité de grille (20→30 : +50% profit par cycle)
GRID_LEVERAGE = 3            # levier grille (indépendant du scalp)
GRID_MAX_SYMBOLS = 5         # 3→5 : avec ATR fix, plus de symboles en range éligibles simultanément
GRID_COOLDOWN_SEC = 300      # délai min avant réactivation après désactivation (5 min)
GRID_GRACE_SEC = 8.0         # délai avant 1er tick (laisse le cache HL confirmer l'ordre)
GRID_FORCE_SYMBOLS: list = []  # debug: force la grille sur ces symboles (ignore régime + position)

# ── Smart entry (limit Alo post-only avec fallback market) ──────────────────
# Réduit les frais en visant le côté maker (~+0.01%) au lieu du taker (+0.045%).
# Fallback market si l'ordre limit n'est pas rempli dans le timeout.
LIMIT_FILL_TIMEOUT_SEC = 30       # Délai max d'attente d'un fill limit avant fallback
LIMIT_USE_MIN_CONFIDENCE = 0.70   # Conf min pour tenter limit (sinon market direct)
LIMIT_MAX_SPREAD_PCT = 0.0020     # Spread max pour tenter limit (20 bps — couvre BTC/SOL)
LIMIT_OK_VOLATILITY = ("low", "medium")  # Régimes vol où limit est tenté
LIMIT_POLL_INTERVAL_SEC = 0.5     # Fréquence de check de fill pendant l'attente
LIMIT_STALE_PCT = 0.0030          # Cancel limit si mid s'éloigne de >0.3% du prix posé

# Frais HL pour calcul fees_est dans les logs
HL_MAKER_FEE_PCT = 0.0001         # 0.01% maker (standard, sans rebate VIP)
HL_TAKER_FEE_PCT = 0.00045        # 0.045% taker

# ── Mode simulation ──────────────────────────────────────────────────────────
SIMULATION_MODE = False
RESET_SIM_POSITIONS = False

# ── Divers / compatibilité legacy ────────────────────────────────────────────
DEBUG_SKIPS = True
CYCLE_SEC = SCAN_INTERVAL_SEC
CYCLE_SECONDS = SCAN_INTERVAL_SEC
