# -*- coding: utf-8 -*-
"""Offline unit tests for TwInstitutionalFetcher (台股三大法人 data-layer fetcher).

Fixtures are trimmed from real TWSE T86 / TPEx OpenAPI responses (captured
2026-06-26) so the parser is pinned to the actual field layout, date formats,
units and buy/sell-net signs — no network is touched.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.tw_institutional_fetcher import (  # noqa: E402
    TwInstitutionalFetcher,
    minguo_to_ad,
    _to_int,
)

# --- real TWSE T86 row for 2330 台積電 @ 20260626 (西元 date, comma-grouped values) ---
T86_FIXTURE = {
    "stat": "OK",
    "date": "20260626",
    "fields": [
        "證券代號", "證券名稱",
        "外陸資買進股數(不含外資自營商)", "外陸資賣出股數(不含外資自營商)", "外陸資買賣超股數(不含外資自營商)",
        "外資自營商買進股數", "外資自營商賣出股數", "外資自營商買賣超股數",
        "投信買進股數", "投信賣出股數", "投信買賣超股數",
        "自營商買賣超股數",
        "自營商買進股數(自行買賣)", "自營商賣出股數(自行買賣)", "自營商買賣超股數(自行買賣)",
        "自營商買進股數(避險)", "自營商賣出股數(避險)", "自營商買賣超股數(避險)",
        "三大法人買賣超股數",
    ],
    "data": [
        ["2330", "台積電          ", "22,676,018", "36,957,173", "-14,281,155",
         "0", "0", "0", "1,034,258", "299,860", "734,398",
         "1,009,368", "226,100", "769,604", "-543,504",
         "3,424,484", "1,871,612", "1,552,872", "-12,537,389"],
        ["2337", "旺宏            ", "99,038,413", "41,711,072", "57,327,341",
         "0", "0", "0", "345,000", "3,850,000", "-3,505,000",
         "1,914,924", "1,683,980", "1,636,000", "47,980",
         "2,954,044", "1,087,100", "1,866,944", "55,737,265"],
    ],
}

# --- real TPEx OpenAPI row for 3105 穩懋 @ 民國 1150626 (plain ints, messy keys) ---
TPEX_FIXTURE = [
    {
        "Date": "1150626",
        "SecuritiesCompanyCode": "3105",
        "CompanyName": "穩懋",
        "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Total Buy": "11888101",
        " Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Total Sell": "12871054",
        "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference": "-982953",
        "Foreign Dealers-Total Buy": "0",
        "Foreign Dealers-TotalSell": "0",
        "ForeignDealers-Difference": "0",
        "ForeignInvestorsIncludeMainlandAreaInvestors-TotalBuy": "11888101",
        "ForeignInvestorsIncludeMainlandAreaInvestors-TotalSell": "12871054",
        "ForeignInvestorsInclude MainlandAreaInvestors-Difference": "-982953",
        "SecuritiesInvestmentTrustCompanies-TotalBuy": "29737",
        "SecuritiesInvestmentTrustCompanies-TotalSell": "2924000",
        "SecuritiesInvestmentTrustCompanies-Difference": "-2894263",
        "Dealers-TotalBuy": "1228357",
        "Dealers-TotalSell": "1726131",
        "Dealers-Difference": "-497774",
        "Dealers -TotalSell": "853696",
        "TotalDifference": "-4374990",
    },
]


def _resp(json_data):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    return resp


def _fetcher():
    # min_request_interval=0 disables the throttle sleep in tests.
    return TwInstitutionalFetcher(min_request_interval=0)


class TestPureHelpers(unittest.TestCase):
    def test_minguo_to_ad(self):
        self.assertEqual(minguo_to_ad("1150626"), "20260626")  # 民國115 -> 西元2026
        self.assertEqual(minguo_to_ad("1010101"), "20120101")  # 民國101 -> 西元2012
        self.assertEqual(minguo_to_ad("0010101"), "19120101")  # 民國1   -> 西元1912
        self.assertEqual(minguo_to_ad("0990101"), "20100101")  # 民國99  -> 西元2010
        for bad in ("", "115062", "20260626", "abcdefg", None):
            self.assertIsNone(minguo_to_ad(bad), bad)

    def test_to_int_preserves_sign_and_strips_commas(self):
        self.assertEqual(_to_int("22,676,018"), 22676018)
        self.assertEqual(_to_int("-14,281,155"), -14281155)   # sign preserved
        self.assertEqual(_to_int("0"), 0)
        self.assertEqual(_to_int("10178972"), 10178972)        # plain TPEx int
        for blank in ("", "--", "-", "—", None, "n/a"):
            self.assertIsNone(_to_int(blank), blank)


class TestT86Parsing(unittest.TestCase):
    def test_twse_2330_net_breakdown(self):
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp(T86_FIXTURE)):
            rec = _fetcher().get_institutional_net("2330.TW", "20260626")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["stock_code"], "2330")
        self.assertEqual(rec["market"], "上市")
        self.assertEqual(rec["source"], "TWSE-T86")
        self.assertEqual(rec["unit"], "shares")
        self.assertEqual(rec["date"], "20260626")
        # foreign = 外陸資 (不含外資自營商) = -14,281,155
        self.assertEqual(rec["foreign_net"], -14281155)
        self.assertEqual(rec["trust_net"], 734398)
        self.assertEqual(rec["dealer_net"], 1009368)
        self.assertEqual(rec["total_net"], -12537389)
        # the official total equals the component sum (sanity, sign-correct)
        self.assertEqual(rec["total_net"], rec["foreign_net"] + rec["trust_net"] + rec["dealer_net"])

    def test_twse_lowercase_suffix_and_other_row(self):
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp(T86_FIXTURE)):
            rec = _fetcher().get_institutional_net("2337.tw")
        self.assertEqual(rec["stock_code"], "2337")
        self.assertEqual(rec["trust_net"], -3505000)   # negative net preserved

    def test_twse_stat_not_ok_fails_open(self):
        bad = {"stat": "很抱歉，沒有符合條件的資料!", "data": []}
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp(bad)):
            self.assertIsNone(_fetcher().get_institutional_net("2330.TW", "20260101"))

    def test_twse_empty_data_fails_open(self):
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp({"stat": "OK", "data": []})):
            self.assertIsNone(_fetcher().get_institutional_net("2330.TW"))


class TestTpexParsing(unittest.TestCase):
    def test_tpex_3105_net_breakdown_and_minguo_date(self):
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp(TPEX_FIXTURE)):
            rec = _fetcher().get_institutional_net("3105.TWO")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["stock_code"], "3105")
        self.assertEqual(rec["market"], "上櫃")
        self.assertEqual(rec["source"], "TPEx-OpenAPI")
        self.assertEqual(rec["date"], "20260626")        # 民國 1150626 -> 西元
        self.assertEqual(rec["foreign_net"], -982953)
        self.assertEqual(rec["trust_net"], -2894263)
        self.assertEqual(rec["dealer_net"], -497774)
        self.assertEqual(rec["total_net"], -4374990)
        self.assertEqual(rec["total_net"], rec["foreign_net"] + rec["trust_net"] + rec["dealer_net"])

    def test_tpex_non_list_fails_open(self):
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp({})):
            self.assertIsNone(_fetcher().get_institutional_net("6488.TWO"))


class TestRoutingAndFailOpen(unittest.TestCase):
    def test_bare_or_non_tw_code_returns_none_without_fetching(self):
        with patch("data_provider.tw_institutional_fetcher.requests.get") as mock_get:
            f = _fetcher()
            self.assertIsNone(f.get_institutional_net("2330"))     # bare -> not applicable
            self.assertIsNone(f.get_institutional_net("AAPL"))
            self.assertIsNone(f.get_institutional_net("600519.SH"))
            mock_get.assert_not_called()

    def test_network_error_fails_open(self):
        with patch("data_provider.tw_institutional_fetcher.requests.get", side_effect=ConnectionError("boom")):
            self.assertIsNone(_fetcher().get_institutional_net("2330.TW", "20260626"))

    def test_unknown_stock_returns_none(self):
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp(T86_FIXTURE)):
            self.assertIsNone(_fetcher().get_institutional_net("9999.TW"))

    def test_whole_market_cached_single_fetch(self):
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp(T86_FIXTURE)) as mock_get:
            f = _fetcher()
            f.get_institutional_net("2330.TW", "20260626")
            f.get_institutional_net("2337.TW", "20260626")   # same (market, date) -> cache hit
            self.assertEqual(mock_get.call_count, 1)


class TestMissingFieldAndEmptyCacheFailOpen(unittest.TestCase):
    """Dual-review P0/P1 guards: a missing/renamed column must NOT become a
    fabricated 0, and an empty/failed fetch must NOT be cached for the TTL."""

    def test_tpex_missing_core_field_drops_row(self):
        import copy
        row = copy.deepcopy(TPEX_FIXTURE[0])
        del row["SecuritiesInvestmentTrustCompanies-Difference"]   # trust column renamed/missing
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp([row])):
            self.assertIsNone(_fetcher().get_institutional_net("3105.TWO"))

    def test_tpex_genuine_zero_is_kept(self):
        import copy
        row = copy.deepcopy(TPEX_FIXTURE[0])
        row["SecuritiesInvestmentTrustCompanies-Difference"] = "0"  # 投信 truly net-zero today
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp([row])):
            rec = _fetcher().get_institutional_net("3105.TWO")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["trust_net"], 0)       # genuine 0 preserved
        self.assertEqual(rec["foreign_net"], -982953)

    def test_twse_missing_core_field_drops_row(self):
        import copy
        fix = copy.deepcopy(T86_FIXTURE)
        trust_idx = fix["fields"].index("投信買賣超股數")
        fix["data"][0][trust_idx] = ""   # 投信買賣超 cell blank -> missing -> drop 2330, not 0
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp(fix)):
            self.assertIsNone(_fetcher().get_institutional_net("2330.TW", "20260626"))

    def test_empty_result_not_cached_and_retried(self):
        empty = {"stat": "OK", "data": []}
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp(empty)) as mock_get:
            f = _fetcher()
            self.assertIsNone(f.get_institutional_net("2330.TW", "20260626"))
            self.assertIsNone(f.get_institutional_net("2330.TW", "20260626"))
            self.assertEqual(mock_get.call_count, 2)   # empty not cached -> re-fetched


class TestStructureRobustness(unittest.TestCase):
    """Maintainer blockers: T86 read by column NAME (rename/reorder -> fail-open),
    and TPEx date-required (an un-attributable trading day drops the row)."""

    def test_twse_reordered_fields_parsed_by_name(self):
        import copy
        fix = copy.deepcopy(T86_FIXTURE)
        perm = list(range(len(fix["fields"])))[::-1]   # reverse the column order
        fix["fields"] = [fix["fields"][p] for p in perm]
        fix["data"] = [[row[p] for p in perm] for row in fix["data"]]
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp(fix)):
            rec = _fetcher().get_institutional_net("2330.TW", "20260626")
        self.assertIsNotNone(rec)                       # parsed correctly despite reorder
        self.assertEqual(rec["foreign_net"], -14281155)
        self.assertEqual(rec["trust_net"], 734398)
        self.assertEqual(rec["dealer_net"], 1009368)
        self.assertEqual(rec["total_net"], -12537389)

    def test_twse_renamed_core_field_fails_open(self):
        import copy
        fix = copy.deepcopy(T86_FIXTURE)
        fix["fields"][10] = "投信買賣超股數_v2"          # 投信 column renamed
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp(fix)):
            self.assertIsNone(_fetcher().get_institutional_net("2330.TW", "20260626"))

    def test_twse_missing_fields_header_fails_open(self):
        fix = {"stat": "OK", "date": "20260626", "data": [["2330", "x", "1", "2", "3"]]}
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp(fix)):
            self.assertIsNone(_fetcher().get_institutional_net("2330.TW", "20260626"))

    def test_tpex_unconvertible_date_drops_row(self):
        import copy
        row = copy.deepcopy(TPEX_FIXTURE[0])
        row["Date"] = "bad-date"                        # not a 7-digit 民國 -> drop
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp([row])):
            self.assertIsNone(_fetcher().get_institutional_net("3105.TWO"))


class TestConcurrencyAndHttpError(unittest.TestCase):
    """Cache-stampede guard: concurrent same-key callers coalesce into one upstream
    fetch (protects the T86 ~3 req/5 s budget); HTTP errors fail open."""

    def test_concurrent_same_key_coalesces_to_single_fetch(self):
        import threading
        import time as _t
        calls = []
        barrier = threading.Barrier(8)

        def slow_get(*a, **k):
            calls.append(1)
            _t.sleep(0.05)   # window for the other threads to pile up on the key lock
            return _resp(T86_FIXTURE)

        f = _fetcher()

        def caller():
            barrier.wait()   # release all 8 threads together so they truly race
            f.get_institutional_net("2330.TW", "20260626")

        with patch("data_provider.tw_institutional_fetcher.requests.get", side_effect=slow_get):
            threads = [threading.Thread(target=caller) for _ in range(8)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
        self.assertEqual(len(calls), 1)   # 8 concurrent same-key callers -> ONE fetch

    def test_different_keys_are_not_coalesced(self):
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp(T86_FIXTURE)) as mock_get:
            f = _fetcher()
            f.get_institutional_net("2330.TW", "20260626")
            f.get_institutional_net("2330.TW", "20260625")   # different date -> different key
            self.assertEqual(mock_get.call_count, 2)

    def test_http_error_fails_open(self):
        import requests as _rq
        resp = MagicMock()
        resp.raise_for_status.side_effect = _rq.HTTPError("429 Too Many Requests")
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=resp):
            self.assertIsNone(_fetcher().get_institutional_net("2330.TW", "20260626"))


class TestCircuitBreakerAndDateGuard(unittest.TestCase):
    """C1 circuit breaker (skip-fast fail-open when an endpoint is down) + C2 TPEx date guard."""

    def test_circuit_breaker_opens_after_3_failures_and_skips_fetch(self):
        import requests as _rq
        from data_provider.realtime_types import CircuitBreaker

        f = _fetcher()
        with patch(
            "data_provider.tw_institutional_fetcher.requests.get",
            side_effect=_rq.ConnectionError("down"),
        ) as mock_get:
            for _ in range(3):  # 3 consecutive failures trip the breaker
                self.assertIsNone(f.get_institutional_net("2330.TW"))
            self.assertEqual(mock_get.call_count, 3)
            # breaker now OPEN -> the 4th call must skip the network round-trip entirely
            self.assertIsNone(f.get_institutional_net("2330.TW"))
            self.assertEqual(mock_get.call_count, 3, "breaker did not skip the fetch when open")
        self.assertEqual(f._breaker.get_status().get("twse"), CircuitBreaker.OPEN)

    def test_circuit_breaker_recovers_after_cooldown_reset(self):
        import requests as _rq
        from data_provider.realtime_types import CircuitBreaker

        f = _fetcher()
        with patch(
            "data_provider.tw_institutional_fetcher.requests.get",
            side_effect=_rq.ConnectionError("down"),
        ):
            for _ in range(3):
                f.get_institutional_net("2330.TW")
        self.assertEqual(f._breaker.get_status().get("twse"), CircuitBreaker.OPEN)
        f._breaker.reset("twse")  # simulate the ~5 min cooldown elapsing / recovery
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp(T86_FIXTURE)):
            rec = f.get_institutional_net("2330.TW")
        self.assertIsNotNone(rec)
        self.assertEqual(f._breaker.get_status().get("twse"), CircuitBreaker.CLOSED)

    def test_empty_responses_do_not_trip_breaker(self):
        # an empty / non-trading-day response means the endpoint RESPONDED -> reachable,
        # so the breaker must stay CLOSED and keep fetching (never skip a recovered day).
        from data_provider.realtime_types import CircuitBreaker

        f = _fetcher()
        empty = {"stat": "OK", "data": []}
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp(empty)) as mock_get:
            for _ in range(3):
                self.assertIsNone(f.get_institutional_net("2330.TW"))
            self.assertEqual(mock_get.call_count, 3, "an empty response wrongly tripped/skipped the breaker")
        self.assertEqual(f._breaker.get_status().get("twse"), CircuitBreaker.CLOSED)

    def test_tpex_explicit_date_match_returns_record(self):
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp(TPEX_FIXTURE)):
            rec = _fetcher().get_institutional_net("3105.TWO", "20260626")  # == served trading day
        self.assertIsNotNone(rec)
        self.assertEqual(rec["date"], "20260626")

    def test_tpex_explicit_date_mismatch_fails_open(self):
        # TPEx serves only the latest day; a mismatched explicit date must not return a
        # wrong-day record -> fail open (None).
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp(TPEX_FIXTURE)):
            rec = _fetcher().get_institutional_net("3105.TWO", "20260101")
        self.assertIsNone(rec)

    def test_tpex_no_date_returns_latest(self):
        with patch("data_provider.tw_institutional_fetcher.requests.get", return_value=_resp(TPEX_FIXTURE)):
            rec = _fetcher().get_institutional_net("3105.TWO")  # no date -> latest, guard inactive
        self.assertIsNotNone(rec)


MI_INDEX_FIXTURE = {
    "stat": "OK",
    "date": "20261126",
    "tables": [
        {
            "title": "115年11月26日 大盤統計資訊",
            "fields": ["成交統計", "成交金額(元)", "成交股數(股)", "成交筆數"],
            "data": [
                ["1.一般股票", "760,000,000,000", "9,000,000,000", "3,000,000"],
                ["總計(1~15)", "845,336,632,675", "11,760,283,963", "3,717,271"],
            ],
        },
        {
            "title": "漲跌證券數合計",
            "fields": ["類型", "整體市場", "股票"],
            "data": [
                ["上漲(漲停)", "7,814(262)", "971(57)"],
                ["下跌(跌停)", "1,871(99)", "83(3)"],
                ["持平", "413", "32"],
            ],
        },
    ],
}

BFI82U_FIXTURE = {
    "stat": "OK",
    "date": "20261126",
    "fields": ["單位名稱", "買進金額", "賣出金額", "買賣差額"],
    "data": [
        ["自營商(自行買賣)", "20,000,000,000", "15,996,555,879", "4,003,444,121"],
        ["自營商(避險)", "25,000,000,000", "16,138,294,340", "8,861,705,660"],
        ["投信", "35,000,000,000", "13,786,923,381", "21,213,076,619"],
        ["外資及陸資(不含外資自營商)", "100,000,000,000", "149,181,133,625", "-49,181,133,625"],
        ["外資自營商", "0", "0", "0"],
        ["合計", "180,000,000,000", "195,102,907,225", "-15,102,907,225"],
    ],
}


class TestTwMarketSummary(unittest.TestCase):
    """Offline tests for TWSE market-wide breadth, turnover, and institutional flows."""

    def test_get_market_summary_parses_mi_index_and_bfi82u_and_caches(self):
        def _fake_get(url, **_kwargs):
            if "MI_INDEX" in url:
                return _resp(MI_INDEX_FIXTURE)
            if "BFI82U" in url:
                return _resp(BFI82U_FIXTURE)
            raise AssertionError(f"Unexpected url: {url}")

        f = _fetcher()
        with patch("data_provider.tw_institutional_fetcher.requests.get", side_effect=_fake_get) as mock_get:
            summary = f.get_market_summary()
            cached = f.get_market_summary()

        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(summary, cached)
        self.assertEqual(summary["date"], "20261126")
        self.assertEqual(summary["date_iso"], "2026-11-26")
        self.assertEqual(summary["up_count"], 971)
        self.assertEqual(summary["down_count"], 83)
        self.assertEqual(summary["flat_count"], 32)
        self.assertEqual(summary["limit_up_count"], 57)
        self.assertEqual(summary["limit_down_count"], 3)
        self.assertEqual(summary["total_turnover_twd"], 845336632675)
        self.assertEqual(summary["total_amount"], 8453.37)

        inst = summary["institutional"]
        self.assertEqual(inst["source"], "TWSE-BFI82U")
        self.assertEqual(inst["foreign_net_twd"], -49181133625)
        self.assertEqual(inst["trust_net_twd"], 21213076619)
        self.assertEqual(inst["dealer_self_net_twd"], 4003444121)
        self.assertEqual(inst["dealer_hedge_net_twd"], 8861705660)
        self.assertEqual(inst["dealer_net_twd"], 12865149781)
        self.assertEqual(inst["total_net_twd"], -15102907225)

    def test_partial_failure_still_returns_available_endpoint_data(self):
        import requests as _rq

        def _fake_get(url, **_kwargs):
            if "MI_INDEX" in url:
                return _resp(MI_INDEX_FIXTURE)
            raise _rq.ConnectionError("bfi82u down")

        with patch("data_provider.tw_institutional_fetcher.requests.get", side_effect=_fake_get):
            summary = _fetcher().get_market_summary()

        self.assertIsNotNone(summary)
        self.assertEqual(summary["up_count"], 971)
        self.assertEqual(summary["total_amount"], 8453.37)
        self.assertIsNone(summary["institutional"])

    def test_both_endpoints_malformed_or_failed_fail_open_none(self):
        with patch(
            "data_provider.tw_institutional_fetcher.requests.get",
            return_value=_resp({"stat": "ERROR", "data": []}),
        ):
            self.assertIsNone(_fetcher().get_market_summary())

    def test_explicit_date_mismatch_fails_open(self):
        def _fake_get(url, **_kwargs):
            if "MI_INDEX" in url:
                return _resp(MI_INDEX_FIXTURE)
            return _resp(BFI82U_FIXTURE)

        with patch("data_provider.tw_institutional_fetcher.requests.get", side_effect=_fake_get):
            self.assertIsNone(_fetcher().get_market_summary(date="20260101"))


if __name__ == "__main__":
    unittest.main()
