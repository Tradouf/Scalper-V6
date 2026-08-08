"""
Parsing des emails de signaux Grok (« BTC Quid is ready »).

Le signal utile tient en une ligne du corps :
    Position recommandée : SHORT BTC-PERP sur Hyperliquid
On en extrait direction + symbole, et en bonus levier / taille suggérés.
Le reste du mail (contexte marché, disclaimers) est ignoré.
"""

from __future__ import annotations

import hashlib
import html
import re
from typing import Optional

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# « Position recommandée : SHORT BTC-PERP » (accents/casse tolérés) ;
# variante anglaise « Recommended position: SHORT BTC-PERP » acceptée.
_DIR_RE = re.compile(
    r"position\s+recommand\S*\s*:\s*(long|short)\s+([a-z0-9]{2,12})\s*[-–]?\s*perp",
    re.IGNORECASE,
)
_DIR_EN_RE = re.compile(
    r"recommended\s+position\s*:\s*(long|short)\s+([a-z0-9]{2,12})\s*[-–]?\s*perp",
    re.IGNORECASE,
)
_LEV_RE = re.compile(r"levier\s*(\d+)\s*x|leverage\s*(\d+)\s*x", re.IGNORECASE)
_SIZE_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[-–]\s*(\d+(?:[.,]\d+)?)\s*%\s*(?:du|of)\s+(?:capital)",
    re.IGNORECASE,
)


def normalize(text: str) -> str:
    """HTML éventuel → texte plat, espaces normalisés."""
    text = html.unescape(text)
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def parse_signal(text: str) -> Optional[dict]:
    """Extrait le signal, ou None si le texte n'en contient pas.

    Retour : {symbol, direction, leverage, size_pct_range, content_hash}
    (leverage / size_pct_range valent None si absents du mail).
    """
    if not text:
        return None
    norm = normalize(text)

    m = _DIR_RE.search(norm) or _DIR_EN_RE.search(norm)
    if not m:
        return None
    direction = m.group(1).upper()
    symbol = m.group(2).upper()

    lev = None
    m_lev = _LEV_RE.search(norm)
    if m_lev:
        lev = int(m_lev.group(1) or m_lev.group(2))

    size_range = None
    m_size = _SIZE_RE.search(norm)
    if m_size:
        size_range = [
            float(m_size.group(1).replace(",", ".")),
            float(m_size.group(2).replace(",", ".")),
        ]

    return {
        "symbol": symbol,
        "direction": direction,
        "leverage": lev,
        "size_pct_range": size_range,
        "content_hash": hashlib.sha1(norm.encode("utf-8")).hexdigest(),
    }
