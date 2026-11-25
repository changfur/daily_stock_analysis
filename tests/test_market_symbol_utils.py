# -*- coding: utf-8 -*-
"""Tests for shared suffix-only market symbol helpers."""

from src.services.market_symbol_utils import (
    get_suffix_market,
    is_suffix_market_symbol,
    normalize_suffix_market_symbol,
    suffix_base_lookup_allowed,
)


def test_suffix_market_detection_covers_supported_yahoo_markets() -> None:
    assert get_suffix_market("7203.t") == "jp"
    assert get_suffix_market("005930.ks") == "kr"
    assert get_suffix_market("247540.kq") == "kr"
    assert get_suffix_market("2330.tw") == "tw"
    assert get_suffix_market("6505.two") == "tw"
    assert get_suffix_market("00631L.TW") == "tw"
    assert get_suffix_market("00631l.tw") == "tw"
    assert get_suffix_market("00632R.TW") == "tw"
    assert get_suffix_market("00679B.TWO") == "tw"
    assert get_suffix_market("2881A.TW") == "tw"
    assert get_suffix_market("2887Z1.TW") == "tw"

    assert is_suffix_market_symbol("7203.T", "jp") is True
    assert is_suffix_market_symbol("7203.T", "kr") is False
    assert is_suffix_market_symbol("00631L.TW", "tw") is True
    assert normalize_suffix_market_symbol("005930.ks") == "005930.KS"
    assert normalize_suffix_market_symbol("00631l.tw") == "00631L.TW"
    assert normalize_suffix_market_symbol("00679b.two") == "00679B.TWO"


def test_suffix_market_detection_rejects_ambiguous_or_invalid_bare_codes() -> None:
    assert get_suffix_market("005930") is None
    assert get_suffix_market("2330") is None
    assert get_suffix_market("00631L") is None
    assert get_suffix_market("123.T") is None
    assert get_suffix_market("1234567.TW") is None
    assert get_suffix_market("00631LL.TW") is None
    assert get_suffix_market("233L.TW") is None
    assert get_suffix_market("ABCD.TW") is None
    assert get_suffix_market("7203.KS") is None


def test_bare_base_lookup_is_only_allowed_for_jp_kr_mvp() -> None:
    assert suffix_base_lookup_allowed("7203.T") is True
    assert suffix_base_lookup_allowed("005930.KS") is True
    assert suffix_base_lookup_allowed("247540.KQ") is True
    assert suffix_base_lookup_allowed("2330.TW") is False
    assert suffix_base_lookup_allowed("00631L.TW") is False
    assert suffix_base_lookup_allowed("6505.TWO") is False
