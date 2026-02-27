"""News sentiment service — fetches Google News RSS and scores headlines.

Word-boundary keyword matching: bullish vs bearish words.
Thresholds: net ±3 → ±2 score, net ±1 → ±1 score.
"""

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

GOOGLE_NEWS_RSS_URL = (
    "https://news.google.com/rss/search?"
    "q={query}+when:1d&hl=en-US&gl=US&ceid=US:en"
)

# Map instrument keys to Google News search queries
INSTRUMENT_NEWS_QUERIES: dict[str, list[str]] = {
    "XAUUSD": ["gold price", "gold futures"],
    "MES": ["S&P 500", "stock market today"],
    "IBUS500": ["S&P 500", "stock market today"],
    "EURUSD": ["EUR USD forex", "euro dollar"],
    "EURJPY": ["EUR JPY forex"],
    "CADJPY": ["CAD JPY forex"],
    "USDJPY": ["USD JPY forex", "dollar yen"],
    "BTC": ["bitcoin price", "bitcoin BTC"],
}

BULLISH_WORDS = {
    "surge", "surges", "surging", "rally", "rallies", "rallying",
    "soar", "soars", "soaring", "jump", "jumps", "jumping",
    "gain", "gains", "record high", "all-time high", "breakout",
    "bullish", "boom", "booming", "upbeat", "optimism", "optimistic",
    "rise", "rises", "rising", "climb", "climbs", "climbing",
    "strong", "strength", "recovery", "recovering", "rebound",
}

BEARISH_WORDS = {
    "crash", "crashes", "crashing", "plunge", "plunges", "plunging",
    "selloff", "sell-off", "tumble", "tumbles", "tumbling",
    "drop", "drops", "dropping", "fall", "falls", "falling",
    "decline", "declines", "declining", "slump", "slumps",
    "bearish", "collapse", "weak", "weakness", "fear", "panic",
    "loss", "losses", "recession", "crisis", "downturn", "plummet",
}


class NewsService:
    """Fetches Google News RSS headlines and scores sentiment."""

    def __init__(self):
        self._cache: dict[str, tuple[dict, float]] = {}
        self._cache_ttl = 900  # 15 minutes

    async def get_news_sentiment(self, instrument_key: str) -> dict:
        """Return news sentiment score and headline analysis for an instrument.

        Returns:
            dict with score (-2..+2), headlines list, bullish/bearish counts.
        """
        key = instrument_key.upper()
        now = time.monotonic()

        cached = self._cache.get(key)
        if cached:
            data, ts = cached
            if now - ts < self._cache_ttl:
                return data

        queries = INSTRUMENT_NEWS_QUERIES.get(key, [])
        if not queries:
            return {"score": 0, "headlines": [], "bullish_count": 0, "bearish_count": 0}

        all_headlines: list[dict] = []
        seen_titles: set[str] = set()

        for query in queries:
            try:
                headlines = await self._fetch_rss(query)
                for h in headlines:
                    # Deduplicate across query feeds
                    title_lower = h["title"].lower()
                    if title_lower not in seen_titles:
                        seen_titles.add(title_lower)
                        all_headlines.append(h)
            except Exception as e:
                logger.warning("News fetch failed for %s: %s", query, e)

        # Score headlines
        bullish_count = 0
        bearish_count = 0
        scored_headlines = []

        for h in all_headlines[:20]:  # Limit to 20 most recent
            title_lower = h["title"].lower()
            sentiment = "neutral"

            # Word-boundary matching to avoid substring false positives
            # (e.g. "surprising" should not match "rising")
            # Check both bullish and bearish, pick the stronger match
            bull_match = any(
                re.search(r'\b' + re.escape(word) + r'\b', title_lower)
                for word in BULLISH_WORDS
            )
            bear_match = any(
                re.search(r'\b' + re.escape(word) + r'\b', title_lower)
                for word in BEARISH_WORDS
            )

            # If both match, treat as neutral (conflicting signals)
            if bull_match and bear_match:
                sentiment = "neutral"
            elif bear_match:
                bearish_count += 1
                sentiment = "bearish"
            elif bull_match:
                bullish_count += 1
                sentiment = "bullish"

            scored_headlines.append({
                "title": h["title"],
                "published": h.get("published", ""),
                "sentiment": sentiment,
            })

        # Net sentiment to score
        net = bullish_count - bearish_count
        if net >= 3:
            score = 2
        elif net >= 1:
            score = 1
        elif net <= -3:
            score = -2
        elif net <= -1:
            score = -1
        else:
            score = 0

        result = {
            "score": score,
            "headlines": scored_headlines[:10],  # Return top 10
            "bullish_count": bullish_count,
            "bearish_count": bearish_count,
            "net_sentiment": net,
        }

        self._cache[key] = (result, now)
        return result

    async def _fetch_rss(self, query: str) -> list[dict]:
        """Fetch and parse Google News RSS feed for a search query."""
        url = GOOGLE_NEWS_RSS_URL.format(query=query)

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()

        headlines = []
        try:
            root = ET.fromstring(resp.text)
            for item in root.iter("item"):
                title_el = item.find("title")
                pub_el = item.find("pubDate")
                if title_el is not None and title_el.text:
                    headlines.append({
                        "title": title_el.text.strip(),
                        "published": pub_el.text.strip() if pub_el is not None and pub_el.text else "",
                    })
        except ET.ParseError as e:
            logger.warning("RSS XML parse error for %s: %s", query, e)

        return headlines
