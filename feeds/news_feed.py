"""
NewsFeed — flux RSS crypto pour enrichir le contexte du stratège (2026-06-14).

Port léger de l'AgentNews V6, MAIS sans LLM propre : ce module ne fait que
COLLECTER et résumer. C'est le stratège Opus qui interprète (il raisonne déjà
au niveau macro). Free, sans clé API (RSS public).

Cache interne : refresh au plus toutes les REFRESH_SEC (les RSS bougent lentement).
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import feedparser

logger = logging.getLogger("v7.news")

RSS_SOURCES = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.theblock.co/rss.xml",
]
REFRESH_SEC = 900          # 15 min
MAX_HEADLINES = 12
RECENT_HOURS = 8           # ne garder que les titres des N dernières heures


class NewsFeed:
    def __init__(self) -> None:
        self._summary = ""
        self._ts = 0.0

    def summary(self) -> str:
        """Résumé compact (titres récents) pour le prompt stratège. Caché."""
        now = time.time()
        if self._summary and now - self._ts < REFRESH_SEC:
            return self._summary
        try:
            self._summary = self._build()
            self._ts = now
        except Exception as e:
            logger.warning("NewsFeed build: %r — garde cache", e)
        return self._summary

    def _build(self) -> str:
        cutoff = time.time() - RECENT_HOURS * 3600
        items: list[tuple[float, str]] = []
        for url in RSS_SOURCES:
            try:
                feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
                for e in feed.entries[:20]:
                    ts = None
                    if getattr(e, "published_parsed", None):
                        ts = time.mktime(e.published_parsed)
                    title = (getattr(e, "title", "") or "").strip()
                    if title and (ts is None or ts >= cutoff):
                        items.append((ts or 0.0, title))
            except Exception as e:
                logger.debug("RSS %s: %r", url, e)
        # dédup + tri récent
        seen, uniq = set(), []
        for ts, t in sorted(items, key=lambda x: -x[0]):
            key = t.lower()[:60]
            if key not in seen:
                seen.add(key); uniq.append(t)
            if len(uniq) >= MAX_HEADLINES:
                break
        if not uniq:
            return "(pas de titres récents récupérés)"
        return "\n".join(f"- {t}" for t in uniq)
