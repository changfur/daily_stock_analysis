# -*- coding: utf-8 -*-
"""Regression tests for Taiwan (台股) suffix-only market support.

Mirrors tests/test_jp_kr_market_support.py (Issue #1718). Taiwan stocks use the
Yahoo Finance suffix forms ``NNNN.TW`` (TWSE-listed) and ``NNNN.TWO`` (TPEx/OTC),
with a 4-6 digit base (wider than JP ``.T``'s 4-5 to cover ETFs like 00878 /
006208). Bare numeric codes keep their existing A-share semantics.
"""

from unittest.mock import patch

import pandas as pd
from data_provider.base import BaseFetcher, DataFetchError, DataFetcherManager, normalize_stock_code
from data_provider.yfinance_fetcher import YfinanceFetcher
from src.core.trading_calendar import MARKET_EXCHANGE, MARKET_TIMEZONE, get_market_for_stock
from src.market_context import detect_market, get_market_guidelines
from src.services.stock_code_utils import is_code_like, normalize_code


class _FakeFetcher(BaseFetcher):
    def __init__(self, name: str, should_fail: bool = False):
        self.name = name
        self.priority = 0 if name != "YfinanceFetcher" else 4
        self.calls = []
        self.should_fail = should_fail

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        raise NotImplementedError

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        raise NotImplementedError

    def get_daily_data(self, stock_code, start_date=None, end_date=None, days=30):
        self.calls.append(stock_code)
        if self.should_fail:
            raise DataFetchError(f"{self.name} should not be called for {stock_code}")
        return pd.DataFrame(
            {
                "date": [pd.Timestamp("2026-06-23")],
                "open": [1.0],
                "high": [1.0],
                "low": [1.0],
                "close": [1.0],
                "volume": [100],
                "amount": [100.0],
                "pct_chg": [0.0],
            }
        )


def test_normalize_and_detect_tw_suffix_codes() -> None:
    assert normalize_stock_code("2330.tw") == "2330.TW"
    assert normalize_stock_code("6505.two") == "6505.TWO"
    assert normalize_stock_code("00878.tw") == "00878.TW"
    assert normalize_stock_code("006208.tw") == "006208.TW"

    assert detect_market("2330.TW") == "tw"
    assert detect_market("0050.TW") == "tw"
    assert detect_market("006208.TW") == "tw"  # 6-digit ETF
    assert detect_market("6505.TWO") == "tw"
    # Bare numeric codes must stay A-share to avoid collision with .TW opt-in.
    assert detect_market("2330") == "cn"

    assert get_market_for_stock("2330.TW") == "tw"
    assert get_market_for_stock("6505.TWO") == "tw"
    # A bare 6-digit code that doubles as a TW ETF (e.g. 006208) must stay
    # A-share without the explicit .TW suffix. (Bare 4-digit codes like "2330"
    # are unrecognized -> None / fail-open, which is existing behavior.)
    assert get_market_for_stock("006208") == "cn"

    assert is_code_like("2330.TW") is True
    assert is_code_like("6505.TWO") is True
    assert normalize_code("6505.TWO") == "6505.TWO"
    assert normalize_code("2330.TW") == "2330.TW"


def test_market_guidelines_for_tw_keep_taiwan_context() -> None:
    tw_guidelines = get_market_guidelines("2330.TW")

    assert "台股" in tw_guidelines
    # Taiwan keeps its own positive framing (±10% limit + three institutional
    # groups); do NOT copy the JP/KR "no A-share price-limit board" wording.
    assert "三大法人" in tw_guidelines
    assert ("±10%" in tw_guidelines) or ("涨跌停" in tw_guidelines)
    # Only China A-share-specific concepts are excluded.
    assert "北向资金" in tw_guidelines
    assert "龙虎榜" in tw_guidelines


def test_yfinance_keeps_tw_suffix_codes_and_indices() -> None:
    fetcher = YfinanceFetcher()

    assert fetcher._convert_stock_code("2330.TW") == "2330.TW"
    assert fetcher._convert_stock_code("6505.TWO") == "6505.TWO"
    assert fetcher._convert_stock_code("006208.TW") == "006208.TW"

    captured = []

    def fake_fetch(_yf, yf_code, name, return_code):
        captured.append((yf_code, name, return_code))
        return {"code": return_code, "name": name, "current": 1.0}

    fetcher._fetch_yf_ticker_data = fake_fetch  # type: ignore[method-assign]

    tw_indices = fetcher.get_main_indices("tw") or []

    assert {item["code"] for item in tw_indices} == {"TWII", "TWOII"}
    assert ("^TWII", "台湾加权指数", "TWII") in captured
    assert ("^TWOII", "台湾柜买指数", "TWOII") in captured


def test_data_fetcher_manager_routes_tw_daily_only_to_yfinance() -> None:
    efinance = _FakeFetcher("EfinanceFetcher", should_fail=True)
    akshare = _FakeFetcher("AkshareFetcher", should_fail=True)
    yfinance = _FakeFetcher("YfinanceFetcher")
    manager = DataFetcherManager(fetchers=[efinance, akshare, yfinance])

    with patch("data_provider.base.record_provider_run_started"), patch("data_provider.base.record_provider_run"):
        tw_df, tw_source = manager.get_daily_data("2330.TW")
        two_df, two_source = manager.get_daily_data("6505.TWO")

    assert tw_source == "YfinanceFetcher"
    assert two_source == "YfinanceFetcher"
    assert not tw_df.empty and not two_df.empty
    assert efinance.calls == []
    assert akshare.calls == []
    assert yfinance.calls == ["2330.TW", "6505.TWO"]


def test_trading_calendar_registers_tw_exchange_and_timezone() -> None:
    assert MARKET_EXCHANGE["tw"] == "XTAI"
    assert MARKET_TIMEZONE["tw"] == "Asia/Taipei"


def test_tw_is_first_class_on_write_paths() -> None:
    """TW is a first-class market on the decision-signal / portfolio / intelligence
    write paths, matching jp/kr.

    Regression guard: the analysis pipeline auto-extracts a DecisionSignal after
    history save (pipeline._extract_decision_signal_after_history_save). If `tw`
    were absent from VALID_MARKETS, _normalize_market("tw") would raise ValueError
    on that main path and the signal would be silently dropped — making tw the
    only yfinance-supported market that can be analyzed but never produces a signal.
    """
    from src.services.decision_signal_service import DecisionSignalService
    from src.services.portfolio_service import VALID_MARKETS
    from src.services.intelligence_service import _ALLOWED_MARKETS

    assert get_market_for_stock("2330.TW") == "tw"  # data layer recognizes tw
    assert DecisionSignalService._normalize_market("tw") == "tw"
    assert "tw" in VALID_MARKETS
    assert "tw" in _ALLOWED_MARKETS


def test_alphanumeric_tw_etf_and_preferred_codes_end_to_end() -> None:
    from api.v1.endpoints.stocks import _validate_stock_code
    from src.agent.stock_scope import extract_stock_code_tokens
    from src.services.stock_code_utils import resolve_daily_stock_identity
    from src.services.stock_list_parser import ParseStatus, parse_analysis_target

    valid_cases = [
        ("00631L.TW", "00631L.TW", "TW"),
        ("00631l.tw", "00631L.TW", "TW"),
        ("00632R.TW", "00632R.TW", "TW"),
        ("00679B.TWO", "00679B.TWO", "TWO"),
        ("00679b.two", "00679B.TWO", "TWO"),
        ("2881A.TW", "2881A.TW", "TW"),
        ("2887Z1.TW", "2887Z1.TW", "TW"),
    ]
    fetcher = YfinanceFetcher()

    for raw, canonical, exchange in valid_cases:
        assert normalize_stock_code(raw) == canonical
        assert detect_market(raw) == "tw"
        assert get_market_for_stock(raw) == "tw"
        assert is_code_like(raw) is True
        assert normalize_code(raw) == canonical
        assert fetcher._convert_stock_code(raw) == canonical
        assert _validate_stock_code(raw) == raw

        identity = resolve_daily_stock_identity(raw)
        assert identity is not None
        assert identity.normalized_code == canonical
        assert identity.market == "tw"

        target = parse_analysis_target(raw)
        assert target.asset_type == ParseStatus.STOCK
        assert target.canonical_id == canonical
        assert target.exchange == exchange

    assert "00631L.TW" in extract_stock_code_tokens("帮我分析 00631L.TW 和 00679B.TWO")
    assert "00679B.TWO" in extract_stock_code_tokens("帮我分析 00631L.TW 和 00679B.TWO")

    efinance = _FakeFetcher("EfinanceFetcher", should_fail=True)
    yfinance = _FakeFetcher("YfinanceFetcher")
    manager = DataFetcherManager(fetchers=[efinance, yfinance])
    with patch("data_provider.base.record_provider_run_started"), patch("data_provider.base.record_provider_run"):
        etf_df, etf_source = manager.get_daily_data("00631L.TW")
    assert etf_source == "YfinanceFetcher"
    assert not etf_df.empty
    assert efinance.calls == []
    assert yfinance.calls == ["00631L.TW"]


def test_tw_main_indices_falls_back_to_tpex_openapi_when_yahoo_twoii_missing() -> None:
    import io
    import json

    fetcher = YfinanceFetcher()

    def fake_fetch(_yf, yf_code, name, return_code):
        if return_code == "TWII":
            return {"code": "TWII", "name": name, "current": 23000.0}
        return None

    fetcher._fetch_yf_ticker_data = fake_fetch  # type: ignore[method-assign]

    tpex_payload = json.dumps([
        {"Date": "20261124", "Open": "276.63", "High": "293.55", "Low": "276.63", "Close": "293.23", "Change": "22.33"},
        {"Date": "20261125", "Open": "290.72", "High": "292.22", "Low": "270.93", "Close": "275.97", "Change": "-17.26"},
    ]).encode("utf-8")

    class _FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    with patch("data_provider.yfinance_fetcher.urlopen", return_value=_FakeResp(tpex_payload)):
        tw_indices = fetcher.get_main_indices("tw") or []

    by_code = {item["code"]: item for item in tw_indices}
    assert set(by_code) == {"TWII", "TWOII"}
    twoii = by_code["TWOII"]
    assert twoii["current"] == 275.97
    assert twoii["change"] == -17.26
    assert round(twoii["prev_close"], 2) == 293.23
    assert round(twoii["change_pct"], 2) == round(-17.26 / 293.23 * 100.0, 2)


def test_get_stock_name_skips_a_share_fetchers_for_tw_stocks() -> None:
    class _NameFetcher(BaseFetcher):
        def __init__(self, name: str, result: str = ""):
            self.name = name
            self.priority = 0 if name != "YfinanceFetcher" else 4
            self.name_calls = []
            self._result = result

        def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
            raise NotImplementedError

        def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
            raise NotImplementedError

        def get_stock_name(self, stock_code: str):
            self.name_calls.append(stock_code)
            return self._result

    pytdx = _NameFetcher("PytdxFetcher", "should-not-be-called")
    baostock = _NameFetcher("BaostockFetcher", "should-not-be-called")
    yfinance = _NameFetcher("YfinanceFetcher", "YC Inox")
    manager = DataFetcherManager(fetchers=[pytdx, baostock, yfinance])

    resolved = manager.get_stock_name("2034.TW", allow_realtime=False)
    assert resolved == "YC Inox"
    assert pytdx.name_calls == []
    assert baostock.name_calls == []
    assert yfinance.name_calls == ["2034.TW"]
