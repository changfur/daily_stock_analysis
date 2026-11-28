# -*- coding: utf-8 -*-
"""Offline unit tests for TW/US analysis enhancements:
1. Taiwan ETF & leveraged/inverse ETF detection + skipping empty financials table
2. Taiwan 5-day institutional & margin trend prompt rendering (streak vs mixed)
3. Dual-listed Taiwan ADR premium/discount calculation & prompt rendering (2330.TW <-> TSM)
4. Daily workflow split schedule (US morning + TW afternoon), DB cache, and 90-day pruning contract
"""

from __future__ import annotations

from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd
import yaml

from data_provider.tw_adr_premium import (
    TwAdrPremiumFetcher,
    compute_adr_premium,
    get_adr_pair_for_code,
)
from src.analyzer import GeminiAnalyzer, build_offshore_chip_summary
from src.search_service import SearchService

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _cjk_chars(text: str) -> str:
    return "".join(_CJK_RE.findall(text))


def _make_analyzer() -> GeminiAnalyzer:
    with patch.object(GeminiAnalyzer, "_init_litellm", return_value=None):
        return GeminiAnalyzer(use_legacy_default_prompt=True)


class TestTaiwanEtfDetectionAndPrompt(unittest.TestCase):
    def test_taiwan_etf_and_leveraged_detection(self):
        self.assertTrue(SearchService.is_index_or_etf("0050.TW", "元大台灣50"))
        self.assertTrue(SearchService.is_index_or_etf("00631L.TW", "元大台灣50正2"))
        self.assertTrue(SearchService.is_index_or_etf("00632R.TW", "元大台灣50反1"))
        self.assertTrue(SearchService.is_index_or_etf("00679B.TWO", "元大美債20年"))
        self.assertTrue(SearchService.is_index_or_etf("TQQQ", "ProShares UltraPro QQQ"))
        self.assertFalse(SearchService.is_index_or_etf("2330.TW", "台積電"))
        self.assertFalse(SearchService.is_index_or_etf("2454.TW", "聯發科"))

        self.assertFalse(SearchService.is_leveraged_or_inverse_etf("0050.TW", "元大台灣50"))
        self.assertTrue(SearchService.is_leveraged_or_inverse_etf("00631L.TW", "元大台灣50正2"))
        self.assertTrue(SearchService.is_leveraged_or_inverse_etf("00632R.TW", "元大台灣50反1"))
        self.assertTrue(SearchService.is_leveraged_or_inverse_etf("SOXL", "Direxion Daily Semiconductor Bull 3X"))
        self.assertFalse(SearchService.is_leveraged_or_inverse_etf("2330.TW", "台積電"))

    def test_etf_skips_financials_table_and_leveraged_etf_gets_risk_warning(self):
        analyzer = _make_analyzer()
        fake_cfg = SimpleNamespace(news_max_age_days=3, news_strategy_profile="short")
        base_ctx = {
            "code": "00631L.TW",
            "date": "2026-11-27",
            "today": {"open": 250.0, "high": 255.0, "low": 248.0, "close": 253.0, "volume": 100000},
            "fundamental_context": {"earnings": {"status": "not_supported", "data": {}}},
            "is_index_etf": True,
            "is_leveraged_etf": True,
        }
        with patch("src.analyzer.get_config", return_value=fake_cfg):
            zh_prompt = analyzer._format_prompt(base_ctx, "元大台灣50正2", report_language="zh")
            en_prompt = analyzer._format_prompt(base_ctx, "Yuanta Daily Taiwan 50 Bull 2X", report_language="en")

        self.assertNotIn("财报与分红（价值投资口径）", zh_prompt)
        self.assertIn("杠杆/反向 ETF 特别警示", zh_prompt)
        self.assertIn("波动耗损", zh_prompt)

        self.assertNotIn("Financials and Dividends", en_prompt)
        self.assertIn("Leveraged/Inverse ETF risk warning", en_prompt)
        self.assertEqual(_cjk_chars(en_prompt), "")

        # Regular equity keeps the financials/dividends section and does NOT get leveraged ETF warning.
        stock_ctx = dict(base_ctx, code="2330.TW", is_index_etf=False, is_leveraged_etf=False)
        with patch("src.analyzer.get_config", return_value=fake_cfg):
            stock_zh = analyzer._format_prompt(stock_ctx, "台积电", report_language="zh")
        self.assertIn("财报与分红（价值投资口径）", stock_zh)
        self.assertNotIn("杠杆/反向 ETF 特别警示", stock_zh)


class TestTwFiveDayTrendPromptRendering(unittest.TestCase):
    def test_renders_streak_and_mixed_trends_bilingually(self):
        analyzer = _make_analyzer()
        fake_cfg = SimpleNamespace(news_max_age_days=3, news_strategy_profile="short")
        ctx = {
            "code": "2330.TW",
            "date": "2026-11-27",
            "today": {"close": 1080.0},
            "fundamental_context": {
                "institution": {
                    "status": "ok",
                    "data": {
                        "foreign_net": 12000000,
                        "trust_net": 1500000,
                        "dealer_net": -500000,
                        "total_net": 13000000,
                        "date": "20261127",
                        "source": "TWSE-T86",
                    },
                }
            },
            "tw_institutional_trend": {
                "days": 5,
                "foreign": {"cumulative": 28500000, "positive_days": 4, "negative_days": 1, "streak": 4, "direction": "consecutive_up"},
                "trust": {"cumulative": 1200000, "positive_days": 2, "negative_days": 3, "streak": 1, "direction": "mixed"},
                "total": {"cumulative": 31000000, "positive_days": 4, "negative_days": 1, "streak": 3, "direction": "consecutive_up"},
            },
            "tw_margin_trading": {
                "margin_balance": 28600,
                "margin_change": -250,
                "short_balance": 45,
                "short_change": 5,
                "margin_short_ratio_pct": 0.16,
                "offsetting": 12,
                "date_iso": "2026-11-27",
                "source": "TWSE-MI_MARGN",
            },
            "tw_margin_trend": {
                "days": 5,
                "margin": {"cumulative": -820, "positive_days": 1, "negative_days": 4, "streak": -3, "direction": "consecutive_down"},
                "short": {"cumulative": 12, "positive_days": 3, "negative_days": 2, "streak": 1, "direction": "mixed"},
            },
        }
        with patch("src.analyzer.get_config", return_value=fake_cfg):
            zh_prompt = analyzer._format_prompt(ctx, "TSMC", report_language="zh")
            en_prompt = analyzer._format_prompt(ctx, "TSMC", report_language="en")

        self.assertIn("连买 4 日（近5日累计 +28,500张）", zh_prompt)
        self.assertIn("近5日买2卖3（累计 +1,200张，无连续方向）", zh_prompt)
        self.assertIn("连减 3 日（近5日累计 -820张）", zh_prompt)
        self.assertIn("近5日增3减2（累计 +12张，无连续方向）", zh_prompt)

        self.assertIn("4d consecutive buy (5d cum +28,500 lots)", en_prompt)
        self.assertIn("5d mixed (2 buy / 3 sell, cum +1,200 lots, no consecutive trend)", en_prompt)
        self.assertIn("3d consecutive drop (5d cum -820 lots)", en_prompt)
        self.assertEqual(_cjk_chars(en_prompt), "")

        summary_zh = build_offshore_chip_summary(ctx, language="zh")
        self.assertIsNotNone(summary_zh)
        self.assertIn("外资连买 4 日", summary_zh)


class TestTwAdrPremium(unittest.TestCase):
    def test_pair_lookup_and_math(self):
        pair_tw = get_adr_pair_for_code("2330.tw")
        self.assertIsNotNone(pair_tw)
        self.assertEqual(pair_tw["adr_symbol"], "TSM")
        self.assertEqual(pair_tw["ratio"], 5)
        self.assertEqual(pair_tw["side"], "tw")

        pair_adr = get_adr_pair_for_code("TSM")
        self.assertIsNotNone(pair_adr)
        self.assertEqual(pair_adr["tw_code"], "2330.TW")
        self.assertEqual(pair_adr["side"], "adr")

        self.assertIsNone(get_adr_pair_for_code("NVDA"))

        calc = compute_adr_premium(adr_price_usd=200.0, tw_price_twd=1080.0, usd_twd=32.4, ratio=5)
        self.assertEqual(calc, {"adr_twd_equiv": 1296.0, "premium_pct": 20.0})
        self.assertIsNone(compute_adr_premium(adr_price_usd=-1, tw_price_twd=1000, usd_twd=32, ratio=5))

    def test_fetcher_uses_local_price_and_caches_yfinance_quotes(self):
        idx = pd.DatetimeIndex(["2026-11-26"])
        frames = {
            "TSM": pd.DataFrame({"Close": [200.0]}, index=idx),
            "2330.TW": pd.DataFrame({"Close": [1080.0]}, index=idx),
            "TWD=X": pd.DataFrame({"Close": [32.4]}, index=idx),
        }
        calls = []

        class _FakeTicker:
            def __init__(self, sym):
                self.sym = sym

            def history(self, **_kwargs):
                calls.append(self.sym)
                return frames[self.sym]

        fetcher = TwAdrPremiumFetcher()
        with patch("yfinance.Ticker", side_effect=_FakeTicker):
            res_tw = fetcher.get_adr_premium("2330.TW", local_price=1080.0, local_date="2026-11-27")
            res_adr = fetcher.get_adr_premium("TSM", local_price=200.0, local_date="2026-11-26")

        self.assertIsNotNone(res_tw)
        self.assertEqual(res_tw["adr_twd_equiv"], 1296.0)
        self.assertEqual(res_tw["premium_pct"], 20.0)
        self.assertEqual(res_tw["tw_date"], "2026-11-27")
        self.assertIsNotNone(res_adr)
        self.assertEqual(res_adr["premium_pct"], 20.0)
        # TWD=X fetched only once across both calls
        self.assertEqual(calls.count("TWD=X"), 1)

    def test_adr_prompt_section_renders_bilingually_without_cjk_in_en(self):
        analyzer = _make_analyzer()
        fake_cfg = SimpleNamespace(news_max_age_days=3, news_strategy_profile="short")
        ctx = {
            "code": "2330.TW",
            "date": "2026-11-27",
            "today": {"close": 1080.0},
            "adr_premium": {
                "tw_code": "2330.TW",
                "adr_symbol": "TSM",
                "side": "tw",
                "ratio": 5,
                "adr_price_usd": 200.0,
                "adr_date": "2026-11-26",
                "tw_price_twd": 1080.0,
                "tw_date": "2026-11-27",
                "usd_twd": 32.4,
                "fx_date": "2026-11-27",
                "adr_twd_equiv": 1296.0,
                "premium_pct": 20.0,
                "structural_premium": True,
                "source": "yfinance",
            },
        }
        with patch("src.analyzer.get_config", return_value=fake_cfg):
            zh_prompt = analyzer._format_prompt(ctx, "TSMC", report_language="zh")
            en_prompt = analyzer._format_prompt(ctx, "TSMC", report_language="en")

        self.assertIn("### ADR 折溢价参考（2330.TW vs TSM）", zh_prompt)
        self.assertIn("+20.00%（ADR 溢价）", zh_prompt)
        self.assertIn("结构性正溢价", zh_prompt)

        self.assertIn("### ADR Premium/Discount Reference (2330.TW vs TSM)", en_prompt)
        self.assertIn("+20.00% (ADR premium)", en_prompt)
        self.assertIn("persistent positive premium", en_prompt)
        self.assertEqual(_cjk_chars(en_prompt), "")


class TestDailyWorkflowSplitAndDbCacheContract(unittest.TestCase):
    def test_daily_workflow_has_split_schedules_cache_prune_and_defaults(self):
        wf_path = Path(__file__).resolve().parents[1] / ".github/workflows/00-daily-analysis.yml"
        data = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        # PyYAML parses top-level `on:` as boolean True
        triggers = data.get("on") or data.get(True)
        crons = [item["cron"] for item in triggers["schedule"]]
        self.assertIn("37 22 * * 1-5", crons)
        self.assertIn("7 8 * * 1-5", crons)

        dispatch_inputs = triggers["workflow_dispatch"]["inputs"]
        self.assertIn("market_scope", dispatch_inputs)
        self.assertEqual(dispatch_inputs["market_scope"]["options"], ["all", "tw", "us"])

        steps = data["jobs"]["analyze"]["steps"]
        uses_list = [s.get("uses", "") for s in steps]
        self.assertTrue(any(u.startswith("actions/cache@v4") for u in uses_list))

        analyze_step = next(s for s in steps if s.get("name") == "执行股票分析")
        env = analyze_step["env"]
        self.assertIn("REPORT_HISTORY_COMPARE_N", env)
        self.assertIn("'3'", env["REPORT_HISTORY_COMPARE_N"])
        self.assertIn("CROSS_ASSET_CONTEXT_ENABLED", env)
        self.assertIn("'true'", env["CROSS_ASSET_CONTEXT_ENABLED"])

        prune_step = next(
            (s for s in steps if "清理过期历史数据库记录" in str(s.get("name", ""))),
            None,
        )
        self.assertIsNotNone(prune_step)
        self.assertIn("timedelta(days=90)", prune_step["run"])


if __name__ == "__main__":
    unittest.main()
