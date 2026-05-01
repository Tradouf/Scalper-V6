from __future__ import annotations

import logging
import time
from typing import Dict, List

import feedparser
import requests

logger = logging.getLogger("sdm.news")


class AgentNewsV2:
    def __init__(self, memory, settings) -> None:
        self.memory = memory
        self.settings = settings
        self.last_payload: Dict = {}

        self.rss_sources = [
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
            "https://cointelegraph.com/rss",
            "https://decrypt.co/feed",
            "https://www.thedefiant.io/feed",
        ]

    def _fetch_rss(self, url: str) -> List[Dict]:
        out: List[Dict] = []
        try:
            resp = requests.get(
                url,
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0 SalleDesMarches/5.0"},
            )
            resp.raise_for_status()
            feed = feedparser.parse(resp.text)

            for entry in getattr(feed, "entries", [])[:20]:
                title = (getattr(entry, "title", "") or "").strip()
                link = (getattr(entry, "link", "") or "").strip()
                summary = (getattr(entry, "summary", "") or "").strip()
                published = (
                    getattr(entry, "published", "")
                    or getattr(entry, "updated", "")
                    or ""
                )
                if title:
                    out.append(
                        {
                            "title": title,
                            "link": link,
                            "summary": summary[:500],
                            "published": published,
                            "source": url,
                        }
                    )
        except Exception as e:
            logger.warning("NewsV2: erreur RSS %s: %r", url, e)
        return out

    def _score_sentiment(self, headlines: List[Dict]) -> Dict:
        bullish_words = {
            "surge", "rally", "breakout", "approval", "adoption", "buy", "bull",
            "record", "growth", "launch", "partnership", "inflow", "up"
        }
        bearish_words = {
            "hack", "drop", "dump", "lawsuit", "ban", "sell", "bear",
            "outflow", "down", "fear", "exploit", "liquidation", "crash"
        }

        bull = 0
        bear = 0

        for h in headlines:
            text = f"{h.get('title', '')} {h.get('summary', '')}".lower()
            bull += sum(1 for w in bullish_words if w in text)
            bear += sum(1 for w in bearish_words if w in text)

        if bull > bear:
            sentiment = "bullish"
        elif bear > bull:
            sentiment = "bearish"
        else:
            sentiment = "neutral"

        return {"sentiment": sentiment, "bull": bull, "bear": bear}

    def _store_news(self, payload: Dict) -> None:
        try:
            if hasattr(self.memory, "update_news") and callable(getattr(self.memory, "update_news")):
                self.memory.update_news(payload)
                return
        except Exception as e:
            logger.warning("NewsV2: erreur update_news mémoire: %r", e)

        for method_name in ("set", "set_value", "update_meta"):
            try:
                method = getattr(self.memory, method_name, None)
                if callable(method):
                    method("news", payload)
                    return
            except Exception:
                pass

        try:
            setattr(self.memory, "news", payload)
        except Exception as e:
            logger.warning("NewsV2: impossible de stocker news: %r", e)

    def analyze(self, symbols: List[str]) -> Dict:
        headlines: List[Dict] = []

        for url in self.rss_sources:
            headlines.extend(self._fetch_rss(url))

        unique = []
        seen = set()
        for h in headlines:
            key = (h.get("title") or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(h)

        unique = unique[:40]
        scored = self._score_sentiment(unique)

        payload = {
            "ts": int(time.time()),
            "symbols": symbols,
            "headlines": unique,
            "sentiment": scored["sentiment"],
            "bull_count": scored["bull"],
            "bear_count": scored["bear"],
        }

        self.last_payload = payload
        self._store_news(payload)

        logger.info(
            "NewsV2: %d headlines, sentiment=%s (bull=%d bear=%d)",
            len(unique),
            scored["sentiment"],
            scored["bull"],
            scored["bear"],
        )

        return payload
