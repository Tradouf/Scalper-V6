"""News RSS + 1 appel LLM macro toutes les 15 min."""

from __future__ import annotations

import logging
import time
from typing import Dict, List

import feedparser
import requests

from llmbot import config
from llmbot import llm

logger = logging.getLogger("sdm.llmbot.news")

RSS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
]

SYSTEM = """Tu es un analyste macro crypto. Réponds UNIQUEMENT en JSON valide :
{"sentiment":"bullish"|"bearish"|"neutral","confidence":0.0-1.0,"block_longs":bool,"block_shorts":bool,"summary":"une phrase"}
block_longs=true seulement si news majeure très négative (hack, ban, crash).
block_shorts=true seulement si euphorie extrême risquée."""


class NewsEngine:
    def __init__(self):
        self._last_refresh = 0.0
        self.state: Dict = {
            "sentiment": "neutral",
            "confidence": 0.0,
            "block_longs": False,
            "block_shorts": False,
            "summary": "",
            "headlines": [],
        }

    def _fetch_headlines(self) -> List[str]:
        titles: List[str] = []
        for url in RSS:
            try:
                resp = requests.get(url, timeout=12, headers={"User-Agent": "LLMBot/1.0"})
                feed = feedparser.parse(resp.text)
                for e in feed.entries[:8]:
                    t = (getattr(e, "title", "") or "").strip()
                    if t:
                        titles.append(t)
            except Exception as e:
                logger.debug("RSS %s: %r", url, e)
        return titles[:20]

    def maybe_refresh(self) -> Dict:
        now = time.time()
        if now - self._last_refresh < config.NEWS_REFRESH_SEC:
            return self.state
        self._last_refresh = now
        headlines = self._fetch_headlines()
        self.state["headlines"] = headlines
        if not headlines:
            return self.state

        user = "Titres récents:\n" + "\n".join(f"- {h}" for h in headlines[:15])
        parsed = llm.chat_json(SYSTEM, user, model=config.MODEL_MACRO, temperature=0.1)
        if parsed:
            self.state.update({
                "sentiment": parsed.get("sentiment", "neutral"),
                "confidence": float(parsed.get("confidence", 0) or 0),
                "block_longs": bool(parsed.get("block_longs", False)),
                "block_shorts": bool(parsed.get("block_shorts", False)),
                "summary": str(parsed.get("summary", ""))[:300],
            })
            logger.info(
                "NEWS macro: %s conf=%.2f block_L=%s block_S=%s — %s",
                self.state["sentiment"], self.state["confidence"],
                self.state["block_longs"], self.state["block_shorts"],
                self.state["summary"][:80],
            )
        else:
            logger.warning("NEWS LLM indisponible — état neutre conservé")
        return self.state