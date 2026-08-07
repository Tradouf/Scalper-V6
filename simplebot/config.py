"""
Configuration SimpleBot — tout est surchargeable par variable d'environnement.
Indépendant de config/settings.py (la V6 et SimpleBot ne partagent rien).

Charge automatiquement le `.env` du repo (sans écraser l'env déjà exporté),
pour que `python -m simplebot.optimizer` se comporte comme `start_simplebot.sh`
et n'écrase pas best_params.json avec le fallback BTC/ETH/SOL.
"""

from __future__ import annotations

import os
from pathlib import Path

# Racine repo (simplebot/ → parent)
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv() -> None:
    """Charge `.env` du repo. N'écrase JAMAIS une variable déjà présente
    (shell, systemd, tests monkeypatch)."""
    env_path = _REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    try:
        raw = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, val = s.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        os.environ[key] = val


_load_dotenv()
# Aligné sur start_simplebot.sh : univers large par défaut en standalone.
os.environ.setdefault("SIMPLEBOT_SYMBOLS", "ALL")


def _resolve_state_dir(env_key: str, default: Path) -> Path:
    """STATE_DIR surchargeable (A/B paper isolé via SIMPLEBOT_STATE_DIR)."""
    raw = (os.environ.get(env_key) or "").strip()
    if raw:
        p = Path(raw).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p
    default.mkdir(parents=True, exist_ok=True)
    return default


# Après dotenv pour que SIMPLEBOT_STATE_DIR du .env soit pris en compte.
STATE_DIR = _resolve_state_dir(
    "SIMPLEBOT_STATE_DIR",
    Path(__file__).resolve().parent / "state",
)
BEST_PARAMS_FILE = STATE_DIR / "best_params.json"
OPTIMIZER_HISTORY_FILE = STATE_DIR / "optimizer_history.jsonl"
LIVE_STATE_FILE = STATE_DIR / "live_state.json"
# Equity paper initiale (dry-run A/B). Live ignore ce flag.
PAPER_START_EQUITY = float(os.environ.get("SIMPLEBOT_PAPER_START_EQUITY", "200") or 200)


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ── Univers ──────────────────────────────────────────────────────────────────
# SIMPLEBOT_SYMBOLS accepte une liste séparée par des virgules OU le mot-clé
# "ALL" → les perps HL les plus liquides, récupérés dynamiquement au démarrage
# (l'univers se met à jour tout seul aux listings/délistings).
#
# En mode ALL, SIMPLEBOT_MAX_SYMBOLS plafonne au top-N par volume notionnel 24h
# (filtre anti-micro-cap : les books trop fins ont un slippage réel ingérable et
# produisent des backtests overfittés). 0 = pas de plafond (tous les non-délistés).
# En cas d'échec réseau, on retombe sur un socle liquide sûr.
_SYMBOLS_FALLBACK = ["BTC", "ETH", "SOL"]
MAX_SYMBOLS = _env_int("SIMPLEBOT_MAX_SYMBOLS", 40)


def _resolve_symbols(raw: str) -> list:
    if raw.strip().upper() != "ALL":
        return [s.strip() for s in raw.split(",") if s.strip()]
    try:
        from simplebot.data import fetch_perp_universe
        names = fetch_perp_universe(top_n=MAX_SYMBOLS or None)
        if names:
            return names
    except Exception as e:  # réseau indispo, endpoint changé…
        import logging
        logging.getLogger("sdm.simplebot.config").warning(
            "Univers ALL indisponible (%r) → fallback %s", e, _SYMBOLS_FALLBACK
        )
    return list(_SYMBOLS_FALLBACK)


# Défaut ALL (comme start_simplebot.sh). Fallback BTC/ETH/SOL seulement si
# la résolution réseau de l'univers échoue (_resolve_symbols).
SYMBOLS = _resolve_symbols(_env_str("SIMPLEBOT_SYMBOLS", "ALL"))
INTERVAL = _env_str("SIMPLEBOT_INTERVAL", "15m")

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}[INTERVAL]

# ── Backtest / optimisation ──────────────────────────────────────────────────
# Attention : l'API candleSnapshot plafonne à ~5000 bougies par requête,
# soit ~52 jours en 15m — ne pas dépasser sans paginer fetch_ohlcv.
BACKTEST_DAYS = _env_int("SIMPLEBOT_BACKTEST_DAYS", 45)
OPTIMIZE_INTERVAL_SEC = _env_int("SIMPLEBOT_OPTIMIZE_INTERVAL_SEC", 3 * 3600)

# Split walk-forward : la grille est classée sur le train ; la validation est
# un filtre BINAIRE (le 1er set du classement train qui confirme est retenu).
VALIDATION_RATIO = _env_float("SIMPLEBOT_VALIDATION_RATIO", 0.30)
TOP_K_VALIDATION = _env_int("SIMPLEBOT_TOP_K_VALIDATION", 10)
MIN_TRAIN_TRADES = _env_int("SIMPLEBOT_MIN_TRAIN_TRADES", 5)
MIN_VALID_TRADES = _env_int("SIMPLEBOT_MIN_VALID_TRADES", 15)
MIN_VALID_PF = _env_float("SIMPLEBOT_MIN_VALID_PF", 1.2)
# Le train doit AUSSI être rentable : un set qui perd en train mais confirme sur
# une fenêtre de validation courte est presque toujours du surapprentissage.
MIN_TRAIN_PF = _env_float("SIMPLEBOT_MIN_TRAIN_PF", 1.0)


def _env_csv(name: str, default: str = "") -> list:
    raw = os.environ.get(name, default)
    if not raw.strip():
        return []
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


# ── Filtre qualité symboles (post-optimiseur, voir symbol_filter.py) ─────────
# Allowlist vide = pas de restriction. Blocklist = exclusion explicite.
SYMBOL_ALLOWLIST = _env_csv("SIMPLEBOT_SYMBOL_ALLOWLIST")
SYMBOL_BLOCKLIST = _env_csv("SIMPLEBOT_SYMBOL_BLOCKLIST")
# 0 = pas de plafond ; sinon ne garde que le top-N par score de validation.
MAX_ACTIVE_SYMBOLS = _env_int("SIMPLEBOT_MAX_ACTIVE_SYMBOLS", 12)
# Seuils qualité post-optimiseur (assouplis juillet 2026 : 1 seul actif avec PF≥1.4).
QUALITY_MIN_VALID_PF = _env_float("SIMPLEBOT_QUALITY_MIN_VALID_PF", 1.30)
QUALITY_MIN_VALID_PNL_PCT = _env_float("SIMPLEBOT_QUALITY_MIN_VALID_PNL_PCT", 0.015)
QUALITY_MIN_VALID_WINRATE = _env_float("SIMPLEBOT_QUALITY_MIN_VALID_WINRATE", 0.38)
QUALITY_MIN_TRAIN_PF = _env_float("SIMPLEBOT_QUALITY_MIN_TRAIN_PF", 1.03)

# Coûts par side (fill taker + glissement) appliqués au backtest ET au sizing.
FEE_PCT = _env_float("SIMPLEBOT_FEE_PCT", 0.00045)
SLIPPAGE_PCT = _env_float("SIMPLEBOT_SLIPPAGE_PCT", 0.0003)

# ── Edge Phase 1 (2026-07) ───────────────────────────────────────────────────
# Filtre de tendance FIXE (hors grille) : +23 % edge/trade en R&D, mais overfit
# s'il est mis dans la grille. 0 = désactivé (rétro-compat tests).
TREND_EMA_FIXED = _env_int("SIMPLEBOT_TREND_EMA_FIXED", 200)
# R:R minimum tp_atr/sl_atr dans la grille. < 1.5 ⇒ expectancy négative à WR
# réaliste 45–55 %. Ex. tp=1.5/sl=4.0 (R:R 0.375) exige WR > 73 % hors frais.
MIN_RR_RATIO = _env_float("SIMPLEBOT_MIN_RR_RATIO", 1.5)
# Gate live : après N closes mesurés, PF live < seuil → symbole désactivé
# jusqu'au prochain reload optimiseur.
LIVE_GATE_MIN_TRADES = _env_int("SIMPLEBOT_LIVE_GATE_MIN_TRADES", 8)
LIVE_GATE_MIN_PF = _env_float("SIMPLEBOT_LIVE_GATE_MIN_PF", 0.9)

# ── Débit API (anti-429) ─────────────────────────────────────────────────────
# Le sweep multi-symboles (jusqu'à MAX_SYMBOLS) enchaîne les requêtes /info et
# peut se faire throttler (HTTP 429). On lisse le débit (pause entre symboles)
# et on réessaie sur 429/erreur réseau avec backoff exponentiel avant d'abandonner.
FETCH_THROTTLE_SEC = _env_float("SIMPLEBOT_FETCH_THROTTLE_SEC", 0.35)
FETCH_MAX_RETRIES = _env_int("SIMPLEBOT_FETCH_MAX_RETRIES", 3)
FETCH_BACKOFF_SEC = _env_float("SIMPLEBOT_FETCH_BACKOFF_SEC", 0.5)

# ── Live ─────────────────────────────────────────────────────────────────────
# DRY-RUN PAR DÉFAUT : le live réel exige SIMPLEBOT_DRY_RUN=0 explicitement.
DRY_RUN = _env_str("SIMPLEBOT_DRY_RUN", "1") not in ("0", "false", "False")

LEVERAGE = _env_int("SIMPLEBOT_LEVERAGE", 3)
MARGIN_PCT = _env_float("SIMPLEBOT_MARGIN_PCT", 0.05)   # marge par trade en % de l'account value
MIN_NOTIONAL_USD = 11.0                                  # minimum HL = $10, marge d'arrondi
# Recommandation : MAX_OPEN_POSITIONS >= min(5, MAX_ACTIVE_SYMBOLS), sinon les
# slots saturent et les signaux des symboles les mieux notés sont censurés.
# (Le tick parcourt les symboles par quality_score décroissant : à slots pleins,
# ce sont les moins bons qui attendent.)
MAX_OPEN_POSITIONS = _env_int("SIMPLEBOT_MAX_OPEN_POSITIONS", 3)
LOOP_SEC = _env_int("SIMPLEBOT_LOOP_SEC", 30)

# ── Sizing dynamique par score de validation (P1) ────────────────────────────
# margin_pct interpolé linéairement entre MARGIN_PCT (pire actif) et
# MARGIN_PCT_MAX (meilleur actif) selon le quality_score normalisé sur les
# symboles actifs. Ex. base 5 % / max 8 % : le top score trade 8 %, le symbole
# limite du cap trade 5 %. SIMPLEBOT_SIZING_DYNAMIC=0 pour revenir au fixe.
SIZING_DYNAMIC = _env_str("SIMPLEBOT_SIZING_DYNAMIC", "1") not in ("0", "false", "False")
MARGIN_PCT_MAX = _env_float("SIMPLEBOT_MARGIN_PCT_MAX", 0.08)

# ── Exécution maker-first (P1) ───────────────────────────────────────────────
# L'edge brut est réel mais les frais taker le mangent (constat MinuteLab).
# Les entrées tentent d'abord un limit post-only (Alo) au mid. Par défaut on
# N'ENVOIE PAS de market en fallback (Phase 1) : un skip vaut mieux qu'un fill
# taker qui détruit l'edge. SIMPLEBOT_EXEC_MARKET_FALLBACK=1 pour l'ancien
# comportement. Compteurs maker/taker/skip persistés dans live_state.json.
EXEC_MAKER_FIRST = _env_str("SIMPLEBOT_EXEC_MAKER_FIRST", "1") not in ("0", "false", "False")
EXEC_MARKET_FALLBACK = _env_str("SIMPLEBOT_EXEC_MARKET_FALLBACK", "0") not in ("0", "false", "False")
EXEC_MAKER_TIMEOUT_SEC = _env_float("SIMPLEBOT_EXEC_MAKER_TIMEOUT_SEC", 30.0)
EXEC_POLL_SEC = _env_float("SIMPLEBOT_EXEC_POLL_SEC", 2.0)
FEE_MAKER_PCT = _env_float("SIMPLEBOT_FEE_MAKER_PCT", 0.00015)   # maker HL base tier

# ── Anti-churn des flips / re-entries (P0 + Phase 1) ─────────────────────────
# Un flip ferme (taker) puis rouvre : 2 flips rapprochés = 4 legs de frais pour
# du bruit. Après un flip sur un symbole, les signaux opposés sont ignorés
# pendant FLIP_COOLDOWN_BARS bougies. Après un close TP/SL/EXCHANGE, les
# nouvelles entrées sont aussi bloquées le même nombre de bougies (anti
# re-chop GRAM/kBONK observé en logs).
FLIP_COOLDOWN_BARS = _env_int("SIMPLEBOT_FLIP_COOLDOWN_BARS", 2)

# Collatéral : sur HL, spot USDC et perp USDC sont séparés. La valeur de compte
# de SimpleBot = perp + spot USDC (sinon un solde logé en spot fait lire 0 au
# perp et déclenche un faux kill-switch). Pour trader, la marge doit être dans le
# perp : si AUTO_FUND_PERP, on vire du spot vers le perp le manque avant d'entrer.
COUNT_SPOT_IN_EQUITY = _env_str("SIMPLEBOT_COUNT_SPOT_IN_EQUITY", "1") not in ("0", "false", "False")
AUTO_FUND_PERP = _env_str("SIMPLEBOT_AUTO_FUND_PERP", "1") not in ("0", "false", "False")
# La somme perp+spot peut sur-compter un résidu perp fantôme (HL renvoie parfois un
# accountValue perp erratique non adossé à du capital réel → fausse equity gonflée,
# et faux kill-switch au reflux du pic fantôme). On plafonne donc la somme par la
# valeur canonique `portfolio` de HL dès qu'elle la dépasse de plus de EQUITY_CANON_TOL.
# Sens unique (baisse) : sûr pour le kill-switch ET le sizing (sous-estimer l'equity
# ne fait que réduire l'exposition). 0 désactive le clamp.
EQUITY_CANON_TOL = _env_float("SIMPLEBOT_EQUITY_CANON_TOL", 0.02)
PERP_FUND_BUFFER = _env_float("SIMPLEBOT_PERP_FUND_BUFFER", 1.5)  # ×marge visée transférée
# Hystérésis kill-switch (P0) : il faut KILL_CONFIRMATIONS lectures CONSÉCUTIVES
# sous le seuil avant de fermer — une lecture isolée aberrante (429, résidu
# fantôme, cohérence différée) ne peut plus tout flatten à elle seule.
KILL_CONFIRMATIONS = _env_int("SIMPLEBOT_KILL_CONFIRMATIONS", 2)
# Log détaillé equity_raw={perp,spot,canon,clamped} au plus toutes les N secondes.
EQUITY_LOG_EVERY_SEC = _env_int("SIMPLEBOT_EQUITY_LOG_EVERY_SEC", 600)

# ── Momentum 4h — stratégie PAPER à paramètres FIGÉS ─────────────────────────
# Seule combinaison ayant survécu à la validation OOS 833j × 31 symboles
# (voir mémoire projet « simplebot-edge-oos ») : suivre le mouvement 48h,
# PAS de take-profit (les TP tuent l'edge), time-exit 12 jours, SL 2×ATR.
# PAS d'optimiseur : la ré-optimisation trailing est empiriquement nocive.
# Paper-only : aucun ordre réel, aucun wallet requis.
MOMENTUM_ENABLED = _env_str("SIMPLEBOT_MOMENTUM", "1") not in ("0", "false", "False")
MOMENTUM_INTERVAL = "4h"
MOMENTUM_INTERVAL_MS = 4 * 3600 * 1000
MOMENTUM_FETCH_DAYS = _env_int("SIMPLEBOT_MOMENTUM_FETCH_DAYS", 14)
MOMENTUM_ROC_BARS = _env_int("SIMPLEBOT_MOMENTUM_ROC_BARS", 12)      # ROC sur 48h
MOMENTUM_THR = _env_float("SIMPLEBOT_MOMENTUM_THR", 0.02)            # seuil ±2%
MOMENTUM_TIME_EXIT_BARS = _env_int("SIMPLEBOT_MOMENTUM_TIME_EXIT_BARS", 72)  # 12 jours
MOMENTUM_SL_ATR = _env_float("SIMPLEBOT_MOMENTUM_SL_ATR", 2.0)
MOMENTUM_ATR_LEN = 14
MOMENTUM_LOOP_SEC = _env_int("SIMPLEBOT_MOMENTUM_LOOP_SEC", 300)
MOMENTUM_PAPER_EQUITY = _env_float("SIMPLEBOT_MOMENTUM_PAPER_EQUITY", 200.0)
MOMENTUM_NOTIONAL_PCT = _env_float("SIMPLEBOT_MOMENTUM_NOTIONAL_PCT", 0.05)  # 5% equity/position
MOMENTUM_MAX_OPEN = _env_int("SIMPLEBOT_MOMENTUM_MAX_OPEN", 0)  # 0 = illimité (le cap 15 saturait : signaux paper censurés en continu)
MOMENTUM_STATE_FILE = STATE_DIR / "momentum_state.json"

# ── Kill-switch ──────────────────────────────────────────────────────────────
# Si l'account value perd KILL_LOSS_PCT par rapport à son pic sur la fenêtre
# glissante KILL_WINDOW_SEC : fermeture de toutes les positions et pause du
# trading pendant KILL_PAUSE_SEC.
KILL_LOSS_PCT = _env_float("SIMPLEBOT_KILL_LOSS_PCT", 0.05)
KILL_WINDOW_SEC = _env_int("SIMPLEBOT_KILL_WINDOW_SEC", 24 * 3600)
KILL_PAUSE_SEC = _env_int("SIMPLEBOT_KILL_PAUSE_SEC", 24 * 3600)
# Fail-safe : si l'account value est illisible KILL_MAX_READ_FAILURES cycles
# consécutifs, on GÈLE les nouvelles entrées (au lieu d'ignorer le check) tant
# que la lecture ne repasse pas. Les positions ouvertes restent protégées par
# leur TP/SL natif sur l'exchange.
KILL_MAX_READ_FAILURES = _env_int("SIMPLEBOT_KILL_MAX_READ_FAILURES", 3)
# Sync périodique des positions HL pour logger les closes TP/SL exchange.
POSITION_SYNC_SEC = _env_int("SIMPLEBOT_POSITION_SYNC_SEC", 120)

# Second wallet — NE PAS réutiliser le wallet de la V6.
ENV_PRIVATE_KEY = "HL2_PRIVATE_KEY"
ENV_ACCOUNT_ADDRESS = "HL2_ACCOUNT_ADDRESS"
