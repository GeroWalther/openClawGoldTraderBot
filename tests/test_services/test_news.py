"""Tests for news sentiment service."""

from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest

from app.services.news import NewsService, INSTRUMENT_RSS_SYMBOLS


RSS_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>Yahoo Finance</title>
{items}
</channel>
</rss>"""


def _make_rss(*headlines: str) -> str:
    """Create a mock RSS XML response."""
    items = "\n".join(
        f"<item><title>{h}</title><pubDate>Tue, 25 Feb 2026 12:00:00 GMT</pubDate></item>"
        for h in headlines
    )
    return RSS_TEMPLATE.format(items=items)


class TestNewsService:
    @pytest.mark.asyncio
    async def test_bullish_headlines_score_positive(self):
        """Multiple bullish headlines should yield positive score."""
        service = NewsService()
        rss = _make_rss(
            "Gold surges to record high",
            "Gold rally continues amid uncertainty",
            "Precious metals soaring on safe-haven demand",
        )

        mock_resp = MagicMock()
        mock_resp.text = rss
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(return_value=mock_resp)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await service.get_news_sentiment("XAUUSD")

        assert result["score"] >= 1
        assert result["bullish_count"] >= 3

    @pytest.mark.asyncio
    async def test_bearish_headlines_score_negative(self):
        """Multiple bearish headlines should yield negative score."""
        service = NewsService()
        rss = _make_rss(
            "Gold crashes below key support",
            "Precious metals plunge on dollar move",
            "Gold selloff deepens amid rate fears",
        )

        mock_resp = MagicMock()
        mock_resp.text = rss
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(return_value=mock_resp)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await service.get_news_sentiment("XAUUSD")

        assert result["score"] <= -1
        assert result["bearish_count"] >= 3

    @pytest.mark.asyncio
    async def test_neutral_headlines_score_zero(self):
        """Neutral headlines should yield score 0."""
        service = NewsService()
        rss = _make_rss(
            "Fed meeting scheduled for next week",
            "Market participants await economic data",
            "Treasury yields hold steady",
        )

        mock_resp = MagicMock()
        mock_resp.text = rss
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(return_value=mock_resp)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await service.get_news_sentiment("XAUUSD")

        assert result["score"] == 0

    @pytest.mark.asyncio
    async def test_strong_bullish_scores_plus_2(self):
        """Net sentiment >= +3 should score +2."""
        service = NewsService()
        rss = _make_rss(
            "Gold surges past $3000",
            "Gold rally accelerates",
            "Gold soaring on geopolitical tensions",
            "Record high for gold prices",
        )

        mock_resp = MagicMock()
        mock_resp.text = rss
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(return_value=mock_resp)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await service.get_news_sentiment("XAUUSD")

        assert result["score"] == 2

    @pytest.mark.asyncio
    async def test_deduplication(self):
        """Same headline from different feeds should be counted once."""
        service = NewsService()
        rss = _make_rss("Gold surges to new high")

        mock_resp = MagicMock()
        mock_resp.text = rss
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(return_value=mock_resp)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await service.get_news_sentiment("XAUUSD")

        # XAUUSD has 2 symbols (GC=F, GLD) but same headline should be counted once
        assert result["bullish_count"] == 1

    @pytest.mark.asyncio
    async def test_caching(self):
        """Second call within TTL should use cache."""
        service = NewsService()
        cached_data = {
            "score": 1, "headlines": [], "bullish_count": 2,
            "bearish_count": 0, "net_sentiment": 2,
        }
        service._cache["XAUUSD"] = (cached_data, __import__("time").monotonic())

        result = await service.get_news_sentiment("XAUUSD")
        assert result["score"] == 1

    @pytest.mark.asyncio
    async def test_unknown_instrument(self):
        """Unknown instrument returns neutral."""
        service = NewsService()
        result = await service.get_news_sentiment("UNKNOWN")
        assert result["score"] == 0

    @pytest.mark.asyncio
    async def test_fetch_failure_graceful(self):
        """RSS fetch failure should not crash."""
        service = NewsService()

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(side_effect=httpx.HTTPError("timeout"))
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await service.get_news_sentiment("XAUUSD")

        assert result["score"] == 0
        assert result["bullish_count"] == 0


class TestInstrumentRssSymbols:
    def test_all_instruments_mapped(self):
        """All instruments should have RSS symbol mappings."""
        from app.instruments import INSTRUMENTS
        for key in INSTRUMENTS:
            assert key in INSTRUMENT_RSS_SYMBOLS, f"Missing RSS mapping for {key}"

    def test_xauusd_symbols(self):
        assert "GC=F" in INSTRUMENT_RSS_SYMBOLS["XAUUSD"]
        assert "GLD" in INSTRUMENT_RSS_SYMBOLS["XAUUSD"]
