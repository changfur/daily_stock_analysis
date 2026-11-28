# -*- coding: utf-8 -*-
"""Taiwan ADR vs underlying stock premium/discount calculator.

Computes the implied TWD-equivalent ADR price and percentage premium/discount
for dual-listed Taiwan ADR pairs (e.g. ``2330.TW`` <-> ``TSM`` at 1 ADR = 5 shares):

    adr_twd_equiv = (adr_price_usd * usd_twd_rate) / shares_per_adr
    premium_pct   = ((adr_twd_equiv - tw_price_twd) / tw_price_twd) * 100

Strictly fail-open and cached in-process per symbol/FX rate so a single upstream
error never interrupts the analysis pipeline.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# (tw_code, adr_symbol, shares_per_adr, has_structural_premium)
TW_ADR_PAIRS: Tuple[Tuple[str, str, int, bool], ...] = (
    ("2330.TW", "TSM", 5, True),
    ("2303.TW", "UMC", 5, False),
    ("3711.TW", "ASX", 2, False),
    ("2412.TW", "CHT", 10, False),
)

_PAIR_BY_CODE: Dict[str, Dict[str, Any]] = {}
for _tw_code, _adr_sym, _ratio, _structural in TW_ADR_PAIRS:
    _PAIR_BY_CODE[_tw_code] = {
        "tw_code": _tw_code,
        "adr_symbol": _adr_sym,
        "ratio": _ratio,
        "structural_premium": _structural,
        "side": "tw",
    }
    _PAIR_BY_CODE[_adr_sym] = {
        "tw_code": _tw_code,
        "adr_symbol": _adr_sym,
        "ratio": _ratio,
        "structural_premium": _structural,
        "side": "adr",
    }


def get_adr_pair_for_code(stock_code: Any) -> Optional[Dict[str, Any]]:
    """Return ADR pair metadata if ``stock_code`` belongs to a supported TW-ADR pair."""
    normalized = str(stock_code or "").strip().upper()
    if not normalized:
        return None
    pair = _PAIR_BY_CODE.get(normalized)
    return dict(pair) if pair is not None else None


def compute_adr_premium(
    adr_price_usd: float,
    tw_price_twd: float,
    usd_twd: float,
    ratio: int,
) -> Optional[Dict[str, float]]:
    """Calculate TWD-equivalent ADR price and premium/discount percentage."""
    try:
        adr = float(adr_price_usd)
        tw = float(tw_price_twd)
        fx = float(usd_twd)
        r = int(ratio)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(adr) and math.isfinite(tw) and math.isfinite(fx)):
        return None
    if adr <= 0 or tw <= 0 or fx <= 0 or r <= 0:
        return None
    adr_twd_equiv = (adr * fx) / r
    premium_pct = ((adr_twd_equiv - tw) / tw) * 100.0
    return {
        "adr_twd_equiv": round(adr_twd_equiv, 2),
        "premium_pct": round(premium_pct, 2),
    }


class TwAdrPremiumFetcher:
    """In-process cached fetcher for Taiwan ADR premium/discount."""

    FX_SYMBOL = "TWD=X"

    def __init__(self, *, cache_ttl_seconds: int = 900) -> None:
        self._cache_ttl = cache_ttl_seconds
        self._lock = threading.Lock()
        self._quote_cache: Dict[str, Tuple[float, float, str]] = {}

    def _read_cached_quote(self, symbol: str) -> Optional[Tuple[float, str]]:
        with self._lock:
            entry = self._quote_cache.get(symbol)
            if entry is not None and (time.time() - entry[0]) < self._cache_ttl:
                return entry[1], entry[2]
        return None

    def _fetch_latest_close(self, symbol: str) -> Optional[Tuple[float, str]]:
        cached = self._read_cached_quote(symbol)
        if cached is not None:
            return cached
        try:
            import yfinance as yf

            hist = yf.Ticker(symbol).history(period="5d", auto_adjust=False)
            if hist is None or hist.empty or "Close" not in hist.columns:
                return None
            valid = hist["Close"].dropna()
            if valid.empty:
                return None
            price = float(valid.iloc[-1])
            if not math.isfinite(price) or price <= 0:
                return None
            ts = valid.index[-1]
            date_str = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
            with self._lock:
                self._quote_cache[symbol] = (time.time(), price, date_str)
            return price, date_str
        except Exception as exc:  # noqa: BLE001 - fail-open by contract
            logger.info("[tw-adr] failed to fetch close for %s: %s", symbol, exc)
            return None

    def get_adr_premium(
        self,
        stock_code: str,
        *,
        local_price: Optional[float] = None,
        local_date: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return ADR premium/discount snapshot for supported TW/ADR tickers, else ``None``."""
        pair = get_adr_pair_for_code(stock_code)
        if pair is None:
            return None

        tw_code = pair["tw_code"]
        adr_symbol = pair["adr_symbol"]
        ratio = pair["ratio"]
        side = pair["side"]

        valid_local = False
        parsed_local_price: Optional[float] = None
        if local_price is not None:
            try:
                val = float(local_price)
                if math.isfinite(val) and val > 0:
                    parsed_local_price = val
                    valid_local = True
            except (TypeError, ValueError):
                valid_local = False

        if side == "tw" and valid_local and parsed_local_price is not None and local_date:
            tw_quote: Optional[Tuple[float, str]] = (parsed_local_price, str(local_date)[:10])
        else:
            tw_quote = self._fetch_latest_close(tw_code)

        if side == "adr" and valid_local and parsed_local_price is not None and local_date:
            adr_quote: Optional[Tuple[float, str]] = (parsed_local_price, str(local_date)[:10])
        else:
            adr_quote = self._fetch_latest_close(adr_symbol)

        fx_quote = self._fetch_latest_close(self.FX_SYMBOL)

        if tw_quote is None or adr_quote is None or fx_quote is None:
            return None

        tw_price, tw_date = tw_quote
        adr_price, adr_date = adr_quote
        usd_twd, fx_date = fx_quote

        calc = compute_adr_premium(adr_price, tw_price, usd_twd, ratio)
        if calc is None:
            return None

        return {
            "tw_code": tw_code,
            "adr_symbol": adr_symbol,
            "side": side,
            "ratio": ratio,
            "adr_price_usd": round(adr_price, 2),
            "adr_date": adr_date,
            "tw_price_twd": round(tw_price, 2),
            "tw_date": tw_date,
            "usd_twd": round(usd_twd, 4),
            "fx_date": fx_date,
            "adr_twd_equiv": calc["adr_twd_equiv"],
            "premium_pct": calc["premium_pct"],
            "structural_premium": bool(pair["structural_premium"]),
            "source": "yfinance",
        }
