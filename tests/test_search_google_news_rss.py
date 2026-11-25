# -*- coding: utf-8 -*-
"""Unit tests for keyless Google News RSS fallback search provider."""

import unittest
from unittest.mock import MagicMock, patch

from src.search_service import GoogleNewsRSSSearchProvider, SearchService


_SAMPLE_RSS_TW = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Google News</title>
    <item>
      <title>\xe5\x8f\xb0\xe7\xa9\x8d\xe9\x9b\xbb\xe6\xb3\x95\xe8\xaa\xaa\xe6\x9c\x83\xe9\x87\x8b\xe5\x88\xa9\xe5\xa4\x9a - \xe7\xb6\x93\xe6\xbf\x9f\xe6\x97\xa5\xe5\xa0\xb1</title>
      <link>https://news.google.com/rss/articles/tw-1</link>
      <pubDate>Wed, 25 Nov 2026 08:30:00 GMT</pubDate>
      <description>&lt;a href="https://example.com"&gt;\xe5\x8f\xb0\xe7\xa9\x8d\xe9\x9b\xbb\xe7\x87\x9f\xe6\x94\xb6\xe5\x89\xb5\xe6\x96\xb0\xe9\xab\x98&lt;/a&gt;</description>
      <source url="https://money.udn.com">\xe7\xb6\x93\xe6\xbf\x9f\xe6\x97\xa5\xe5\xa0\xb1</source>
    </item>
  </channel>
</rss>
"""

_SAMPLE_RSS_US = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Google News</title>
    <item>
      <title>Nvidia shares rally on AI chip demand - CNBC</title>
      <link>https://news.google.com/rss/articles/us-1</link>
      <pubDate>Tue, 24 Nov 2026 21:15:00 GMT</pubDate>
      <description>&lt;p&gt;Strong datacenter revenue guidance.&lt;/p&gt;</description>
      <source url="https://www.cnbc.com">CNBC</source>
    </item>
  </channel>
</rss>
"""


class TestGoogleNewsRSSSearchProvider(unittest.TestCase):
    @staticmethod
    def _mock_response(status_code: int = 200, content: bytes = b"") -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.content = content
        return resp

    @patch("src.search_service.requests.get")
    def test_cjk_query_uses_zh_tw_locale_and_parses_rss_items(self, mock_get):
        mock_get.return_value = self._mock_response(200, _SAMPLE_RSS_TW)
        provider = GoogleNewsRSSSearchProvider()

        resp = provider.search("台積電 2330", max_results=5, days=3)

        self.assertTrue(resp.success)
        self.assertEqual(resp.provider, "GoogleNewsRSS")
        self.assertEqual(len(resp.results), 1)
        item = resp.results[0]
        self.assertEqual(item.title, "台積電法說會釋利多")
        self.assertEqual(item.source, "經濟日報")
        self.assertEqual(item.published_date, "2026-11-25")
        self.assertEqual(item.snippet, "台積電營收創新高")
        self.assertIsNotNone(item.retrieved_at)

        called_url = mock_get.call_args.args[0]
        self.assertIn("hl=zh-TW", called_url)
        self.assertIn("gl=TW", called_url)
        self.assertIn("when%3A3d", called_url)

    @patch("src.search_service.requests.get")
    def test_english_query_uses_en_us_locale(self, mock_get):
        mock_get.return_value = self._mock_response(200, _SAMPLE_RSS_US)
        provider = GoogleNewsRSSSearchProvider()

        resp = provider.search("NVDA stock", max_results=5, days=7)

        self.assertTrue(resp.success)
        self.assertEqual(len(resp.results), 1)
        self.assertEqual(resp.results[0].title, "Nvidia shares rally on AI chip demand")
        self.assertEqual(resp.results[0].source, "CNBC")
        self.assertEqual(resp.results[0].published_date, "2026-11-24")
        self.assertEqual(resp.results[0].snippet, "Strong datacenter revenue guidance.")

        called_url = mock_get.call_args.args[0]
        self.assertIn("hl=en-US", called_url)
        self.assertIn("gl=US", called_url)
        self.assertIn("when%3A7d", called_url)

    @patch("src.search_service.requests.get")
    def test_http_error_fails_open_without_raising(self, mock_get):
        mock_get.return_value = self._mock_response(503, b"service unavailable")
        provider = GoogleNewsRSSSearchProvider()

        resp = provider.search("NVDA stock", max_results=5, days=3)

        self.assertFalse(resp.success)
        self.assertEqual(resp.results, [])
        self.assertIn("503", resp.error_message or "")

    def test_search_service_registers_google_news_rss_only_when_enabled(self):
        disabled = SearchService()
        self.assertFalse(disabled.is_available)
        self.assertFalse(any(p.name == "GoogleNewsRSS" for p in disabled._providers))

        enabled = SearchService(google_news_rss_enabled=True)
        self.assertTrue(enabled.is_available)
        self.assertEqual([p.name for p in enabled._providers], ["GoogleNewsRSS"])
        self.assertTrue(enabled._constructor_kwargs["google_news_rss_enabled"])

        # API-key providers keep higher priority than Google News RSS
        with_key = SearchService(tavily_keys=["tvly-test"], google_news_rss_enabled=True)
        self.assertEqual([p.name for p in with_key._providers], ["Tavily", "GoogleNewsRSS"])


if __name__ == "__main__":
    unittest.main()
