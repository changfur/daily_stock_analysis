# -*- coding: utf-8 -*-
"""TwInstitutionalFetcher — Taiwan 三大法人 (institutional-investor) daily net buy/sell.

Data-layer only, ``tw``-only, strictly additive. This module is a self-contained
data-access building block: it fetches, parses, caches and fail-opens. It is NOT
wired into the analysis report / Web / scoring path — that is a deliberate
follow-up (per #1777). It does not touch the existing A-share / HK / US / JP / KR
flows in ``data_provider/base.py``.

Sources (政府開放資料, 政府資料開放授權條款第 1 版 / OGDL v1, commercial-safe, no key):
  - 上市 TWSE T86 「三大法人買賣超日報」 (per-stock), legacy RWD JSON endpoint
    https://www.twse.com.tw/rwd/zh/fund/T86?response=json&date=YYYYMMDD&selectType=ALLBUT0999
    (date is 西元 ``YYYYMMDD``; numeric values are comma-formatted strings)
  - 上櫃 TPEx ``tpex_3insti_daily_trading``, OpenAPI
    https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading
    (date is 民國 ``1150626``; numeric values are plain integer strings)

Fail-open contract: any network error, rate-limit, empty response, unexpected
shape or missing field returns ``None`` (no data) — it never raises into the
caller, so the analysis main flow is never interrupted.

Units are **shares (股)**, not lots (張). Buy/sell-net signs are preserved
(negative = net sell).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Dict, Optional, Tuple

import requests

from data_provider.realtime_types import CircuitBreaker

logger = logging.getLogger(__name__)

_T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
_TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"
_BFI82U_URL = "https://www.twse.com.tw/rwd/zh/fund/BFI82U"
_MI_INDEX_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
_MI_MARGN_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
_TPEX_MARGIN_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance"
_COUNT_WITH_LIMIT_RE = re.compile(r"^\s*([\d,]+)\s*(?:\(\s*([\d,]+)\s*\))?\s*$")
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# TWSE T86 core column NAMES. Read by name (not a fixed index) so a TWSE column
# rename / reorder fails open instead of silently shipping misaligned numbers.
# foreign = 外陸資 (NOT incl 外資自營商): foreign-dealer sits outside the 外資
# category in the official 三大法人 total.
_T86_CODE = "證券代號"
_T86_FOREIGN = "外陸資買賣超股數(不含外資自營商)"
_T86_TRUST = "投信買賣超股數"
_T86_DEALER = "自營商買賣超股數"
_T86_TOTAL = "三大法人買賣超股數"
_T86_CORE = (_T86_CODE, _T86_FOREIGN, _T86_TRUST, _T86_DEALER, _T86_TOTAL)

# TPEx OpenAPI column keys (verified live 2026-06; note the inconsistent spacing in
# the official feed). foreign = dealer-excluded, matching TotalDifference =
# foreign-excl + trust + dealer.
_TPEX_FOREIGN_EXCL = (
    "Foreign Investors include Mainland Area Investors "
    "(Foreign Dealers excluded)-Difference"
)
_TPEX_TRUST = "SecuritiesInvestmentTrustCompanies-Difference"
_TPEX_DEALER = "Dealers-Difference"
_TPEX_TOTAL = "TotalDifference"


def _to_int(value: Any) -> Optional[int]:
    """Parse a TWSE/TPEx numeric cell to int, preserving sign.

    Handles comma grouping (T86) and plain ints (TPEx). Empty / ``--`` / ``-`` /
    non-numeric -> ``None`` (treated as missing, never a fabricated 0).
    """
    try:
        text = str(value).replace(",", "").replace(" ", "").strip()
    except (TypeError, ValueError):
        return None
    if text in ("", "-", "--", "—"):
        return None
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return None


def minguo_to_ad(date_str: Any) -> Optional[str]:
    """Convert a TPEx 民國 date ``YYYMMDD`` (e.g. ``1150626``) to 西元 ``YYYYMMDD``.

    ``1150626`` -> ``20260626`` (民國 115 + 1911 = 西元 2026). Returns ``None`` for
    anything that is not a 7-digit 民國 date, so a format change fails open.
    """
    text = str(date_str).strip()
    if not (text.isdigit() and len(text) == 7):
        return None
    return f"{int(text[:3]) + 1911}{text[3:]}"


def _parse_count_with_limit(value: Any) -> Tuple[Optional[int], Optional[int]]:
    """Parse TWSE MI_INDEX breadth cell like ``'971(57)'`` or ``'32'``."""
    match = _COUNT_WITH_LIMIT_RE.match(str(value or ""))
    if not match:
        return None, None
    total = _to_int(match.group(1))
    limit = _to_int(match.group(2)) if match.group(2) is not None else None
    return total, limit


def _format_ad_date_iso(ad_date: Optional[str]) -> Optional[str]:
    """Format an 8-digit ``YYYYMMDD`` date string as ``YYYY-MM-DD``."""
    if not ad_date or len(ad_date) != 8 or not ad_date.isdigit():
        return None
    return f"{ad_date[:4]}-{ad_date[4:6]}-{ad_date[6:8]}"


class TwInstitutionalFetcher:
    """Fetch Taiwan per-stock 三大法人 net buy/sell, ``.TW`` (上市) / ``.TWO`` (上櫃) only."""

    name = "TwInstitutionalFetcher"

    def __init__(
        self,
        *,
        cache_ttl_seconds: int = 900,
        min_request_interval: float = 1.8,
        timeout: int = 15,
    ) -> None:
        # Whole-market single-day cache keyed by (market, ad_date); filtered per stock.
        self._cache: Dict[Any, Dict[str, dict]] = {}
        self._cache_at: Dict[Any, float] = {}
        self._cache_ttl = cache_ttl_seconds
        self._timeout = timeout
        # TWSE T86 RWD endpoint has an informal ~3 req / 5 s ban; throttle requests.
        self._min_interval = min_request_interval
        self._last_request_at = 0.0
        self._lock = threading.Lock()
        self._throttle_lock = threading.Lock()
        # One lock per unique (market, ad_date) key; bounded by tw markets x
        # distinct dates queried -- low thousands at most, negligible memory.
        self._inflight: Dict[Any, threading.Lock] = {}
        # Per-market circuit breaker (keyed "twse"/"tpex"): when an endpoint is down
        # (>= 3 consecutive failures) skip the network round-trip for ~5 min and fail
        # open, instead of paying timeout + throttle on every stock during an outage.
        # Reuses the repo's CircuitBreaker (same one DataFetcherManager uses).
        self._breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=300.0)

    # ------------------------------------------------------------------ public
    def get_institutional_net(
        self, stock_code: str, date: Optional[str] = None
    ) -> Optional[dict]:
        """Return the normalized 三大法人 record for one TW stock, or ``None``.

        ``stock_code`` must carry an explicit ``.TW`` / ``.TWO`` suffix; a bare or
        non-TW code returns ``None`` (not applicable). ``date`` (西元 ``YYYYMMDD``)
        only applies to 上市/T86; 上櫃/TPEx OpenAPI serves the latest trading day.
        Fail-open: any error returns ``None``.
        """
        market = self._market_of(stock_code)
        if market is None:
            return None
        base = self._base_code(stock_code)
        try:
            table = self._whole_market(market, date)
        except Exception as exc:  # noqa: BLE001 - fail-open by contract
            logger.info(
                "[tw-inst] fetch failed market=%s code=%s: %s", market, stock_code, exc
            )
            return None
        if not table:
            return None
        record = table.get(base)
        # TPEx OpenAPI serves only the LATEST trading day (no date param). If a caller
        # asked for a specific date, never silently return a different-day record --
        # fail open (None) so a date-mismatched 上櫃 figure can't reach a report.
        if record is not None and date and market == "tpex":
            requested = self._norm_ad_date(date)
            if requested and record.get("date") != requested:
                logger.info(
                    "[tw-inst] TPEx %s requested date %s != served %s -> fail-open",
                    base, requested, record.get("date"),
                )
                return None
        return record

    # ------------------------------------------------------------------ routing
    @staticmethod
    def _market_of(stock_code: Any) -> Optional[str]:
        upper = str(stock_code or "").strip().upper()
        if upper.endswith(".TWO"):
            return "tpex"
        if upper.endswith(".TW"):
            return "twse"
        return None

    @staticmethod
    def _base_code(stock_code: Any) -> str:
        return str(stock_code or "").strip().upper().rsplit(".", 1)[0]

    @staticmethod
    def _norm_ad_date(date: Any) -> Optional[str]:
        if not date:
            return None
        text = str(date).strip().replace("-", "").replace("/", "")
        return text if (text.isdigit() and len(text) == 8) else None

    # -------------------------------------------------- whole-market cached fetch
    def _whole_market(self, market: str, date: Optional[str]) -> Dict[str, dict]:
        """Whole-market single-day table {code: record}, cached per (market, date).

        May raise on network / HTTP errors -- the public get_institutional_net wraps
        this in a fail-open try/except. Only non-empty results are cached, so a
        transient rate-limit / empty response is retried on the next call rather
        than serving an empty table for the whole TTL.

        Concurrent callers for the SAME (market, date) coalesce into a single
        upstream fetch (cache-stampede guard) -- this keeps the T86 ~3 req/5 s
        budget intact under parallel callers; different keys still fetch in
        parallel, and the master lock is never held across network I/O. On a fetch
        error the key-lock is released and waiting callers each retry independently
        (serialized only by _throttle), since failures are deliberately not cached.
        """
        ad_date = self._norm_ad_date(date) if market == "twse" else None
        key = (market, ad_date)
        cached = self._read_cache(key)
        if cached is not None:
            return cached
        # Serialize same-key fetches so a burst of callers issues ONE request, not N.
        with self._key_lock(key):
            cached = self._read_cache(key)  # double-check: a prior holder may have filled it
            if cached is not None:
                return cached
            # Circuit breaker: if this endpoint has been failing (>= 3 in a row), skip
            # the network round-trip and fail open (empty) until the ~5 min cooldown
            # half-opens -- so a TWSE/TPEx outage costs ~0 per stock, not timeout+throttle.
            if not self._breaker.is_available(market):
                logger.info("[tw-inst] %s circuit OPEN -> skip fetch, fail-open", market)
                return {}
            try:
                table = self._fetch_twse(ad_date) if market == "twse" else self._fetch_tpex()
            except Exception as exc:  # network / HTTP error -> trip the breaker, then re-raise
                self._breaker.record_failure(market, str(exc))
                raise
            # The breaker tracks REACHABILITY (open only on hard network/HTTP errors).
            # An empty / stat!=OK body still means the endpoint RESPONDED, so it counts
            # as success: it resets the failure streak and, during HALF_OPEN recovery,
            # closes the breaker instead of re-opening it (so a no-data day mid-recovery
            # can never strand the breaker open). Only non-empty tables are cached.
            self._breaker.record_success(market)
            if table:  # never cache an empty / failed fetch -> no TTL-long blackout
                with self._lock:
                    self._cache[key] = table
                    self._cache_at[key] = time.time()
            return table

    def _read_cache(self, key: Any) -> Optional[Dict[str, dict]]:
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and (time.time() - self._cache_at.get(key, 0.0)) < self._cache_ttl:
                return cached
        return None

    def _key_lock(self, key: Any) -> threading.Lock:
        with self._lock:
            lock = self._inflight.get(key)
            if lock is None:
                lock = threading.Lock()
                self._inflight[key] = lock
            return lock

    def _throttle(self) -> None:
        with self._throttle_lock:
            wait = self._min_interval - (time.time() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.time()

    def _get_json(self, url: str, params: Optional[dict] = None) -> Any:
        self._throttle()
        resp = requests.get(
            url,
            params=params,
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------- TWSE T86 (上市)
    def _fetch_twse(self, ad_date: Optional[str]) -> Dict[str, dict]:
        params = {"response": "json", "selectType": "ALLBUT0999"}
        if ad_date:
            params["date"] = ad_date
        payload = self._get_json(_T86_URL, params)
        if not isinstance(payload, dict) or payload.get("stat") != "OK":
            return {}
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows:
            return {}
        idx = self._t86_index_map(payload.get("fields"))
        if idx is None:  # header missing or a core column renamed/removed -> fail-open
            logger.info("[tw-inst] T86 fields header missing/renamed -> fail-open")
            return {}
        payload_date = self._norm_ad_date(payload.get("date")) or ad_date
        table: Dict[str, dict] = {}
        for row in rows:
            record = self._parse_t86_row(row, payload_date, idx)
            if record is not None:
                table[record["stock_code"]] = record
        return table

    @staticmethod
    def _t86_index_map(fields: Any) -> Optional[Dict[str, int]]:
        """Map each core T86 column NAME to its index, or None if any is missing.

        Reading by name (not a fixed index) means a TWSE column rename / reorder
        fails open rather than silently shipping misaligned foreign/trust/dealer
        numbers under stale indices.
        """
        if not isinstance(fields, list):
            return None
        idx: Dict[str, int] = {}
        for name in _T86_CORE:
            try:
                idx[name] = fields.index(name)
            except ValueError:
                return None
        return idx

    @staticmethod
    def _parse_t86_row(
        row: Any, ad_date: Optional[str], idx: Dict[str, int]
    ) -> Optional[dict]:
        if not isinstance(row, (list, tuple)) or any(i >= len(row) for i in idx.values()):
            return None
        if ad_date is None:  # data with no attributable trading date -> fail-open
            return None
        code = str(row[idx[_T86_CODE]]).strip()
        if not code:
            return None
        foreign = _to_int(row[idx[_T86_FOREIGN]])  # 外陸資 (ex 外資自營商)
        trust = _to_int(row[idx[_T86_TRUST]])
        dealer = _to_int(row[idx[_T86_DEALER]])
        total = _to_int(row[idx[_T86_TOTAL]])
        # A None core component means a missing / unparseable column (NOT genuine 0,
        # which parses to 0) -> drop the row so a report never reads a fabricated zero.
        if foreign is None or trust is None or dealer is None:
            return None
        return TwInstitutionalFetcher._build_record(
            code, ad_date, "上市", "TWSE-T86", foreign, trust, dealer, total
        )

    # -------------------------------------------------------------- TPEx (上櫃)
    def _fetch_tpex(self) -> Dict[str, dict]:
        payload = self._get_json(_TPEX_URL)
        if not isinstance(payload, list) or not payload:
            return {}
        table: Dict[str, dict] = {}
        for raw in payload:
            record = self._parse_tpex_row(raw)
            if record is not None:
                table[record["stock_code"]] = record
        return table

    @staticmethod
    def _parse_tpex_row(raw: Any) -> Optional[dict]:
        if not isinstance(raw, dict):
            return None
        code = str(raw.get("SecuritiesCompanyCode", "")).strip()
        if not code:
            return None
        ad_date = minguo_to_ad(raw.get("Date", ""))
        if ad_date is None:  # 民國 date unconvertible -> no attributable day -> fail-open
            return None
        foreign = _to_int(raw.get(_TPEX_FOREIGN_EXCL))  # dealer-excluded foreign
        trust = _to_int(raw.get(_TPEX_TRUST))
        dealer = _to_int(raw.get(_TPEX_DEALER))
        total = _to_int(raw.get(_TPEX_TOTAL))
        # A None core component means a missing / renamed column -> fail-open (never a
        # fabricated 0). Genuine zero activity parses to 0 and is kept.
        if foreign is None or trust is None or dealer is None:
            return None
        return TwInstitutionalFetcher._build_record(
            code, ad_date, "上櫃", "TPEx-OpenAPI", foreign, trust, dealer, total
        )

    # -------------------------------------------------------------- normalize
    @staticmethod
    def _build_record(
        code: str,
        ad_date: Optional[str],
        market_label: str,
        source: str,
        foreign: int,
        trust: int,
        dealer: int,
        total: Optional[int],
    ) -> dict:
        # foreign / trust / dealer are guaranteed non-None by the parsers (a missing
        # component fails the row open upstream), so a genuine 0 is preserved as 0
        # and is never confused with a missing column.
        return {
            "stock_code": code,
            "date": ad_date,
            "market": market_label,
            "source": source,
            "unit": "shares",
            "foreign_net": foreign,
            "trust_net": trust,
            "dealer_net": dealer,
            # Official total when present; otherwise the component sum (kept consistent).
            "total_net": total if total is not None else foreign + trust + dealer,
        }

    # ---------------------------------------- TWSE market-wide summary (大盤)
    def get_market_summary(self, date: Optional[str] = None) -> Optional[dict]:
        """Fetch TWSE mainboard breadth, total turnover, and 三大法人 net buy/sell.

        Combines two keyless official TWSE RWD endpoints:
          - ``MI_INDEX?response=json&type=MS``: listed stock advance/decline/limit
            counts and mainboard total turnover (TWD).
          - ``BFI82U?response=json``: foreign, trust, dealer (prop/hedge), and
            total institutional net buy/sell amounts (TWD).

        Fail-open: each endpoint fails independently; returns ``None`` only when
        neither endpoint produces usable data. Never raises into the caller.
        """
        ad_date = self._norm_ad_date(date)
        key = ("twse_market_summary", ad_date)
        cached = self._read_cache(key)
        if cached is not None:
            return cached

        with self._key_lock(key):
            cached = self._read_cache(key)
            if cached is not None:
                return cached

            mi_data: Optional[dict] = None
            bfi_data: Optional[dict] = None

            if self._breaker.is_available("twse_mi_index"):
                try:
                    mi_data = self._fetch_twse_mi_index_summary(ad_date)
                    self._breaker.record_success("twse_mi_index")
                except Exception as exc:  # noqa: BLE001 - fail-open by contract
                    self._breaker.record_failure("twse_mi_index", str(exc))
                    logger.info("[tw-market] TWSE MI_INDEX fetch failed: %s", exc)
            else:
                logger.info("[tw-market] twse_mi_index circuit OPEN -> skip fetch")

            if self._breaker.is_available("twse_bfi82u"):
                try:
                    bfi_data = self._fetch_twse_bfi82u(ad_date)
                    self._breaker.record_success("twse_bfi82u")
                except Exception as exc:  # noqa: BLE001 - fail-open by contract
                    self._breaker.record_failure("twse_bfi82u", str(exc))
                    logger.info("[tw-market] TWSE BFI82U fetch failed: %s", exc)
            else:
                logger.info("[tw-market] twse_bfi82u circuit OPEN -> skip fetch")

            if not mi_data and not bfi_data:
                return None

            summary_date = (
                (mi_data or {}).get("date")
                or (bfi_data or {}).get("date")
                or ad_date
            )
            summary: Dict[str, Any] = {
                "date": summary_date,
                "date_iso": _format_ad_date_iso(summary_date),
                "up_count": (mi_data or {}).get("up_count", 0),
                "down_count": (mi_data or {}).get("down_count", 0),
                "flat_count": (mi_data or {}).get("flat_count", 0),
                "limit_up_count": (mi_data or {}).get("limit_up_count", 0),
                "limit_down_count": (mi_data or {}).get("limit_down_count", 0),
                "total_turnover_twd": (mi_data or {}).get("total_turnover_twd"),
                "total_amount": (mi_data or {}).get("total_amount", 0.0),
                "institutional": bfi_data,
            }

            with self._lock:
                self._cache[key] = summary
                self._cache_at[key] = time.time()
            return summary

    def _fetch_twse_mi_index_summary(self, ad_date: Optional[str]) -> Optional[dict]:
        """Parse TWSE MI_INDEX (type=MS) for total turnover and listed-stock breadth."""
        params: Dict[str, str] = {"response": "json", "type": "MS"}
        if ad_date:
            params["date"] = ad_date
        payload = self._get_json(_MI_INDEX_URL, params)
        if not isinstance(payload, dict) or payload.get("stat") != "OK":
            return None
        payload_date = self._norm_ad_date(payload.get("date"))
        if ad_date and payload_date and payload_date != ad_date:
            logger.info(
                "[tw-market] TWSE MI_INDEX requested date %s != served %s -> fail-open",
                ad_date,
                payload_date,
            )
            return None

        tables = payload.get("tables")
        if not isinstance(tables, list):
            return None

        result: Dict[str, Any] = {
            "date": payload_date or ad_date,
            "date_iso": _format_ad_date_iso(payload_date or ad_date),
            "source": "TWSE-MI_INDEX",
        }
        parsed_any = False

        for table in tables:
            if not isinstance(table, dict):
                continue
            fields = table.get("fields")
            rows = table.get("data")
            if not isinstance(fields, list) or not isinstance(rows, list):
                continue

            # 1) Mainboard total turnover table ("大盤統計資訊")
            if "成交統計" in fields and "成交金額(元)" in fields:
                stat_idx = fields.index("成交統計")
                amt_idx = fields.index("成交金額(元)")
                for row in rows:
                    if not isinstance(row, (list, tuple)) or max(stat_idx, amt_idx) >= len(row):
                        continue
                    label = str(row[stat_idx] or "").strip()
                    if label.startswith("總計"):
                        turnover_twd = _to_int(row[amt_idx])
                        if turnover_twd is not None and turnover_twd > 0:
                            result["total_turnover_twd"] = turnover_twd
                            result["total_amount"] = round(turnover_twd / 1e8, 2)
                            parsed_any = True
                        break

            # 2) Listed stock breadth table ("漲跌證券數合計")
            if "類型" in fields and "股票" in fields:
                type_idx = fields.index("類型")
                stock_idx = fields.index("股票")
                up_count = down_count = flat_count = None
                limit_up = limit_down = 0
                for row in rows:
                    if not isinstance(row, (list, tuple)) or max(type_idx, stock_idx) >= len(row):
                        continue
                    label = str(row[type_idx] or "").strip()
                    cell = row[stock_idx]
                    if label.startswith("上漲"):
                        cnt, lim = _parse_count_with_limit(cell)
                        if cnt is not None:
                            up_count = cnt
                            limit_up = lim or 0
                    elif label.startswith("下跌"):
                        cnt, lim = _parse_count_with_limit(cell)
                        if cnt is not None:
                            down_count = cnt
                            limit_down = lim or 0
                    elif label.startswith("持平"):
                        cnt, _ = _parse_count_with_limit(cell)
                        if cnt is not None:
                            flat_count = cnt

                if up_count is not None and down_count is not None:
                    result["up_count"] = up_count
                    result["down_count"] = down_count
                    result["flat_count"] = flat_count or 0
                    result["limit_up_count"] = limit_up
                    result["limit_down_count"] = limit_down
                    parsed_any = True

        return result if parsed_any else None

    def _fetch_twse_bfi82u(self, ad_date: Optional[str]) -> Optional[dict]:
        """Parse TWSE BFI82U for market-wide 三大法人 net buy/sell amounts (TWD)."""
        params: Dict[str, str] = {"response": "json"}
        if ad_date:
            params["date"] = ad_date
        payload = self._get_json(_BFI82U_URL, params)
        if not isinstance(payload, dict) or payload.get("stat") != "OK":
            return None
        payload_date = self._norm_ad_date(payload.get("date"))
        if ad_date and payload_date and payload_date != ad_date:
            logger.info(
                "[tw-market] TWSE BFI82U requested date %s != served %s -> fail-open",
                ad_date,
                payload_date,
            )
            return None

        fields = payload.get("fields")
        rows = payload.get("data")
        if not isinstance(fields, list) or not isinstance(rows, list) or not rows:
            return None
        if "單位名稱" not in fields or "買賣差額" not in fields:
            logger.info("[tw-market] TWSE BFI82U fields header missing/renamed -> fail-open")
            return None

        name_idx = fields.index("單位名稱")
        diff_idx = fields.index("買賣差額")

        foreign_excl: Optional[int] = None
        foreign_dealer: int = 0
        trust: Optional[int] = None
        dealer_self: Optional[int] = None
        dealer_hedge: Optional[int] = None
        total: Optional[int] = None

        for row in rows:
            if not isinstance(row, (list, tuple)) or max(name_idx, diff_idx) >= len(row):
                continue
            unit_name = str(row[name_idx] or "").strip()
            net_val = _to_int(row[diff_idx])
            if net_val is None:
                continue
            if unit_name == "投信":
                trust = net_val
            elif unit_name.startswith("外資及陸資"):
                foreign_excl = net_val
            elif unit_name == "外資自營商":
                foreign_dealer = net_val
            elif unit_name == "自營商(自行買賣)":
                dealer_self = net_val
            elif unit_name == "自營商(避險)":
                dealer_hedge = net_val
            elif unit_name == "合計":
                total = net_val

        if foreign_excl is None or trust is None or (dealer_self is None and dealer_hedge is None):
            return None

        foreign_net = foreign_excl + foreign_dealer
        dealer_net = (dealer_self or 0) + (dealer_hedge or 0)
        resolved_total = total if total is not None else (foreign_net + trust + dealer_net)
        resolved_date = payload_date or ad_date

        return {
            "date": resolved_date,
            "date_iso": _format_ad_date_iso(resolved_date),
            "source": "TWSE-BFI82U",
            "unit": "TWD",
            "foreign_net_twd": foreign_net,
            "trust_net_twd": trust,
            "dealer_net_twd": dealer_net,
            "dealer_self_net_twd": dealer_self,
            "dealer_hedge_net_twd": dealer_hedge,
            "total_net_twd": resolved_total,
        }

    # ---------------------------------------- TW per-stock margin trading (融資融券)
    def get_margin_trading(
        self, stock_code: str, date: Optional[str] = None
    ) -> Optional[dict]:
        """Return the normalized 融資融券 record (unit: lots / 張) for one TW stock, or ``None``.

        Supports ``.TW`` (TWSE ``MI_MARGN``) and ``.TWO`` (TPEx OpenAPI
        ``tpex_mainboard_margin_balance``). Whole-market single-day table is cached
        per (market, date) and filtered per stock. Strictly fail-open: any error
        returns ``None``.
        """
        market = self._market_of(stock_code)
        if market is None:
            return None
        base = self._base_code(stock_code)
        try:
            table = self._whole_market_margin(market, date)
        except Exception as exc:  # noqa: BLE001 - fail-open by contract
            logger.info(
                "[tw-margin] fetch failed market=%s code=%s: %s", market, stock_code, exc
            )
            return None
        if not table:
            return None
        record = table.get(base)
        if record is not None and date and market == "tpex":
            requested = self._norm_ad_date(date)
            if requested and record.get("date") != requested:
                logger.info(
                    "[tw-margin] TPEx %s requested date %s != served %s -> fail-open",
                    base,
                    requested,
                    record.get("date"),
                )
                return None
        return record

    def _whole_market_margin(self, market: str, date: Optional[str]) -> Dict[str, dict]:
        ad_date = self._norm_ad_date(date) if market == "twse" else None
        key = (f"{market}_margin", ad_date)
        cached = self._read_cache(key)
        if cached is not None:
            return cached
        breaker_key = f"{market}_margin"
        with self._key_lock(key):
            cached = self._read_cache(key)
            if cached is not None:
                return cached
            if not self._breaker.is_available(breaker_key):
                logger.info("[tw-margin] %s circuit OPEN -> skip fetch, fail-open", breaker_key)
                return {}
            try:
                table = (
                    self._fetch_twse_margin(ad_date)
                    if market == "twse"
                    else self._fetch_tpex_margin()
                )
            except Exception as exc:
                self._breaker.record_failure(breaker_key, str(exc))
                raise
            self._breaker.record_success(breaker_key)
            if table:
                with self._lock:
                    self._cache[key] = table
                    self._cache_at[key] = time.time()
            return table

    def _fetch_twse_margin(self, ad_date: Optional[str]) -> Dict[str, dict]:
        params: Dict[str, str] = {"response": "json", "selectType": "ALL"}
        if ad_date:
            params["date"] = ad_date
        payload = self._get_json(_MI_MARGN_URL, params)
        if not isinstance(payload, dict) or payload.get("stat") != "OK":
            return {}
        payload_date = self._norm_ad_date(payload.get("date"))
        if ad_date and payload_date and payload_date != ad_date:
            logger.info(
                "[tw-margin] TWSE MI_MARGN requested date %s != served %s -> fail-open",
                ad_date,
                payload_date,
            )
            return {}
        resolved_date = payload_date or ad_date
        if not resolved_date:
            return {}
        tables = payload.get("tables")
        if not isinstance(tables, list):
            return {}

        target_table = None
        for table in tables:
            if not isinstance(table, dict):
                continue
            fields = table.get("fields")
            if (
                isinstance(fields, list)
                and "代號" in fields
                and fields.count("前日餘額") >= 2
                and fields.count("今日餘額") >= 2
            ):
                target_table = table
                break
        if target_table is None:
            logger.info("[tw-margin] TWSE MI_MARGN detail table missing/renamed -> fail-open")
            return {}

        fields = target_table["fields"]
        rows = target_table.get("data")
        if not isinstance(rows, list) or not rows:
            return {}

        code_idx = fields.index("代號")
        prev_indices = [i for i, f in enumerate(fields) if f == "前日餘額"]
        curr_indices = [i for i, f in enumerate(fields) if f == "今日餘額"]
        m_prev_idx, s_prev_idx = prev_indices[0], prev_indices[1]
        m_curr_idx, s_curr_idx = curr_indices[0], curr_indices[1]
        offset_idx = fields.index("資券互抵") if "資券互抵" in fields else None
        req_max = max(code_idx, m_prev_idx, m_curr_idx, s_prev_idx, s_curr_idx)

        table_out: Dict[str, dict] = {}
        for row in rows:
            if not isinstance(row, (list, tuple)) or req_max >= len(row):
                continue
            code = str(row[code_idx] or "").strip()
            if not code:
                continue
            m_prev = _to_int(row[m_prev_idx])
            m_curr = _to_int(row[m_curr_idx])
            s_prev = _to_int(row[s_prev_idx])
            s_curr = _to_int(row[s_curr_idx])
            if m_prev is None or m_curr is None or s_prev is None or s_curr is None:
                continue
            offsetting = (
                _to_int(row[offset_idx])
                if offset_idx is not None and offset_idx < len(row)
                else None
            )
            table_out[code] = self._build_margin_record(
                code=code,
                ad_date=resolved_date,
                market_label="上市",
                source="TWSE-MI_MARGN",
                margin_prev=m_prev,
                margin_curr=m_curr,
                short_prev=s_prev,
                short_curr=s_curr,
                offsetting=offsetting,
            )
        return table_out

    def _fetch_tpex_margin(self) -> Dict[str, dict]:
        payload = self._get_json(_TPEX_MARGIN_URL)
        if not isinstance(payload, list) or not payload:
            return {}
        table_out: Dict[str, dict] = {}
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            code = str(raw.get("SecuritiesCompanyCode", "")).strip()
            if not code:
                continue
            ad_date = minguo_to_ad(raw.get("Date", ""))
            if ad_date is None:
                continue
            m_prev = _to_int(raw.get("MarginPurchaseBalancePreviousDay"))
            m_curr = _to_int(raw.get("MarginPurchaseBalance"))
            s_prev = _to_int(raw.get("ShortSaleBalancePreviousDay"))
            s_curr = _to_int(raw.get("ShortSaleBalance"))
            if m_prev is None or m_curr is None or s_prev is None or s_curr is None:
                continue
            offsetting = _to_int(raw.get("Offsetting"))
            table_out[code] = self._build_margin_record(
                code=code,
                ad_date=ad_date,
                market_label="上櫃",
                source="TPEx-OpenAPI",
                margin_prev=m_prev,
                margin_curr=m_curr,
                short_prev=s_prev,
                short_curr=s_curr,
                offsetting=offsetting,
            )
        return table_out

    @staticmethod
    def _build_margin_record(
        *,
        code: str,
        ad_date: str,
        market_label: str,
        source: str,
        margin_prev: int,
        margin_curr: int,
        short_prev: int,
        short_curr: int,
        offsetting: Optional[int] = None,
    ) -> dict:
        ratio = round((short_curr / margin_curr) * 100.0, 2) if margin_curr > 0 else 0.0
        rec: Dict[str, Any] = {
            "stock_code": code,
            "date": ad_date,
            "date_iso": _format_ad_date_iso(ad_date),
            "market": market_label,
            "source": source,
            "unit": "lots",
            "margin_prev_balance": margin_prev,
            "margin_balance": margin_curr,
            "margin_change": margin_curr - margin_prev,
            "short_prev_balance": short_prev,
            "short_balance": short_curr,
            "short_change": short_curr - short_prev,
            "margin_short_ratio_pct": ratio,
        }
        if offsetting is not None:
            rec["offsetting"] = offsetting
        return rec


