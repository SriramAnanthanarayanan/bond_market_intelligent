# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests>=2.31.0",
#   "beautifulsoup4>=4.12.0",
#   "pandas>=2.1.0",
#   "yfinance>=0.2.28",
#   "fredapi>=0.5.1",
#   "python-dotenv>=1.0.0",
#   "lxml>=4.9.3",
# ]
# ///
"""
Bond Market Intelligence Monitor
Indian retail investor — single-file, manual run.
"""

# ══════════════════════════════════════════════════════════════════════════════
# SECTION A: IMPORTS + LOGGING SETUP
# ══════════════════════════════════════════════════════════════════════════════

import argparse
import csv
import io
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

import requests

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

try:
    import pandas as pd
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import config as cfg

# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging() -> None:
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    log_file = logs_dir / f"run_{date.today().isoformat()}.txt"
    separator_needed = log_file.exists()

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    fmt = logging.Formatter("%(message)s")

    # Force UTF-8 on Windows console (avoids cp1252 UnicodeEncodeError)
    try:
        utf8_stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
        console_handler = logging.StreamHandler(utf8_stdout)
    except AttributeError:
        console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    root.addHandler(console_handler)

    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    if separator_needed:
        logging.info("")
        logging.info("═" * 55)
        logging.info(f"  NEW RUN — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} IST")
        logging.info("═" * 55)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION B: HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class FetchResult:
    value: object
    source: str
    is_manual: bool = False
    is_stale: bool = False
    data_date: date = field(default_factory=date.today)
    fallback_used: bool = False
    extra: dict = field(default_factory=dict)


def _cpi_month_label(offset: int) -> str:
    """offset=0 → latest available CPI month (prev month), 1 → 2 months ago, 2 → 3 months ago."""
    m = date.today().month - 1 - offset
    y = date.today().year
    while m <= 0:
        m += 12
        y -= 1
    return datetime(y, m, 1).strftime("%B %Y")


def _duration_hint(instruments: list) -> str:
    """Derive a maturity-year target range from the suggested ETF names."""
    years = []
    for inst in instruments:
        m = re.search(r"Bharat Bond ETF (\d{4})", inst)
        if m:
            years.append(int(m.group(1)))
    if not years:
        return ""
    today_year = date.today().year
    y_min, y_max = min(years), max(years)
    dur_min = y_min - today_year
    dur_max = y_max - today_year + 2
    return (f"Target: bonds/ETFs maturing {y_min}–{y_max + 2} "
            f"({dur_min}–{dur_max} yrs duration). "
            f"Verify availability — new series may exist beyond these.")


def validate_float(prompt: str, min_val: float, max_val: float) -> float:
    while True:
        raw = input(prompt).strip()
        try:
            val = float(raw)
            if min_val <= val <= max_val:
                return val
            logging.warning(f"  Value {val} out of range [{min_val}, {max_val}]. Try again.")
        except ValueError:
            logging.warning("  Not a number. Try again.")


def validate_int(prompt: str, min_val: int, max_val: int) -> int:
    while True:
        raw = input(prompt).strip()
        try:
            val = int(raw)
            if min_val <= val <= max_val:
                return val
            logging.warning(f"  Value {val} out of range [{min_val}, {max_val}]. Try again.")
        except ValueError:
            logging.warning("  Not an integer. Try again.")


def days_old_label(data_date: date) -> str:
    delta = (date.today() - data_date).days
    if delta == 0:
        return "current (fetched today)"
    return f"{delta} day{'s' if delta != 1 else ''} old  (from {data_date})"


def business_days_ago(df, n_trading_days: int):
    """Return close value approximately n trading days ago from a yfinance DataFrame."""
    if len(df) <= n_trading_days:
        return None
    return float(df["Close"].iloc[-(n_trading_days + 1)])


def read_decision_log_last_row() -> dict:
    log_path = Path("decision_log.csv")
    if not log_path.exists():
        return {}
    try:
        with open(log_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            last = {}
            for row in reader:
                last = row
        return last
    except Exception as e:
        logging.warning(f"  Could not read decision_log.csv: {e}")
        return {}


def read_decision_log_all_rows() -> list:
    log_path = Path("decision_log.csv")
    if not log_path.exists():
        return []
    try:
        with open(log_path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception as e:
        logging.warning(f"  Could not read decision_log.csv: {e}")
        return []


def get_yield_from_log(target_days_ago: int, tolerance_days: int = 4) -> Optional[float]:
    """Find yield_today from a log row closest to target_days_ago calendar days ago."""
    rows = read_decision_log_all_rows()
    if not rows:
        return None
    today = date.today()
    target_date = today - timedelta(days=target_days_ago)
    best_row = None
    best_diff = float("inf")
    for row in rows:
        try:
            row_date = date.fromisoformat(row["run_date"])
            diff = abs((row_date - target_date).days)
            if diff < best_diff and diff <= tolerance_days:
                best_diff = diff
                best_row = row
        except Exception:
            continue
    if best_row is None:
        return None
    try:
        val = float(best_row["yield_today"])
        if cfg.YIELD_MIN_VALID <= val <= cfg.YIELD_MAX_VALID:
            return val
    except Exception:
        pass
    return None


def get_ccil_zero_rate_hint() -> Optional[float]:
    """
    Fetch CCIL zero-coupon 10Y rate as a user reference hint only.
    This is the spot zero rate, NOT the benchmark par yield.
    Par yield is typically 30-60 bps lower — do not use this directly.
    """
    try:
        h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp = safe_request("https://www.ccilindia.com/", headers=h)
        if resp is None:
            return None
        import re as _re, json as _json
        match = _re.search(r'var\s+records\s*=\s*(\[.*?\]);', resp.text, _re.DOTALL)
        if not match:
            return None
        records = _json.loads(match.group(1))
        if not records:
            return None
        latest = max(records, key=lambda r: r.get("_id", 0))
        val = float(latest.get("_zerorate_10", 0))
        if cfg.YIELD_MIN_VALID <= val <= cfg.YIELD_MAX_VALID + 1.5:
            return val
    except Exception:
        pass
    return None


def safe_request(url: str, headers: Optional[dict] = None) -> Optional[requests.Response]:
    """Single wrapper for all HTTP GETs: timeout, logging, None on any failure."""
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp
    except requests.exceptions.Timeout:
        logging.warning(f"  ✗ failed: timeout (15s) — {url}")
        return None
    except requests.exceptions.HTTPError as e:
        logging.warning(f"  ✗ failed: HTTP {e.response.status_code} — {url}")
        return None
    except Exception as e:
        logging.warning(f"  ✗ failed: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# SECTION C: FETCHER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

# Global manual entry counter
manual_entry_count = 0


# ─── DATA 1: 10-Year G-Sec Yield ─────────────────────────────────────────────

# Yahoo Finance symbols for India 10Y — try multiple; availability changes over time
_YFINANCE_YIELD_TICKERS = [
    "^INBMK10Y",   # primary (may be delisted)
    "IN10Y=X",     # Reuters-style fallback
    "INBMK10Y",    # without caret
    "GIND10YR=RR", # Refinitiv format
]


def _extract_yield_from_df(hist) -> Optional[dict]:
    """Shared extraction logic for any yfinance DataFrame."""
    if hist.empty or len(hist) < cfg.YIELD_MIN_ROWS:
        return None
    latest = float(hist["Close"].iloc[-1])
    if not (cfg.YIELD_MIN_VALID <= latest <= cfg.YIELD_MAX_VALID):
        return None
    y30 = business_days_ago(hist, 22)
    y60 = business_days_ago(hist, 43)
    if y30 is None or y60 is None:
        return None
    if not all(cfg.YIELD_MIN_VALID <= v <= cfg.YIELD_MAX_VALID for v in [y30, y60]):
        return None
    latest_date = hist.index[-1]
    if hasattr(latest_date, "date"):
        latest_date = latest_date.date()
    if (date.today() - latest_date).days > 5:
        return None
    return {"yield_today": latest, "yield_30d_ago": y30, "yield_60d_ago": y60}


def _fetch_yield_yfinance(ticker: str) -> Optional[dict]:
    if not YFINANCE_AVAILABLE:
        return None
    logging.info(f"  Trying yfinance ({ticker})...")
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            hist = yf.Ticker(ticker).history(period="70d", interval="1d")
        result = _extract_yield_from_df(hist)
        if result is None:
            logging.info(f"  ✗ failed: only {len(hist)} rows or values out of range")
        else:
            logging.info("  ✓ succeeded")
        return result
    except Exception as e:
        logging.warning(f"  ✗ failed: {e}")
        return None


def _fetch_yield_stooq() -> Optional[dict]:
    """stooq.com — serves historical CSV directly, no JS, no auth needed."""
    logging.info("  Trying stooq.com (10inbmk.b)...")
    # stooq India 10Y benchmark bond symbol
    url = "https://stooq.com/q/d/l/?s=10inbmk.b&i=d"
    resp = safe_request(url)
    if resp is None:
        return None
    try:
        lines = [l for l in resp.text.strip().splitlines() if l.strip()]
        if len(lines) < 3:
            logging.info("  ✗ failed: too few rows in CSV")
            return None
        # stooq CSV: Date,Open,High,Low,Close,Volume
        header = lines[0].lower().split(",")
        if "close" not in header:
            logging.info("  ✗ failed: unexpected CSV header")
            return None
        close_idx = header.index("close")
        date_idx  = header.index("date")
        rows = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) <= max(close_idx, date_idx):
                continue
            try:
                d = date.fromisoformat(parts[date_idx].strip())
                v = float(parts[close_idx].strip())
                rows.append((d, v))
            except Exception:
                continue
        if not rows:
            logging.info("  ✗ failed: could not parse CSV rows")
            return None
        rows.sort(key=lambda x: x[0], reverse=True)
        if len(rows) < cfg.YIELD_MIN_ROWS:
            logging.info(f"  ✗ failed: only {len(rows)} rows (need {cfg.YIELD_MIN_ROWS})")
            return None
        y_today = rows[0][1]
        y30 = rows[22][1] if len(rows) > 22 else None
        y60 = rows[43][1] if len(rows) > 43 else None
        if y30 is None or y60 is None:
            logging.info("  ✗ failed: insufficient history for lookback")
            return None
        for v in [y_today, y30, y60]:
            if not (cfg.YIELD_MIN_VALID <= v <= cfg.YIELD_MAX_VALID):
                logging.info(f"  ✗ failed: value {v} out of valid range")
                return None
        lag = (date.today() - rows[0][0]).days
        if lag > 5:
            logging.info(f"  ✗ failed: latest data is {lag} days old")
            return None
        logging.info("  ✓ succeeded")
        return {"yield_today": y_today, "yield_30d_ago": y30, "yield_60d_ago": y60}
    except Exception as e:
        logging.warning(f"  ✗ failed (parse error): {e}")
        return None


def _fetch_yield_countryeconomy() -> Optional[dict]:
    """
    countryeconomy.com — plain HTML table, daily data, no JS rendering.
    Fetches current month + prior month to cover 60d history.
    URL pattern: /bonds/india?dr=YYYY-MM for monthly pages.
    """
    if not BS4_AVAILABLE:
        return None
    logging.info("  Trying countryeconomy.com...")

    def _fetch_month_rows(url: str) -> list:
        resp = safe_request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        if resp is None:
            return []
        try:
            soup = BeautifulSoup(resp.text, "html.parser")
            rows = []
            for table in soup.find_all("table"):
                for tr in table.find_all("tr")[1:]:
                    cols = [td.get_text(strip=True) for td in tr.find_all("td")]
                    if len(cols) >= 2:
                        try:
                            d = datetime.strptime(cols[0].strip(), "%m/%d/%Y").date()
                            v = float(cols[1].strip().replace("%", ""))
                            if cfg.YIELD_MIN_VALID <= v <= cfg.YIELD_MAX_VALID:
                                rows.append((d, v))
                        except Exception:
                            continue
            return rows
        except Exception:
            return []

    today = date.today()
    base_url = "https://countryeconomy.com/bonds/india"

    # Fetch current month + prior two months to guarantee 60d+ history
    all_rows: list = []
    for months_back in range(3):
        target = date(today.year, today.month, 1)
        # roll back months_back months
        m = today.month - months_back
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        url = base_url if months_back == 0 else f"{base_url}?dr={y:04d}-{m:02d}"
        all_rows.extend(_fetch_month_rows(url))

    if not all_rows:
        logging.info("  ✗ failed: no rows parsed")
        return None

    # Deduplicate and sort descending
    seen = set()
    unique_rows = []
    for r in sorted(all_rows, key=lambda x: x[0], reverse=True):
        if r[0] not in seen:
            seen.add(r[0])
            unique_rows.append(r)

    # Detect data frequency from median gap between consecutive rows
    gaps = []
    for i in range(min(5, len(unique_rows) - 1)):
        gap = abs((unique_rows[i][0] - unique_rows[i + 1][0]).days)
        gaps.append(gap)
    median_gap = sorted(gaps)[len(gaps) // 2] if gaps else 1

    if median_gap <= 3:
        idx_30d = 22
        idx_60d = 43
        freq_label = "daily"
    elif median_gap <= 10:
        idx_30d = 4
        idx_60d = 9
        freq_label = "weekly"
    else:
        idx_30d = 1
        idx_60d = 2
        freq_label = "monthly"

    logging.info(f"  countryeconomy.com data frequency: {freq_label} (median gap: {median_gap}d)")

    min_rows_needed = idx_60d + 2
    if len(unique_rows) < min_rows_needed:
        logging.info(f"  ✗ failed: only {len(unique_rows)} rows, need {min_rows_needed}")
        return None

    latest_date = unique_rows[0][0]
    lag_days = (today - latest_date).days
    if lag_days > 5:
        logging.info(f"  ✗ failed: latest data is {lag_days} days old (stale)")
        return None
    logging.info(f"  Latest data date: {latest_date} ({lag_days}d old)")

    y_today = unique_rows[0][1]
    y30 = unique_rows[idx_30d][1] if len(unique_rows) > idx_30d else None
    y60 = unique_rows[idx_60d][1] if len(unique_rows) > idx_60d else None

    if y30 is None or y60 is None:
        logging.info(f"  ✗ failed: insufficient rows for {freq_label} lookback")
        return None

    for v in [y_today, y30, y60]:
        if not (cfg.YIELD_MIN_VALID <= v <= cfg.YIELD_MAX_VALID):
            logging.info(f"  ✗ failed: value {v} out of valid range")
            return None

    logging.info(f"  ✓ succeeded ({len(unique_rows)} rows, {unique_rows[-1][0]} to {unique_rows[0][0]})")
    return {"yield_today": y_today, "yield_30d_ago": y30, "yield_60d_ago": y60}


def _fetch_yield_investing_com() -> Optional[dict]:
    """Investing.com now uses JS rendering — may return empty table. Best-effort only."""
    if not BS4_AVAILABLE:
        return None
    logging.info("  Trying Investing.com scrape (JS-rendered — likely to fail)...")
    url = "https://www.investing.com/rates-bonds/india-10-year-bond-yield-historical-data"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    resp = safe_request(url, headers=headers)
    if resp is None:
        return None
    try:
        soup = BeautifulSoup(resp.text, "lxml")
        table = soup.find("table")
        if table is None:
            logging.info("  ✗ failed: no table found (JS rendering likely)")
            return None
        rows = []
        for tr in table.find_all("tr")[1:]:
            cols = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cols) >= 2:
                try:
                    d = datetime.strptime(cols[0], "%b %d, %Y").date()
                    v = float(cols[1].replace(",", ""))
                    rows.append((d, v))
                except Exception:
                    continue
        if not rows:
            logging.info("  ✗ failed: no parseable rows (JS rendering likely)")
            return None
        rows.sort(key=lambda x: x[0], reverse=True)
        y_today = rows[0][1]
        y30 = rows[22][1] if len(rows) > 22 else None
        y60 = rows[43][1] if len(rows) > 43 else None
        if y30 is None or y60 is None:
            logging.info(f"  ✗ failed: only {len(rows)} rows, need 44+")
            return None
        for v in [y_today, y30, y60]:
            if not (cfg.YIELD_MIN_VALID <= v <= cfg.YIELD_MAX_VALID):
                logging.info(f"  ✗ failed: value {v} out of range")
                return None
        logging.info("  ✓ succeeded")
        return {"yield_today": y_today, "yield_30d_ago": y30, "yield_60d_ago": y60}
    except Exception as e:
        logging.warning(f"  ✗ failed (parse error): {e}")
        return None


def fetch_yield() -> FetchResult:
    logging.info("\n[DATA 1] Fetching 10Y G-Sec yield...")

    # Try all yfinance tickers in order
    for ticker in _YFINANCE_YIELD_TICKERS:
        data = _fetch_yield_yfinance(ticker)
        if data:
            return FetchResult(value=data, source=f"yfinance {ticker}",
                               fallback_used=(ticker != _YFINANCE_YIELD_TICKERS[0]),
                               data_date=date.today())

    # countryeconomy.com — plain HTML table, daily data, proven working
    data = _fetch_yield_countryeconomy()
    if data:
        return FetchResult(value=data, source="countryeconomy.com",
                           fallback_used=True, data_date=date.today())

    # stooq.com — needs API key as of 2025, will fail gracefully
    data = _fetch_yield_stooq()
    if data:
        return FetchResult(value=data, source="stooq.com 10inbmk.b",
                           fallback_used=True, data_date=date.today())

    # Investing.com — best-effort only (JS-rendered, likely fails)
    data = _fetch_yield_investing_com()
    if data:
        return FetchResult(value=data, source="Investing.com scrape",
                           fallback_used=True, data_date=date.today())

    # ── All live sources exhausted — smart manual fallback ─────────────────
    global manual_entry_count

    # Step 1: Get CCIL zero-coupon rate as a reference hint (not used directly)
    logging.info("  Trying CCIL for reference hint (zero-coupon rate)...")
    ccil_hint = get_ccil_zero_rate_hint()
    if ccil_hint:
        logging.info(f"  CCIL zero-coupon rate: {ccil_hint:.2f}%")
        logging.info(f"  Use this only as a sanity check on your manual entry.")
        logging.info(f"  Do NOT subtract any number from this — the spread is not fixed.")
    else:
        logging.info("  ✗ CCIL hint unavailable")

    # Step 2: Fetch 30d/60d ago from decision log if we have history
    y30 = get_yield_from_log(target_days_ago=30, tolerance_days=3)
    y60 = get_yield_from_log(target_days_ago=60, tolerance_days=5)
    if y30:
        logging.info(f"  Found ~30d ago yield in log: {y30:.2f}%")
    if y60:
        logging.info(f"  Found ~60d ago yield in log: {y60:.2f}%")

    # Step 3: Manual entry for what we still need
    logging.info("─" * 50)
    logging.info("Enter 10Y G-Sec benchmark (par) yield.")
    logging.info("Reference sources (pick any one):")
    logging.info("  NSE India : https://www.nseindia.com/market-data/fixed-income-securities")
    logging.info("  RBI       : https://www.rbi.org.in  (Monetary → Rates → G-Sec yields)")
    logging.info("  BSE India : https://www.bseindia.com/markets/debt/DebtSearch.aspx")
    logging.info("  Moneycontrol: https://www.moneycontrol.com/bonds/")
    logging.info("  Google    : search 'India 10 year bond yield today'")
    logging.info("       Or: https://www.investing.com/rates-bonds/india-10-year-bond-yield")
    if ccil_hint:
        logging.info(f"  CCIL zero-coupon rate for reference: {ccil_hint:.2f}% (do not adjust — spread varies)")
    logging.info("─" * 50)

    y_today = validate_float("10Y G-Sec yield TODAY (%): ", cfg.YIELD_MIN_VALID, cfg.YIELD_MAX_VALID)

    if y30 is None:
        logging.info("  For 30d ago: https://www.rbi.org.in/Scripts/WSSViewDetail.aspx?TYPE=Section&PARAM1=2")
        logging.info("           Or: https://www.bseindia.com/markets/debt/DebtSearch.aspx (set date)")
        y30 = validate_float("10Y G-Sec yield ~30 DAYS AGO (%): ", cfg.YIELD_MIN_VALID, cfg.YIELD_MAX_VALID)
    if y60 is None:
        y60 = validate_float("10Y G-Sec yield ~60 DAYS AGO (%): ", cfg.YIELD_MIN_VALID, cfg.YIELD_MAX_VALID)

    source_label = "Manual entry"
    if y30 is not None or y60 is not None:
        log_items = []
        if get_yield_from_log(30, 3): log_items.append("30d from log")
        if get_yield_from_log(60, 5): log_items.append("60d from log")
        if log_items:
            source_label = f"Manual (today) + log ({', '.join(log_items)})"

    manual_entry_count += 1
    return FetchResult(
        value={"yield_today": y_today, "yield_30d_ago": y30, "yield_60d_ago": y60},
        source=source_label,
        is_manual=True,
        data_date=date.today(),
        fallback_used=True,
    )


# ─── DATA 2: CPI Inflation ────────────────────────────────────────────────────

def _fetch_cpi_fred() -> Optional[dict]:
    """FRED: index values → compute YoY %. More reliable than World Bank for India monthly."""
    logging.info("  Trying FRED India CPI (INDCPIALLMINMEI)...")
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=INDCPIALLMINMEI"
    resp = safe_request(url)
    if resp is None:
        return None
    try:
        lines = resp.text.strip().splitlines()
        if len(lines) < 15:
            logging.info("  ✗ failed: insufficient data rows")
            return None
        rows = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) == 2:
                try:
                    rows.append((parts[0].strip(), float(parts[1].strip())))
                except ValueError:
                    continue
        if len(rows) < 14:
            logging.info("  ✗ failed: not enough rows for YoY calculation")
            return None
        vals = []
        for i in range(-3, 0):
            current = rows[i][1]
            year_ago = rows[i - 12][1]
            pct = (current / year_ago - 1) * 100
            vals.append(round(pct, 2))
        if not all(cfg.CPI_MIN_VALID <= v <= cfg.CPI_MAX_VALID for v in vals):
            logging.info("  ✗ failed: computed values out of range")
            return None
        try:
            latest_data_date = date.fromisoformat(rows[-1][0])
        except Exception:
            latest_data_date = date.today()
        logging.info(f"  ✓ succeeded (data through {latest_data_date}, ~1-2 month lag possible)")
        return {"cpi_m1": vals[0], "cpi_m2": vals[1], "cpi_m3": vals[2], "core_cpi": None,
                "_data_date": latest_data_date}
    except Exception as e:
        logging.warning(f"  ✗ failed (parse error): {e}")
        return None


def _fetch_cpi_worldbank() -> Optional[dict]:
    """World Bank: annual/lagged — backup only. Often misses latest month."""
    logging.info("  Trying World Bank API (annual lag warning)...")
    url = ("https://api.worldbank.org/v2/country/IN/indicator/FP.CPI.TOTL.ZG"
           "?format=json&mrv=6&frequency=M")
    resp = safe_request(url)
    if resp is None:
        return None
    try:
        data = resp.json()
        if not isinstance(data, list) or len(data) < 2:
            logging.info("  ✗ failed: unexpected response structure")
            return None
        records = [r for r in data[1] if r.get("value") is not None]
        records.sort(key=lambda r: r["date"])
        if len(records) < 3:
            logging.info(f"  ✗ failed: only {len(records)} valid records")
            return None
        vals = [float(r["value"]) for r in records[-3:]]
        if not all(cfg.CPI_MIN_VALID <= v <= cfg.CPI_MAX_VALID for v in vals):
            logging.info("  ✗ failed: values out of valid range")
            return None
        logging.info("  ✓ succeeded (⚠️ World Bank data often lags 1-2 months — verify manually)")
        return {"cpi_m1": vals[0], "cpi_m2": vals[1], "cpi_m3": vals[2], "core_cpi": None}
    except Exception as e:
        logging.warning(f"  ✗ failed (parse error): {e}")
        return None


def fetch_cpi() -> FetchResult:
    global manual_entry_count
    logging.info("\n[DATA 2] Fetching CPI Inflation...")

    last_row = read_decision_log_last_row()
    cached_data = None
    days_since_update = None

    if last_row.get("cpi_m1") and last_row.get("cpi_data_date"):
        try:
            cached_date = date.fromisoformat(last_row["cpi_data_date"])
            days_since_update = (date.today() - cached_date).days
            cached_data = {
                "cpi_m1":    float(last_row["cpi_m1"]),
                "cpi_m2":    float(last_row["cpi_m2"]),
                "cpi_m3":    float(last_row["cpi_m3"]),
                "core_cpi":  float(last_row["core_cpi"]) if last_row.get("core_cpi") else None,
                "data_date": cached_date,
            }
        except Exception:
            cached_data = None

    today_day = date.today().day
    cpi_refresh_needed = False

    if cached_data is None:
        cpi_refresh_needed = True
        logging.info("  No cached CPI data found — fetching fresh.")
    elif days_since_update <= 30:
        cpi_refresh_needed = False
        logging.info(f"  CPI updated {days_since_update} days ago — reusing cache.")
    elif today_day < cfg.CPI_UPDATE_WINDOW[0]:
        cpi_refresh_needed = False
        logging.info(f"  Before CPI release window (day {today_day}) — reusing cache.")
    else:
        logging.info(f"  CPI data is {days_since_update} days old. New release may be available.")
        ans = input("Update CPI data? (y/n): ").strip().lower()
        cpi_refresh_needed = (ans == "y")

    if not cpi_refresh_needed and cached_data:
        is_stale = days_since_update > cfg.DATA_FRESHNESS["cpi"]
        logging.info(f"  Using cached CPI data (age: {days_since_update} days)")
        return FetchResult(
            value={"cpi_m1": cached_data["cpi_m1"],
                   "cpi_m2": cached_data["cpi_m2"],
                   "cpi_m3": cached_data["cpi_m3"],
                   "core_cpi": cached_data["core_cpi"]},
            source=last_row.get("cpi_source", "Cache"),
            is_stale=is_stale,
            data_date=cached_data["data_date"],
            fallback_used=False,
        )

    # Try automated sources — but CPI APIs lag heavily.
    # Manual entry (30 seconds) is the normal expected flow for accuracy.
    logging.info("  Trying automated CPI sources (may lag 1-2 months)...")
    data = _fetch_cpi_fred()
    if data:
        actual_date = data.pop("_data_date", date.today())
        return FetchResult(value=data, source="FRED", data_date=actual_date)

    data = _fetch_cpi_worldbank()
    if data:
        return FetchResult(value=data, source="World Bank API", fallback_used=True, data_date=date.today())

    # Manual entry — expected path, not emergency fallback
    lbl1 = _cpi_month_label(2)   # oldest
    lbl2 = _cpi_month_label(1)
    lbl3 = _cpi_month_label(0)   # latest
    logging.info("─" * 50)
    logging.info("CPI APIs unavailable or lagged. Enter last 3 months manually (~30 sec).")
    logging.info(f"  Google search for each month:")
    logging.info(f"    'India CPI inflation {lbl1}'  → enter the YoY % figure")
    logging.info(f"    'India CPI inflation {lbl2}'  → enter the YoY % figure")
    logging.info(f"    'India CPI inflation {lbl3}'  → enter the YoY % figure")
    logging.info("  Source: any mospi.gov.in / rbi.org.in / Mint / ET headline.")
    logging.info("  Enter the number only — e.g. 4.85 not 4.85%")
    logging.info("─" * 50)
    m1 = validate_float(f"{lbl1} CPI%: ", cfg.CPI_MIN_VALID, cfg.CPI_MAX_VALID)
    m2 = validate_float(f"{lbl2} CPI%: ", cfg.CPI_MIN_VALID, cfg.CPI_MAX_VALID)
    m3 = validate_float(f"{lbl3} CPI% (latest): ", cfg.CPI_MIN_VALID, cfg.CPI_MAX_VALID)
    core_raw = input("Core CPI (press Enter to skip): ").strip()
    core_cpi = None
    if core_raw:
        try:
            v = float(core_raw)
            if cfg.CPI_MIN_VALID <= v <= cfg.CPI_MAX_VALID:
                core_cpi = v
        except ValueError:
            pass
    manual_entry_count += 1
    return FetchResult(
        value={"cpi_m1": m1, "cpi_m2": m2, "cpi_m3": m3, "core_cpi": core_cpi},
        source="Manual entry",
        is_manual=True,
        data_date=date.today(),
        fallback_used=True,
    )


# ─── DATA 3: RBI Stance ───────────────────────────────────────────────────────

STANCE_PATTERNS = [
    "withdrawal of accommodation",
    "calibrated tightening",
    "accommodative",
    "neutral",
]

STANCE_OPTIONS = {
    1: "accommodative",
    2: "neutral",
    3: "withdrawal of accommodation",
    4: "calibrated tightening",
}


def _parse_stance_from_text(text: str) -> Tuple[Optional[str], Optional[int], Optional[int], str]:
    text_lower = text.lower()
    found_stance = None
    for pattern in STANCE_PATTERNS:
        if pattern in text_lower:
            found_stance = pattern
            break

    votes_for = votes_against = None
    vote_match = re.search(r"(\d)\s*[–\-to]+\s*(\d)", text)
    if not vote_match:
        vote_match = re.search(r"(\d+)\s+members?\s+voted", text, re.IGNORECASE)

    if vote_match:
        try:
            a, b = int(vote_match.group(1)), int(vote_match.group(2))
            if 1 <= a <= 6 and 1 <= b <= 6:
                votes_for, votes_against = a, b
        except Exception:
            pass

    if found_stance and votes_for is not None:
        confidence = "HIGH"
    elif found_stance:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    return found_stance, votes_for, votes_against, confidence


def _fetch_stance_rbi() -> Optional[dict]:
    if not BS4_AVAILABLE:
        logging.info("  ✗ failed: beautifulsoup4 not installed")
        return None
    logging.info("  Trying RBI press release page...")
    url = "https://rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    resp = safe_request(url, headers=headers)
    if resp is None:
        return None
    try:
        soup = BeautifulSoup(resp.text, "lxml")
        links = soup.find_all("a", href=True)
        mpc_links = []
        for a in links:
            title = (a.get("title") or a.get_text()).lower()
            if any(kw in title for kw in ["monetary policy", "mpc", "repo rate"]):
                mpc_links.append(a["href"])
        if not mpc_links:
            logging.info("  ✗ failed: no MPC links found")
            return None
        latest_url = mpc_links[0]
        if not latest_url.startswith("http"):
            latest_url = "https://rbi.org.in" + latest_url
        pr_resp = safe_request(latest_url, headers=headers)
        if pr_resp is None:
            return None
        full_text = BeautifulSoup(pr_resp.text, "lxml").get_text()
        stance, votes_for, votes_against, confidence = _parse_stance_from_text(full_text)
        if confidence == "LOW":
            logging.info("  ✗ failed: confidence LOW (stance not detected)")
            return None
        logging.info(f"  ✓ succeeded (confidence: {confidence})")
        return {
            "stance": stance,
            "votes_for": votes_for,
            "votes_against": votes_against,
            "confidence": confidence,
        }
    except Exception as e:
        logging.warning(f"  ✗ failed (parse error): {e}")
        return None


def fetch_stance() -> FetchResult:
    global manual_entry_count
    logging.info("\n[DATA 3] Fetching RBI Stance...")

    last_row = read_decision_log_last_row()
    cached_stance = None
    cached_date_str = None

    if last_row.get("rbi_stance"):
        cached_stance = last_row["rbi_stance"]
        cached_date_str = last_row.get("stance_data_date", "unknown")
        logging.info(f"  Last known RBI stance: {cached_stance} (from {cached_date_str})")
    else:
        logging.info("  No cached stance found.")

    logging.info("")
    ans = input("Has there been an RBI MPC meeting since your last run? (y/n): ").strip().lower()
    fetch_fresh = (ans == "y") or (cached_stance is None)

    if not fetch_fresh:
        try:
            stance_date = date.fromisoformat(cached_date_str)
        except Exception:
            stance_date = date.today()
        is_stale = (date.today() - stance_date).days > cfg.DATA_FRESHNESS["stance"]
        return FetchResult(
            value={
                "stance": cached_stance,
                "votes_for": (int(float(last_row["votes_for"]))
                              if last_row.get("votes_for", "").strip()
                              and last_row["votes_for"].strip() not in ("", "None")
                              else None),
                "votes_against": (6 - int(float(last_row["votes_for"]))
                                  if last_row.get("votes_for", "").strip()
                                  and last_row["votes_for"].strip() not in ("", "None")
                                  else None),
                "stance_changed": last_row.get("stance_changed", "False") == "True",
                "confidence": "CACHED",
            },
            source=last_row.get("stance_source", "Cache"),
            is_stale=is_stale,
            data_date=stance_date,
        )

    data = _fetch_stance_rbi()
    if data:
        prev_stance = cached_stance
        data["stance_changed"] = (prev_stance is not None and prev_stance != data["stance"])
        return FetchResult(value=data, source="RBI Press Release",
                           data_date=date.today(), fallback_used=False)

    # Manual entry
    mpc_search = date.today().strftime("%B %Y")
    logging.info("─" * 50)
    logging.info("RBI stance could not be parsed. Enter manually (~1 min).")
    logging.info(f"  Google: 'RBI MPC {mpc_search} decision'")
    logging.info("  Look for: stance name + vote split (e.g. '5-1 majority')")
    logging.info("─" * 50)
    logging.info("Select stance:")
    for k, v in STANCE_OPTIONS.items():
        logging.info(f"  {k}. {v}")
    choice = validate_int("Enter choice (1-4): ", 1, 4)
    stance = STANCE_OPTIONS[choice]
    logging.info("  Votes: look for '5-1' or '4-2' in the article.")
    logging.info("  Enter the FIRST number — members who voted FOR this decision.")
    logging.info("  Example: vote is '6-0' (unanimous) → enter 6")
    votes_for = validate_int("Votes FOR this decision (1-6): ", 1, 6)
    votes_against = 6 - votes_for
    prev_stance = cached_stance
    stance_changed = (prev_stance is not None and prev_stance != stance)
    manual_entry_count += 1
    return FetchResult(
        value={
            "stance": stance,
            "votes_for": votes_for,
            "votes_against": votes_against,
            "stance_changed": stance_changed,
            "confidence": "MANUAL",
        },
        source="Manual entry",
        is_manual=True,
        data_date=date.today(),
        fallback_used=True,
    )


# ─── DATA 4: INR/USD ──────────────────────────────────────────────────────────

def _get_inr_30d_from_log() -> Optional[float]:
    last_row = read_decision_log_last_row()
    if last_row.get("inr_today") and last_row.get("run_date"):
        try:
            row_date = date.fromisoformat(last_row["run_date"])
            age = (date.today() - row_date).days
            if 25 <= age <= 45:
                return float(last_row["inr_today"])
        except Exception:
            pass
    return None


def fetch_inr() -> FetchResult:
    global manual_entry_count
    logging.info("\n[DATA 4] Fetching INR/USD...")

    # Source 1: yfinance
    if YFINANCE_AVAILABLE:
        logging.info("  Trying yfinance (USDINR=X)...")
        try:
            t = yf.Ticker("USDINR=X")
            hist = t.history(period="35d", interval="1d")
            if not hist.empty and len(hist) >= 25:
                latest = float(hist["Close"].iloc[-1])
                if cfg.INR_MIN_VALID <= latest <= cfg.INR_MAX_VALID:
                    inr_30d = business_days_ago(hist, 22)
                    if inr_30d and cfg.INR_MIN_VALID <= inr_30d <= cfg.INR_MAX_VALID:
                        logging.info("  ✓ succeeded")
                        return FetchResult(
                            value={"inr_today": latest, "inr_30d_ago": inr_30d},
                            source="yfinance USDINR=X",
                            data_date=date.today(),
                        )
            logging.info(f"  ✗ failed: insufficient data ({len(hist)} rows)")
        except Exception as e:
            logging.warning(f"  ✗ failed: {e}")

    # Source 2: ExchangeRate-API
    logging.info("  Trying ExchangeRate-API...")
    inr_today = None
    resp = safe_request("https://api.exchangerate-api.com/v4/latest/USD")
    if resp is not None:
        try:
            inr_today = float(resp.json()["rates"]["INR"])
            if not (cfg.INR_MIN_VALID <= inr_today <= cfg.INR_MAX_VALID):
                logging.warning(f"  ✗ failed: value {inr_today} out of range")
                inr_today = None
            else:
                logging.info("  ✓ succeeded (current rate only)")
        except Exception as e:
            logging.warning(f"  ✗ failed (parse error): {e}")

    if inr_today is None:
        # Source 3: Frankfurter
        logging.info("  Trying Frankfurter API...")
        resp = safe_request("https://api.frankfurter.app/latest?to=INR")
        if resp is not None:
            try:
                inr_today = float(resp.json()["rates"]["INR"])
                if not (cfg.INR_MIN_VALID <= inr_today <= cfg.INR_MAX_VALID):
                    logging.warning(f"  ✗ failed: value {inr_today} out of range")
                    inr_today = None
                else:
                    logging.info("  ✓ succeeded (current rate only)")
            except Exception as e:
                logging.warning(f"  ✗ failed (parse error): {e}")

    if inr_today is not None:
        # Try to get 30d ago from log
        inr_30d = _get_inr_30d_from_log()
        if inr_30d:
            logging.info("  Using ~30d ago INR from decision_log.csv")
            return FetchResult(
                value={"inr_today": inr_today, "inr_30d_ago": inr_30d},
                source="API + log fallback",
                fallback_used=True,
                data_date=date.today(),
            )
        # Ask for 30d ago
        logging.info("  Could not find 30d ago INR in log. Please enter manually.")
        inr_30d = validate_float("INR/USD 30 days ago: ", cfg.INR_MIN_VALID, cfg.INR_MAX_VALID)
        manual_entry_count += 1
        return FetchResult(
            value={"inr_today": inr_today, "inr_30d_ago": inr_30d},
            source="API + manual 30d",
            fallback_used=True,
            data_date=date.today(),
        )

    # Source 4: Full manual
    logging.info("─" * 50)
    logging.info("Check: https://www.google.com/search?q=USD+INR+rate")
    logging.info("Or: https://in.tradingview.com/symbols/USDINR/")
    logging.info("─" * 50)
    inr_today = validate_float("INR/USD today: ", cfg.INR_MIN_VALID, cfg.INR_MAX_VALID)
    inr_30d   = validate_float("INR/USD 30 days ago: ", cfg.INR_MIN_VALID, cfg.INR_MAX_VALID)
    manual_entry_count += 1
    return FetchResult(
        value={"inr_today": inr_today, "inr_30d_ago": inr_30d},
        source="Manual entry",
        is_manual=True,
        data_date=date.today(),
        fallback_used=True,
    )


# ─── DATA 5: US Fed Funds Rate ────────────────────────────────────────────────

def _calc_fed_direction(rates: list) -> Tuple[float, str, int]:
    diffs = [rates[i + 1] - rates[i] for i in range(len(rates) - 1)]
    last_move = diffs[-1]
    if last_move < -0.01:
        direction = "cutting"
    elif last_move > 0.01:
        direction = "hiking"
    else:
        direction = "holding"

    consecutive = 1
    for d in reversed(diffs[:-1]):
        if direction == "cutting" and d < -0.01:
            consecutive += 1
        elif direction == "hiking" and d > 0.01:
            consecutive += 1
        elif direction == "holding" and abs(d) <= 0.01:
            consecutive += 1
        else:
            break

    return rates[-1], direction, consecutive


def fetch_fed() -> FetchResult:
    global manual_entry_count
    logging.info("\n[DATA 5] Fetching US Fed Funds Rate...")

    # Source 1: FRED direct CSV
    logging.info("  Trying FRED direct CSV (FEDFUNDS)...")
    resp = safe_request("https://fred.stlouisfed.org/graph/fredgraph.csv?id=FEDFUNDS")
    if resp is not None:
        try:
            lines = resp.text.strip().splitlines()
            rows = []
            for line in lines[1:]:
                parts = line.split(",")
                if len(parts) == 2:
                    try:
                        rows.append((parts[0].strip(), float(parts[1].strip())))
                    except ValueError:
                        continue
            if len(rows) >= 8:
                rates = [r[1] for r in rows[-8:]]
                fed_rate, direction, consecutive = _calc_fed_direction(rates)
                if 0.0 <= fed_rate <= 10.0:
                    data_date = date.fromisoformat(rows[-1][0]) if rows else date.today()
                    logging.info("  ✓ succeeded")
                    return FetchResult(
                        value={"fed_rate": fed_rate, "fed_direction": direction,
                               "fed_consecutive": consecutive},
                        source="FRED CSV",
                        data_date=data_date,
                    )
            logging.info("  ✗ failed: insufficient rows")
        except Exception as e:
            logging.warning(f"  ✗ failed (parse error): {e}")

    # Source 2: FRED API (if key available)
    fred_key = os.getenv("FRED_API_KEY")
    if fred_key:
        logging.info("  Trying FRED API (with key)...")
        try:
            from fredapi import Fred
            fred = Fred(api_key=fred_key)
            data = fred.get_series("FEDFUNDS", observation_start="2024-01-01")
            rates = list(data.dropna().values)
            if len(rates) >= 6:
                fed_rate, direction, consecutive = _calc_fed_direction(rates[-8:])
                if 0.0 <= fed_rate <= 10.0:
                    logging.info("  ✓ succeeded")
                    return FetchResult(
                        value={"fed_rate": fed_rate, "fed_direction": direction,
                               "fed_consecutive": consecutive},
                        source="FRED API",
                        fallback_used=True,
                        data_date=date.today(),
                    )
        except Exception as e:
            logging.warning(f"  ✗ failed: {e}")
    else:
        logging.info("  Skipping FRED API (no FRED_API_KEY in .env)")

    # Source 3: Manual entry
    logging.info("─" * 50)
    logging.info("Check: https://www.federalreserve.gov/releases/h15/")
    logging.info("Or: https://fred.stlouisfed.org/series/FEDFUNDS")
    logging.info("─" * 50)
    fed_rate  = validate_float("Current Fed funds rate (%): ", 0.0, 10.0)
    direction_raw = ""
    while direction_raw not in ("cutting", "hiking", "holding"):
        direction_raw = input("Direction (cutting/hiking/holding): ").strip().lower()
        if direction_raw not in ("cutting", "hiking", "holding"):
            logging.warning("  Enter: cutting, hiking, or holding")
    consecutive = validate_int("Consecutive meetings in this direction: ", 1, 20)
    manual_entry_count += 1
    return FetchResult(
        value={"fed_rate": fed_rate, "fed_direction": direction_raw,
               "fed_consecutive": consecutive},
        source="Manual entry",
        is_manual=True,
        data_date=date.today(),
        fallback_used=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION D: SCORING FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def compute_data_confidence(results: dict) -> Tuple[float, str]:
    score = 5.0
    for key, fr in results.items():
        if fr.is_manual:
            score -= 1.0
        elif fr.is_stale:
            score -= 0.5
        elif fr.fallback_used:
            score -= 0.25

    if score >= 5.0:
        label = "HIGH — All data fresh and automated"
    elif score >= 4.0:
        label = "MEDIUM-HIGH — Minor fallbacks used"
    elif score >= 3.0:
        label = "MEDIUM — Some manual entries"
    else:
        label = "LOW — Multiple manual entries"

    return score, label


def compute_regime(cpi_readings: List[float]) -> Tuple[str, float]:
    m1, m2, m3 = cpi_readings
    cpi_latest = m3
    cpi_oldest = m1
    cpi_2m_change_bps = (cpi_oldest - cpi_latest) * 100

    if cpi_latest > cfg.CPI_HIGH:
        if cpi_2m_change_bps >= 50:
            return "HIGH_BUT_IMPROVING", 0.8
        return "HIGH", 0.6
    elif cpi_latest < cfg.CPI_LOW:
        if cpi_2m_change_bps < -30:
            return "LOW_BUT_RISING", 1.0
        return "LOW", 1.2
    return "MODERATE", 1.0


def compute_yield_signal(yield_today: float, yield_30d_ago: float,
                         yield_60d_ago: float) -> Tuple[float, dict]:
    change_60d = (yield_60d_ago - yield_today) * 100
    change_30d = (yield_30d_ago - yield_today) * 100

    if change_60d >= cfg.YIELD_STRONG_BPS:
        base = 2.0
        base_reason = f"Fell {change_60d:.1f} bps in 60d (>= {cfg.YIELD_STRONG_BPS} threshold)"
    elif change_60d >= cfg.YIELD_MODERATE_BPS:
        base = 1.0
        base_reason = f"Fell {change_60d:.1f} bps in 60d (>= {cfg.YIELD_MODERATE_BPS} threshold)"
    else:
        base = 0.0
        base_reason = f"Moved {change_60d:.1f} bps in 60d (below threshold)"

    if change_30d >= cfg.YIELD_VELOCITY_BPS:
        bonus = 1.0
        bonus_reason = f"Velocity active: fell {change_30d:.1f} bps in 30d"
    else:
        bonus = 0.0
        bonus_reason = f"No velocity: {change_30d:.1f} bps in 30d (need {cfg.YIELD_VELOCITY_BPS})"

    return base + bonus, {
        "change_60d": change_60d,
        "change_30d": change_30d,
        "base": base,
        "base_reason": base_reason,
        "bonus": bonus,
        "bonus_reason": bonus_reason,
    }


def compute_cpi_signal(cpi_readings: List[float],
                       core_cpi: Optional[float]) -> Tuple[float, dict]:
    m1, m2, m3 = cpi_readings

    all_three_falling    = (m3 < m2 < m1)
    two_of_three_falling = (m3 < m2) or (m2 < m1)

    if all_three_falling:
        trend_score  = 1.0
        trend_reason = f"All 3 months falling: {m1}→{m2}→{m3}"
        cpi_direction = "falling"
    elif two_of_three_falling:
        trend_score  = 0.5
        trend_reason = f"2 of 3 months falling: {m1}→{m2}→{m3}"
        cpi_direction = "mixed"
    else:
        trend_score  = 0.0
        trend_reason = f"Not falling: {m1}→{m2}→{m3}"
        cpi_direction = "rising"

    if core_cpi is not None:
        if core_cpi < cfg.CPI_LOW and all_three_falling:
            core_score  = 1.0
            core_reason = f"Core CPI {core_cpi}% < {cfg.CPI_LOW} and falling"
        elif core_cpi < cfg.CPI_LOW:
            core_score  = 0.5
            core_reason = f"Core CPI {core_cpi}% < {cfg.CPI_LOW} (trend mixed)"
        else:
            core_score  = 0.0
            core_reason = f"Core CPI {core_cpi}% >= {cfg.CPI_LOW} threshold"
    else:
        core_score  = 0.0
        core_reason = "Core CPI not provided — skipped"

    return trend_score + core_score, {
        "trend_score": trend_score,
        "trend_reason": trend_reason,
        "core_score": core_score,
        "core_reason": core_reason,
        "cpi_direction": cpi_direction,
    }


def compute_stance_signal(stance: str, stance_changed: bool,
                          votes_for: Optional[int]) -> Tuple[float, dict]:
    stance_map = {
        "accommodative":               1.0,
        "neutral":                     0.5,
        "withdrawal of accommodation": 0.0,
        "calibrated tightening":       0.0,
    }
    base = stance_map.get(stance, 0.0)
    change_bonus = 0.5 if stance_changed else 0.0

    if votes_for is not None:
        if votes_for >= 5:   vote_bonus = 0.5
        elif votes_for == 4: vote_bonus = 0.25
        else:                vote_bonus = 0.0
    else:
        vote_bonus = 0.0

    return base + change_bonus + vote_bonus, {
        "base": base,
        "change_bonus": change_bonus,
        "vote_bonus": vote_bonus,
    }


def run_scoring(
    yield_today: float, yield_30d_ago: float, yield_60d_ago: float,
    cpi_readings: List[float], core_cpi: Optional[float],
    stance: str, stance_changed: bool, votes_for: Optional[int],
    inr_today: float, inr_30d_ago: float,
    fed_direction: str, fed_consecutive: int,
) -> dict:

    # Veto check
    inr_30d_change_pct = (inr_today - inr_30d_ago) / inr_30d_ago * 100
    inr_veto    = inr_30d_change_pct >= cfg.INR_VETO_PCT
    inr_warning = cfg.INR_WARN_PCT <= inr_30d_change_pct < cfg.INR_VETO_PCT
    fed_veto    = (fed_direction == "hiking" and fed_consecutive >= 3)
    veto_active = inr_veto or fed_veto

    veto_reason = ""
    if inr_veto:
        veto_reason = f"INR depreciated {inr_30d_change_pct:.1f}% in 30d (>= {cfg.INR_VETO_PCT}% threshold)"
    elif fed_veto:
        veto_reason = f"Fed hiking {fed_consecutive} consecutive meetings (>= 3 threshold)"

    # Regime
    regime_label, multiplier = compute_regime(cpi_readings)

    # Signals
    yield_score, yield_detail = compute_yield_signal(yield_today, yield_30d_ago, yield_60d_ago)
    cpi_score, cpi_detail     = compute_cpi_signal(cpi_readings, core_cpi)
    stance_score, stance_detail = compute_stance_signal(stance, stance_changed, votes_for)

    # Conflict check
    yield_falling  = yield_detail["change_60d"] > 5
    cpi_direction  = cpi_detail["cpi_direction"]
    conflict       = (
        (yield_falling and cpi_direction == "rising") or
        (not yield_falling and cpi_direction == "falling")
    )
    conflict_note = ""
    if conflict:
        conflict_note = (
            f"⚠️ CONFLICT DETECTED: "
            f"Yield {'falling' if yield_falling else 'rising'} "
            f"but CPI {cpi_direction}. "
            f"Score capped at 3.0. "
            f"Wait for signals to align before acting."
        )

    raw_score = yield_score + cpi_score + stance_score
    if conflict:
        raw_score = min(raw_score, 3.0)
    adjusted_score = raw_score * multiplier

    if adjusted_score >= cfg.SCORE_STRONG:
        band = "STRONG"
    elif adjusted_score >= cfg.SCORE_MODERATE:
        band = "MODERATE"
    elif adjusted_score >= cfg.SCORE_WEAK:
        band = "WEAK"
    else:
        band = "NEGATIVE"

    return {
        "inr_30d_change_pct": inr_30d_change_pct,
        "inr_veto": inr_veto,
        "inr_warning": inr_warning,
        "fed_veto": fed_veto,
        "veto_active": veto_active,
        "veto_reason": veto_reason,
        "regime_label": regime_label,
        "multiplier": multiplier,
        "yield_score": yield_score,
        "yield_detail": yield_detail,
        "cpi_score": cpi_score,
        "cpi_detail": cpi_detail,
        "stance_score": stance_score,
        "stance_detail": stance_detail,
        "conflict": conflict,
        "conflict_note": conflict_note,
        "raw_score": raw_score,
        "adjusted_score": adjusted_score,
        "band": band,
        "cpi_direction": cpi_direction,
    }


def _fetch_yield_peak_countryeconomy(months: int = 6) -> Optional[float]:
    """Fetch up to `months` months of history from countryeconomy.com; return max yield."""
    if not BS4_AVAILABLE:
        return None
    today = date.today()
    base_url = "https://countryeconomy.com/bonds/india"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    values = []
    for months_back in range(months):
        m = today.month - months_back
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        url = base_url if months_back == 0 else f"{base_url}?dr={y:04d}-{m:02d}"
        resp = safe_request(url, headers=headers)
        if resp is None:
            continue
        try:
            soup = BeautifulSoup(resp.text, "html.parser")
            for table in soup.find_all("table"):
                for tr in table.find_all("tr")[1:]:
                    cols = [td.get_text(strip=True) for td in tr.find_all("td")]
                    if len(cols) >= 2:
                        try:
                            v = float(cols[1].strip().replace("%", ""))
                            if cfg.YIELD_MIN_VALID <= v <= cfg.YIELD_MAX_VALID:
                                values.append(v)
                        except Exception:
                            continue
        except Exception:
            continue
    return max(values) if values else None


def get_yield_peak_auto(yield_today: float) -> Tuple[float, str]:
    """Return (peak_yield, source_note) for cycle stage.
    Uses log + countryeconomy.com until log covers 18 months; log-only after that.
    """
    rows = read_decision_log_all_rows()
    cutoff = date.today() - timedelta(days=548)  # 18 months

    log_yields = [yield_today]
    log_dates  = []
    for row in rows:
        try:
            d = date.fromisoformat(row["run_date"])
            if d >= cutoff:
                v = float(row["yield_today"])
                if cfg.YIELD_MIN_VALID <= v <= cfg.YIELD_MAX_VALID:
                    log_yields.append(v)
                    log_dates.append(d)
        except Exception:
            continue

    log_span_days = (max(log_dates) - min(log_dates)).days if len(log_dates) >= 2 else 0
    months_to_fetch = max(0, 18 - log_span_days // 30)

    web_peak = None
    if months_to_fetch > 0:
        first_run_note = " (one-time cost — stops once log covers 18 months)" if not log_dates else ""
        logging.info(f"  Log covers ~{log_span_days // 30}m of 18m needed — "
                     f"fetching {months_to_fetch} months from countryeconomy.com{first_run_note}...")
        web_peak = _fetch_yield_peak_countryeconomy(months=months_to_fetch)

    all_candidates = log_yields + ([web_peak] if web_peak is not None else [])
    peak = max(all_candidates)
    n_log = len(log_yields) - 1  # exclude current

    if months_to_fetch == 0:
        source = f"log ({n_log} runs, full 18m coverage)"
    elif web_peak is not None:
        source = f"log ({n_log} runs, {log_span_days}d) + countryeconomy.com ({months_to_fetch}m gap fill)"
    else:
        source = f"log ({n_log} runs, {log_span_days}d) — countryeconomy fetch failed"

    return peak, source


def compute_cycle_stage(yield_today: float) -> Tuple[str, str]:
    logging.info("\n  Auto-detecting yield peak for cycle stage...")
    yield_peak, peak_source = get_yield_peak_auto(yield_today)
    total_fall_bps = (yield_peak - yield_today) * 100
    logging.info(f"  Peak: {yield_peak:.2f}%  Current: {yield_today:.2f}%"
                 f"  Fallen: {total_fall_bps:.0f} bps  Source: {peak_source}")
    if total_fall_bps <= 0:
        return "EARLY", f"Yield at or above peak ({yield_peak:.2f}%) — {peak_source}"
    elif total_fall_bps < cfg.CYCLE_EARLY_MAX:
        return "EARLY", f"{total_fall_bps:.0f} bps fallen from {yield_peak:.2f}% — {peak_source}"
    elif total_fall_bps < cfg.CYCLE_MID_MAX:
        return "MID", f"{total_fall_bps:.0f} bps fallen from {yield_peak:.2f}% — {peak_source}"
    else:
        return "LATE", f"{total_fall_bps:.0f} bps fallen from {yield_peak:.2f}% — cycle mature — {peak_source}"


def compute_recommendation(band: str, cycle_stage: str, veto_active: bool,
                            veto_reason: str, band_changed: bool,
                            no_action_note: str, conflict: bool) -> Tuple[str, str, list, str, int, str]:
    if veto_active:
        return ("HOLD", veto_reason, [], "", 0,
                "New money: wait — do not deploy fresh capital until this veto clears.")

    if band == "STRONG" and cycle_stage == "EARLY" and not conflict:
        market_confidence = "HIGH"
        tranche_pct = 100
        return ("INCREASE LONG DURATION",
                f"Strong signal, early cycle. Full tranche ({tranche_pct}%).",
                cfg.INSTRUMENTS["long_early"], market_confidence, tranche_pct,
                "New money: excellent entry point — deploy full planned allocation now.")

    if band == "STRONG" and cycle_stage == "MID" and not conflict:
        market_confidence = "MEDIUM-HIGH"
        tranche_pct = 80
        return ("INCREASE LONG DURATION",
                f"Strong signal, mid cycle. {tranche_pct}% tranche.",
                cfg.INSTRUMENTS["long_mid"], market_confidence, tranche_pct,
                f"New money: good entry point — deploy {tranche_pct}% of planned allocation now.")

    if band == "STRONG" and (cycle_stage == "LATE" or conflict):
        market_confidence = "MEDIUM"
        tranche_pct = 50
        return ("HOLD / SMALL ADD",
                f"Strong signal but cycle mature. {tranche_pct}% tranche max.",
                cfg.INSTRUMENTS["long_late"], market_confidence, tranche_pct,
                f"New money: late in cycle — enter cautiously, {tranche_pct}% tranche max.")

    if band == "MODERATE" and conflict:
        return ("HOLD",
                "Moderate score but signals conflict. Wait for alignment before acting.",
                [], "LOW", 0,
                "New money: signals conflict — wait for alignment before deploying fresh capital.")

    if band == "MODERATE" and not conflict:
        market_confidence = "MEDIUM-LOW"
        tranche_pct = 30
        return ("CONSIDER MODERATE ENTRY",
                f"Moderate signal. {tranche_pct}% tranche only.",
                cfg.INSTRUMENTS["medium"], market_confidence, tranche_pct,
                f"New money: decent entry — {tranche_pct}% tranche, build position gradually.")

    if band == "WEAK":
        return ("STAY SHORT DURATION",
                "Insufficient signal. Stay in short-duration instruments.",
                cfg.INSTRUMENTS["short"], "LOW", 0,
                "New money: park in short-duration instruments until signal strengthens.")

    if band == "NEGATIVE":
        return ("REDUCE LONG EXPOSURE",
                "Signals point to rising rates. Reduce duration.",
                cfg.INSTRUMENTS["short"], "LOW", 0,
                "New money: avoid long duration entirely — park new capital in short-duration instruments.")

    return ("HOLD", "Signals unclear.", [], "LOW", 0,
            "New money: wait until signals are clearer.")


def get_market_confidence(band: str, cycle_stage: str, conflict: bool) -> Tuple[str, int]:
    if band == "STRONG" and cycle_stage == "EARLY" and not conflict:
        return "HIGH", 100
    elif band == "STRONG" and cycle_stage == "MID" and not conflict:
        return "MEDIUM-HIGH", 80
    elif band == "STRONG" and (cycle_stage == "LATE" or conflict):
        return "MEDIUM", 50
    elif band == "MODERATE" and not conflict:
        return "MEDIUM-LOW", 30
    return "LOW", 0


# ══════════════════════════════════════════════════════════════════════════════
# SECTION E: OUTPUT FORMATTER
# ══════════════════════════════════════════════════════════════════════════════

def format_age(fr: FetchResult, freshness_key: str) -> str:
    delta = (date.today() - fr.data_date).days
    threshold = cfg.DATA_FRESHNESS[freshness_key]
    if delta == 0:
        age_str = "current  (fetched today)"
    else:
        age_str = f"{delta} day{'s' if delta != 1 else ''} old  (from {fr.data_date})"
    if delta > threshold:
        age_str += f"  ⚠️"
    if fr.fallback_used and not fr.is_manual:
        age_str += "  [fallback used]"
    if fr.is_manual:
        age_str += "  [manual entry]"
    return age_str


def print_output(
    today: date,
    safe_mode: bool,
    manual_count: int,
    yield_fr: FetchResult,
    cpi_fr: FetchResult,
    stance_fr: FetchResult,
    inr_fr: FetchResult,
    fed_fr: FetchResult,
    scoring: dict,
    cycle_stage: str,
    cycle_note: str,
    band: str,
    previous_band: Optional[str],
    band_changed: bool,
    recommendation: str,
    rec_detail: str,
    instruments_list: list,
    market_confidence: str,
    data_confidence_label: str,
    no_action_note: str,
    new_money_note: str = "",
) -> None:
    W = 55
    logging.info("")
    logging.info("═" * W)
    logging.info("  BOND DURATION MONITOR")
    logging.info(f"  {today.strftime('%Y-%m-%d')}  {datetime.now().strftime('%H:%M')} IST")
    logging.info("═" * W)

    if safe_mode:
        logging.info("")
        logging.info("⚠️  SAFE MODE ACTIVE")
        logging.info(f"    {manual_count} data points entered manually.")
        logging.info("    Verify all inputs before acting on recommendation.")
        logging.info("═" * W)

    logging.info("")
    logging.info("── DATA FRESHNESS ──────────────────────────────────")
    logging.info(f"10Y Yield  : {format_age(yield_fr,  'yield')}  |  Source: {yield_fr.source}")
    logging.info(f"CPI data   : {format_age(cpi_fr,   'cpi')}  |  Source: {cpi_fr.source}")
    logging.info(f"RBI Stance : {format_age(stance_fr, 'stance')}  |  Source: {stance_fr.source}")
    logging.info(f"INR/USD    : {format_age(inr_fr,   'inr')}  |  Source: {inr_fr.source}")
    logging.info(f"Fed Rate   : {format_age(fed_fr,   'fed')}  |  Source: {fed_fr.source}")

    # Unpack values
    yv = yield_fr.value
    cv = cpi_fr.value
    sv = stance_fr.value
    iv = inr_fr.value
    fv = fed_fr.value

    y_today  = yv["yield_today"]
    y30      = yv["yield_30d_ago"]
    y60      = yv["yield_60d_ago"]
    m1, m2, m3 = cv["cpi_m1"], cv["cpi_m2"], cv["cpi_m3"]
    core     = cv.get("core_cpi")
    stance   = sv["stance"]
    vf       = sv.get("votes_for")
    va       = sv.get("votes_against")
    s_changed = sv.get("stance_changed", False)
    inr_now  = iv["inr_today"]
    inr_30   = iv["inr_30d_ago"]
    fed_rate = fv["fed_rate"]
    fed_dir  = fv["fed_direction"]
    fed_cons = fv["fed_consecutive"]

    change_30d_bps = (y30 - y_today) * 100
    change_60d_bps = (y60 - y_today) * 100

    logging.info("")
    logging.info("── RAW INPUTS ──────────────────────────────────────")
    logging.info(f"10Y Yield  today  : {y_today:.2f}%")
    logging.info(f"10Y Yield  30d ago: {y30:.2f}%   (Δ {change_30d_bps:+.1f} bps)")
    logging.info(f"10Y Yield  60d ago: {y60:.2f}%   (Δ {change_60d_bps:+.1f} bps)")
    logging.info("")
    logging.info(f"CPI (oldest → latest): {m1}% → {m2}% → {m3}%")
    logging.info(f"Core CPI: {core}%" if core is not None else "Core CPI: not provided")
    logging.info("")
    logging.info(f"RBI Stance    : {stance}")
    if vf is not None and va is not None:
        logging.info(f"Vote split    : {vf}–{va}")
    else:
        logging.info("Vote split    : unknown")
    logging.info(f"Stance changed: {'Yes' if s_changed else 'No'}")
    logging.info("")
    inr_chg = scoring["inr_30d_change_pct"]
    logging.info(f"INR/USD today  : {inr_now:.2f}")
    logging.info(f"INR/USD 30d ago: {inr_30:.2f}   (change: {inr_chg:+.2f}%)")
    logging.info("")
    logging.info(f"Fed rate     : {fed_rate:.2f}%")
    logging.info(f"Direction    : {fed_dir}")
    logging.info(f"Consecutive  : {fed_cons} meetings")

    logging.info("")
    logging.info("── VETO CHECK ──────────────────────────────────────")
    if scoring["inr_veto"]:
        logging.info(f"INR 30d move : {inr_chg:+.2f}%   🚨 ACTIVE")
    elif scoring["inr_warning"]:
        logging.info(f"INR 30d move : {inr_chg:+.2f}%   ⚠️ WARNING")
    else:
        logging.info(f"INR 30d move : {inr_chg:+.2f}%   CLEAR ✓")

    if scoring["fed_veto"]:
        logging.info(f"Fed constraint: 🚨 ACTIVE (hiking {fed_cons} consecutive meetings)")
    else:
        logging.info("Fed constraint: CLEAR ✓")

    if scoring["veto_active"]:
        logging.info(f"Overall veto : 🚨 ACTIVE — {scoring['veto_reason']}")
    else:
        logging.info("Overall veto : CLEAR ✓")

    if not scoring["veto_active"]:
        logging.info("")
        logging.info("── SCORING ─────────────────────────────────────────")
        logging.info(f"Regime    : {scoring['regime_label']}   Multiplier: {scoring['multiplier']}×")
        logging.info("")
        yd = scoring["yield_detail"]
        logging.info(f"Yield signal : {scoring['yield_score']:.1f} / 3.0")
        logging.info(f"  60d move   : {yd['change_60d']:.1f} bps → {yd['base_reason']}")
        logging.info(f"  30d velocity: {yd['change_30d']:.1f} bps → {yd['bonus_reason']}")
        logging.info("")
        cd = scoring["cpi_detail"]
        logging.info(f"CPI signal   : {scoring['cpi_score']:.1f} / 2.0")
        logging.info(f"  Trend      : {cd['trend_reason']}")
        logging.info(f"  Core CPI   : {cd['core_reason']}")
        logging.info("")
        sd = scoring["stance_detail"]
        logging.info(f"Stance signal: {scoring['stance_score']:.1f} / 2.0")
        logging.info(f"  Base {sd['base']:.2f} + Change bonus {sd['change_bonus']:.2f} + Vote bonus {sd['vote_bonus']:.2f}")
        logging.info("")
        if scoring["conflict"]:
            logging.info(f"Conflict     : {scoring['conflict_note']}")
        else:
            logging.info("Conflict     : NONE ✓")
        logging.info("")
        logging.info(f"Raw score    : {scoring['raw_score']:.2f} / 8.0")
        logging.info(f"× Multiplier : {scoring['multiplier']}×")
        logging.info(f"Adj score    : {scoring['adjusted_score']:.2f} / 8.0")
        logging.info(f"Band         : {band}")
        logging.info("")
        prev_str = previous_band if previous_band else "—"
        logging.info(f"Previous band: {prev_str}")
        if previous_band is None:
            logging.info("Band changed : First run — no previous band to compare")
        elif band_changed:
            logging.info("Band changed : YES — action may be needed")
        else:
            logging.info("Band changed : NO — no change needed")
        logging.info("")
        logging.info(f"Cycle stage  : {cycle_stage}  ({cycle_note})")

    logging.info("")
    logging.info("── CONFIDENCE ──────────────────────────────────────")
    logging.info(f"Market signal : {market_confidence}")
    logging.info(f"Data quality  : {data_confidence_label}")

    logging.info("")
    logging.info("── RECOMMENDATION ──────────────────────────────────")
    logging.info("")
    logging.info(f"  ▶  Existing position: {recommendation}")
    logging.info("")
    logging.info(f"  {rec_detail}")
    if new_money_note:
        logging.info("")
        logging.info(f"  ▶  New / not yet invested: {new_money_note}")
    if instruments_list:
        logging.info("")
        logging.info("  Suggested instruments:")
        for inst in instruments_list:
            logging.info(f"  → {inst}")
        hint = _duration_hint(instruments_list)
        if hint:
            logging.info(f"  ⚠ {hint}")
    if scoring["conflict"] and scoring["conflict_note"]:
        logging.info("")
        logging.info(f"  {scoring['conflict_note']}")
    if scoring["inr_warning"]:
        logging.info("")
        logging.info(f"  ⚠️ INR WARNING: {inr_chg:+.2f}% depreciation in 30d "
                     f"(above {cfg.INR_WARN_PCT}% caution level).")
    if (no_action_note
            and not band_changed
            and not scoring["veto_active"]
            and no_action_note.strip() != rec_detail.strip()):
        logging.info("")
        logging.info(f"  ℹ️  {no_action_note}")

    logging.info("")
    logging.info("── UPCOMING EVENTS ─────────────────────────────────")
    events = [
        ("Next MPC",  cfg.NEXT_MPC_DATE),
        ("Next CPI",  cfg.NEXT_CPI_DATE),
        ("Next FOMC", cfg.NEXT_FOMC_DATE),
    ]
    for label, ds in events:
        try:
            event_date = date.fromisoformat(ds)
            delta_days = (event_date - today).days
            logging.info(f"{label}  : {ds}  ({delta_days} days away)")
            if 0 <= delta_days <= 7:
                logging.info(f"  ⚠️ {label} in {delta_days} days. Consider waiting until after release before acting.")
        except Exception:
            logging.info(f"{label}  : {ds}  (date parse error)")

    logging.info("")
    logging.info("═" * W)


def write_html_report(
    today: date,
    safe_mode: bool,
    manual_count: int,
    yield_fr: FetchResult,
    cpi_fr: FetchResult,
    stance_fr: FetchResult,
    inr_fr: FetchResult,
    fed_fr: FetchResult,
    scoring: dict,
    cycle_stage: str,
    cycle_note: str,
    band: str,
    previous_band: Optional[str],
    band_changed: bool,
    recommendation: str,
    rec_detail: str,
    instruments_list: list,
    market_confidence: str,
    data_confidence_label: str,
    no_action_note: str,
    html_base_rec: str = "",
    html_base_detail: str = "",
    html_base_instruments: Optional[list] = None,
    html_base_new_money: str = "",
) -> None:
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    report_path = reports_dir / f"run_{today.isoformat()}.html"

    yv = yield_fr.value;  cv = cpi_fr.value
    sv = stance_fr.value; iv = inr_fr.value; fv = fed_fr.value

    y_today = yv["yield_today"]; y30 = yv["yield_30d_ago"]; y60 = yv["yield_60d_ago"]
    m1, m2, m3 = cv["cpi_m1"], cv["cpi_m2"], cv["cpi_m3"]; core = cv.get("core_cpi")
    stance = sv["stance"]; vf = sv.get("votes_for"); va = sv.get("votes_against")
    inr_now = iv["inr_today"]; inr_30 = iv["inr_30d_ago"]
    fed_rate = fv["fed_rate"]; fed_dir = fv["fed_direction"]; fed_cons = fv["fed_consecutive"]
    inr_chg = scoring["inr_30d_change_pct"]
    chg30 = (y30 - y_today) * 100; chg60 = (y60 - y_today) * 100

    band_css = {"STRONG": "strong", "MODERATE": "moderate",
                "WEAK": "weak", "NEGATIVE": "negative"}.get(band, "moderate")
    rec_css  = "veto" if scoring["veto_active"] else band_css

    def age_html(fr: FetchResult, key: str) -> str:
        delta = (today - fr.data_date).days
        threshold = cfg.DATA_FRESHNESS[key]
        s = "current" if delta == 0 else f"{delta}d old"
        cls = "stale" if delta > threshold else ""
        if fr.is_manual:
            cls = "manual"; s += " (manual)"
        elif fr.fallback_used:
            s += " (fallback)"
        return f'<span class="{cls}">{s}</span>'

    def bar(score: float, maxs: float) -> str:
        pct = min(100, int(score / maxs * 100))
        return (f'<div class="bar-wrap"><div class="bar" style="width:{pct}%"></div></div>')

    _disp_instruments = html_base_instruments if html_base_instruments is not None else instruments_list
    instr_html = ""
    if _disp_instruments:
        items = "".join(f"<li>{i}</li>" for i in _disp_instruments)
        hint = _duration_hint(_disp_instruments)
        hint_html = (f'<div class="dur-hint">&#9888; {hint}</div>' if hint else "")
        instr_html = f'<ul class="instruments">{items}</ul>{hint_html}'

    conflict_html = ""
    if scoring["conflict"] and scoring["conflict_note"]:
        conflict_html = f'<div class="conflict-warn">&#9888; {scoring["conflict_note"]}</div>'

    def veto_row_html(label, active, warning, msg_a, msg_c, msg_w=""):
        if active:
            return f'<div class="vrow vrow-active">&#128680; <b>{label}:</b> {msg_a}</div>'
        if warning:
            return f'<div class="vrow vrow-warn">&#9888; <b>{label}:</b> {msg_w}</div>'
        return f'<div class="vrow vrow-clear">&#10003; <b>{label}:</b> {msg_c}</div>'

    inr_row = veto_row_html(
        "INR 30d move", scoring["inr_veto"], scoring["inr_warning"],
        f"{inr_chg:+.2f}% &mdash; VETO ACTIVE",
        f"{inr_chg:+.2f}% &mdash; clear",
        f"{inr_chg:+.2f}% &mdash; warning (&gt;{cfg.INR_WARN_PCT}%)",
    )
    fed_row = veto_row_html(
        "Fed constraint", scoring["fed_veto"], False,
        f"Hiking {fed_cons} consecutive &mdash; VETO ACTIVE",
        "clear",
    )

    events_html = ""
    for lbl, ds in [("Next MPC", cfg.NEXT_MPC_DATE),
                    ("Next CPI", cfg.NEXT_CPI_DATE),
                    ("Next FOMC", cfg.NEXT_FOMC_DATE)]:
        try:
            dd = (date.fromisoformat(ds) - today).days
            soon = ' class="soon"' if 0 <= dd <= 7 else ""
            warn = f" &mdash; &#9888; {dd}d" if 0 <= dd <= 7 else f" ({dd}d)"
            events_html += (f'<div class="erow"><span>{lbl}</span>'
                            f'<span{soon}>{ds}{warn}</span></div>')
        except Exception:
            events_html += f'<div class="erow"><span>{lbl}</span><span>{ds}</span></div>'

    safe_banner = ""
    if safe_mode:
        safe_banner = (f'<div class="safe-banner">&#9888; <b>SAFE MODE</b>: '
                       f'{manual_count} data points entered manually. Verify before acting.</div>')

    prev_str = previous_band or "&mdash;"
    band_change_html = (
        f'<div class="band-changed">Band changed: {prev_str} &rarr; <b>{band}</b></div>'
        if band_changed else
        f'<div class="band-same">Band unchanged: {band} &mdash; no action needed</div>'
    )

    score_section = ""
    if not scoring["veto_active"]:
        yd = scoring["yield_detail"]; cd = scoring["cpi_detail"]; sd = scoring["stance_detail"]
        score_section = (
            '<div class="card">'
            '<h2>Score Breakdown</h2>'
            f'<div class="score-meta">Regime: <b>{scoring["regime_label"]}</b> &times; {scoring["multiplier"]}'
            f' &nbsp;|&nbsp; Raw: <b>{scoring["raw_score"]:.2f}</b>'
            f' &rarr; Adj: <b>{scoring["adjusted_score"]:.2f}</b>/8.0'
            f' &nbsp;|&nbsp; Band: <b class="b-{band_css}">{band}</b></div>'
            '<div class="sig-row"><div class="sig-lbl">Yield</div>'
            + bar(scoring["yield_score"], 3.0) +
            f'<div class="sig-val">{scoring["yield_score"]:.1f}/3</div></div>'
            f'<div class="sig-note">{yd["base_reason"]} &middot; {yd["bonus_reason"]}</div>'
            '<div class="sig-row" style="margin-top:8px"><div class="sig-lbl">CPI</div>'
            + bar(scoring["cpi_score"], 2.0) +
            f'<div class="sig-val">{scoring["cpi_score"]:.1f}/2</div></div>'
            f'<div class="sig-note">{cd["trend_reason"]}</div>'
            '<div class="sig-row" style="margin-top:8px"><div class="sig-lbl">Stance</div>'
            + bar(scoring["stance_score"], 2.0) +
            f'<div class="sig-val">{scoring["stance_score"]:.1f}/2</div></div>'
            f'<div class="sig-note">Base {sd["base"]:.2f} + Change {sd["change_bonus"]:.2f}'
            f' + Votes {sd["vote_bonus"]:.2f}</div>'
            f'<div style="margin-top:10px">Cycle stage: <b>{cycle_stage}</b> &mdash; {cycle_note}</div>'
            + conflict_html +
            f'<div style="margin-top:10px">{band_change_html}</div>'
            '</div>'
        )

    no_action_html = ""
    if not band_changed and not scoring["veto_active"]:
        prev_label = previous_band or "—"
        no_action_html = (
            f'<div class="no-change-badge">&#9989; Band unchanged '
            f'({prev_label} &rarr; {band}) &mdash; no new action needed</div>'
        )

    new_money_html = ""
    if html_base_new_money:
        new_money_html = (
            '<div class="rec-label">If not yet invested</div>'
            f'<div class="new-money">{html_base_new_money}</div>'
        )

    now_str = datetime.now().strftime("%H:%M")
    html = (
        '<!DOCTYPE html><html lang="en"><head>'
        '<meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">'
        f'<title>Bond Monitor &mdash; {today}</title>'
        '<style>'
        '*{box-sizing:border-box;margin:0;padding:0}'
        'body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;'
        'background:#f2f3f7;color:#1a1a2e;padding:14px;font-size:15px}'
        '.container{max-width:680px;margin:0 auto}'
        'h2{font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;color:#888;'
        'margin-bottom:11px;border-bottom:1px solid #eee;padding-bottom:5px}'
        '.card{background:#fff;border-radius:10px;padding:18px;margin-bottom:13px;'
        'box-shadow:0 1px 4px rgba(0,0,0,.08)}'
        '.header{margin-bottom:0}'
        '.header h1{font-size:1.25rem;color:#1a1a2e}'
        '.subtitle{color:#777;font-size:.83rem;margin-top:4px}'
        '.safe-banner{background:#fffbeb;border:1px solid #fbbf24;border-radius:8px;'
        'padding:11px;margin-bottom:13px;font-size:.86rem}'
        '.rec-action{font-size:1.3rem;font-weight:700;margin-bottom:6px}'
        '.rec-detail{color:#555;font-size:.88rem;margin-bottom:10px}'
        '.card.r-strong{border-left:5px solid #22c55e}'
        '.card.r-moderate{border-left:5px solid #f59e0b}'
        '.card.r-weak{border-left:5px solid #f97316}'
        '.card.r-negative,.card.r-veto{border-left:5px solid #ef4444;background:#fef9f9}'
        '.b-strong{color:#16a34a}.b-moderate{color:#d97706}'
        '.b-weak,.b-negative{color:#dc2626}'
        '.instruments{list-style:none;font-size:.86rem}'
        '.instruments li{padding:2px 0}.instruments li::before{content:"\\2192  ";color:#aaa}'
        '.no-action{font-size:.83rem;color:#888;margin-top:8px}'
        '.dur-hint{font-size:.8rem;color:#92400e;background:#fffbeb;border:1px solid #fcd34d;'
        'border-radius:5px;padding:7px 9px;margin-top:8px}'
        '.score-meta{font-size:.85rem;color:#555;margin-bottom:13px}'
        '.sig-row{display:flex;align-items:center;gap:8px;margin:3px 0}'
        '.sig-lbl{width:56px;font-size:.78rem;color:#777}'
        '.bar-wrap{flex:1;background:#e9ecef;border-radius:4px;height:7px}'
        '.bar{height:7px;border-radius:4px;background:#3b82f6}'
        '.sig-val{width:34px;font-size:.78rem;font-weight:600;text-align:right}'
        '.sig-note{font-size:.76rem;color:#999;margin-left:64px;margin-bottom:2px}'
        '.band-changed{background:#f0fdf4;border:1px solid #86efac;border-radius:6px;'
        'padding:7px;font-size:.83rem}'
        '.band-same{background:#f9f9f9;border:1px solid #e5e7eb;border-radius:6px;'
        'padding:7px;font-size:.83rem;color:#777}'
        '.conflict-warn{background:#fef3c7;border:1px solid #fcd34d;border-radius:6px;'
        'padding:9px;font-size:.81rem;margin-top:9px}'
        '.vrow{padding:7px 10px;border-radius:6px;font-size:.86rem;margin-bottom:6px}'
        '.vrow-active{background:#fef2f2;border:1px solid #fca5a5}'
        '.vrow-warn{background:#fffbeb;border:1px solid #fcd34d}'
        '.vrow-clear{background:#f0fdf4;border:1px solid #bbf7d0;color:#166534}'
        'table{width:100%;border-collapse:collapse;font-size:.84rem}'
        'table td{padding:5px 2px;vertical-align:top}'
        'table td:first-child{color:#777;width:40%;padding-right:8px}'
        '.stale{color:#f97316}.manual{color:#8b5cf6}'
        '.erow{display:flex;justify-content:space-between;padding:6px 0;'
        'border-bottom:1px solid #f0f0f0;font-size:.85rem}'
        '.erow:last-child{border-bottom:none}'
        '.soon{color:#f97316;font-weight:600}'
        '.no-change-badge{background:#eff6ff;border:1px solid #93c5fd;border-radius:6px;'
        'padding:8px 10px;font-size:.82rem;color:#1d4ed8;margin-top:8px}'
        '.rec-label{font-size:.7rem;text-transform:uppercase;letter-spacing:.04em;'
        'color:#999;margin-top:10px;margin-bottom:2px}'
        '.new-money{background:#f5f3ff;border:1px solid #c4b5fd;border-radius:6px;'
        'padding:8px 10px;font-size:.85rem;color:#5b21b6;margin-top:4px}'
        '@media(max-width:480px){.sig-lbl{width:44px}}'
        '</style></head><body><div class="container">'

        f'<div class="card header">'
        f'<h1>Bond Duration Monitor</h1>'
        f'<div class="subtitle">{today} &nbsp;&middot;&nbsp; {now_str} IST'
        f' &nbsp;&middot;&nbsp; {data_confidence_label}</div></div>'

        + safe_banner +

        f'<div class="card r-{rec_css}">'
        f'<h2>Recommendation</h2>'
        '<div class="rec-label">If already invested</div>'
        f'<div class="rec-action">{html_base_rec or recommendation}</div>'
        f'<div class="rec-detail">{html_base_detail or rec_detail}</div>'
        + instr_html + no_action_html + new_money_html +
        '</div>'

        '<div class="card"><h2>Veto Check</h2>'
        + inr_row + fed_row +
        '</div>'

        + score_section +

        '<div class="card"><h2>Raw Inputs</h2>'
        '<table>'
        f'<tr><td>10Y Yield today</td><td>{y_today:.2f}%</td></tr>'
        f'<tr><td>10Y Yield 30d ago</td><td>{y30:.2f}% (&Delta; {chg30:+.1f} bps)</td></tr>'
        f'<tr><td>10Y Yield 60d ago</td><td>{y60:.2f}% (&Delta; {chg60:+.1f} bps)</td></tr>'
        f'<tr><td>CPI (old&rarr;latest)</td><td>{m1}% &rarr; {m2}% &rarr; {m3}%</td></tr>'
        f'<tr><td>Core CPI</td><td>{"not provided" if core is None else f"{core}%"}</td></tr>'
        f'<tr><td>RBI Stance</td><td>{stance}</td></tr>'
        f'<tr><td>Vote split</td><td>{"unknown" if vf is None else f"{vf}&ndash;{va}"}</td></tr>'
        f'<tr><td>INR/USD today</td><td>{inr_now:.2f}</td></tr>'
        f'<tr><td>INR/USD 30d ago</td><td>{inr_30:.2f} ({inr_chg:+.2f}%)</td></tr>'
        f'<tr><td>Fed funds rate</td><td>{fed_rate:.2f}% &mdash; {fed_dir} ({fed_cons} mtgs)</td></tr>'
        f'<tr><td>Market confidence</td><td>{market_confidence}</td></tr>'
        '</table></div>'

        '<div class="card"><h2>Data Freshness</h2>'
        '<table>'
        f'<tr><td>10Y Yield</td><td>{age_html(yield_fr,"yield")} &nbsp;&middot;&nbsp; {yield_fr.source}</td></tr>'
        f'<tr><td>CPI</td><td>{age_html(cpi_fr,"cpi")} &nbsp;&middot;&nbsp; {cpi_fr.source}</td></tr>'
        f'<tr><td>RBI Stance</td><td>{age_html(stance_fr,"stance")} &nbsp;&middot;&nbsp; {stance_fr.source}</td></tr>'
        f'<tr><td>INR/USD</td><td>{age_html(inr_fr,"inr")} &nbsp;&middot;&nbsp; {inr_fr.source}</td></tr>'
        f'<tr><td>Fed Rate</td><td>{age_html(fed_fr,"fed")} &nbsp;&middot;&nbsp; {fed_fr.source}</td></tr>'
        '</table></div>'

        '<div class="card"><h2>Upcoming Events</h2>'
        + events_html +
        '</div>'

        '</div></body></html>'
    )

    report_path.write_text(html, encoding="utf-8")
    logging.info(f"  HTML report saved: {report_path}")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION F: CSV WRITER
# ══════════════════════════════════════════════════════════════════════════════

CSV_COLUMNS = [
    "run_date", "run_time",
    "yield_today", "yield_30d_ago", "yield_60d_ago",
    "yield_60d_change_bps", "yield_source",
    "cpi_m1", "cpi_m2", "cpi_m3", "core_cpi",
    "cpi_data_date", "cpi_source",
    "rbi_stance", "votes_for", "stance_changed",
    "stance_data_date", "stance_source",
    "inr_today", "inr_30d_ago", "inr_30d_change_pct",
    "inr_source",
    "fed_rate", "fed_direction", "fed_consecutive",
    "fed_source",
    "regime", "multiplier",
    "yield_signal", "cpi_signal", "stance_signal",
    "raw_score", "adjusted_score", "band",
    "previous_band", "band_changed",
    "veto_active", "conflict_detected",
    "cycle_stage", "market_confidence",
    "data_confidence", "safe_mode",
    "recommendation", "your_action",
]


def write_decision_log(row: dict) -> None:
    log_path = Path("decision_log.csv")
    is_new = not log_path.exists()
    with open(log_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def build_csv_row(
    today: date,
    yield_fr: FetchResult,
    cpi_fr: FetchResult,
    stance_fr: FetchResult,
    inr_fr: FetchResult,
    fed_fr: FetchResult,
    scoring: dict,
    cycle_stage: str,
    band: str,
    previous_band: Optional[str],
    band_changed: bool,
    market_confidence: str,
    data_confidence_score: float,
    data_confidence_label: str,
    safe_mode: bool,
    recommendation: str,
    your_action: str,
) -> dict:
    yv = yield_fr.value
    cv = cpi_fr.value
    sv = stance_fr.value
    iv = inr_fr.value
    fv = fed_fr.value
    return {
        "run_date":              today.isoformat(),
        "run_time":              datetime.now().strftime("%H:%M:%S"),
        "yield_today":           round(yv["yield_today"], 4),
        "yield_30d_ago":         round(yv["yield_30d_ago"], 4),
        "yield_60d_ago":         round(yv["yield_60d_ago"], 4),
        "yield_60d_change_bps":  round(scoring["yield_detail"]["change_60d"], 2),
        "yield_source":          yield_fr.source,
        "cpi_m1":                cv["cpi_m1"],
        "cpi_m2":                cv["cpi_m2"],
        "cpi_m3":                cv["cpi_m3"],
        "core_cpi":              cv.get("core_cpi", ""),
        "cpi_data_date":         cpi_fr.data_date.isoformat(),
        "cpi_source":            cpi_fr.source,
        "rbi_stance":            sv["stance"],
        "votes_for":             sv.get("votes_for", "") if sv.get("votes_for") is not None else "",
        "stance_changed":        sv.get("stance_changed", False),
        "stance_data_date":      stance_fr.data_date.isoformat(),
        "stance_source":         stance_fr.source,
        "inr_today":             round(iv["inr_today"], 4),
        "inr_30d_ago":           round(iv["inr_30d_ago"], 4),
        "inr_30d_change_pct":    round(scoring["inr_30d_change_pct"], 4),
        "inr_source":            inr_fr.source,
        "fed_rate":              fv["fed_rate"],
        "fed_direction":         fv["fed_direction"],
        "fed_consecutive":       fv["fed_consecutive"],
        "fed_source":            fed_fr.source,
        "regime":                scoring["regime_label"],
        "multiplier":            scoring["multiplier"],
        "yield_signal":          scoring["yield_score"],
        "cpi_signal":            scoring["cpi_score"],
        "stance_signal":         scoring["stance_score"],
        "raw_score":             round(scoring["raw_score"], 4),
        "adjusted_score":        round(scoring["adjusted_score"], 4),
        "band":                  band,
        "previous_band":         previous_band or "",
        "band_changed":          band_changed,
        "veto_active":           scoring["veto_active"],
        "conflict_detected":     scoring["conflict"],
        "cycle_stage":           cycle_stage,
        "market_confidence":     market_confidence,
        "data_confidence":       f"{data_confidence_score:.2f} — {data_confidence_label}",
        "safe_mode":             safe_mode,
        "recommendation":        recommendation,
        "your_action":           your_action,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SECTION G: TEST MODE
# ══════════════════════════════════════════════════════════════════════════════

def run_test_mode() -> None:
    logging.info("")
    logging.info("═" * 55)
    logging.info("  BOND MONITOR — TEST MODE")
    logging.info("═" * 55)

    # ── Test data ────────────────────────────────────────
    yield_today    = 6.71
    yield_30d_ago  = 6.85
    yield_60d_ago  = 6.96
    cpi_readings   = [5.3, 4.9, 4.6]
    core_cpi       = 4.3
    stance         = "accommodative"
    votes_for      = 6
    votes_against  = 0
    stance_changed = True
    inr_today      = 84.3
    inr_30d_ago    = 84.1
    fed_rate       = 4.25
    fed_direction  = "cutting"
    fed_consecutive = 3
    yield_peak     = 7.35

    def check(label: str, got, expected, note: str = "") -> bool:
        if isinstance(expected, str):
            ok = str(got).upper().startswith(expected.upper())
        elif isinstance(expected, float):
            ok = abs(got - expected) < 0.01
        else:
            ok = got == expected
        status = "PASS ✓" if ok else f"FAIL ✗  (got: {got})"
        logging.info(f"  {label:<28}: expected {expected!s:<20} {status}  {note}")
        return ok

    passes = 0
    total  = 0

    def tc(label, got, expected, note=""):
        nonlocal passes, total
        total += 1
        if check(label, got, expected, note):
            passes += 1

    # ── Scenario 1: Normal ───────────────────────────────
    logging.info("")
    logging.info("SCENARIO 1: Normal test data")
    logging.info("─" * 55)

    scoring = run_scoring(
        yield_today, yield_30d_ago, yield_60d_ago,
        cpi_readings, core_cpi,
        stance, stance_changed, votes_for,
        inr_today, inr_30d_ago,
        fed_direction, fed_consecutive,
    )

    total_fall_bps = (yield_peak - yield_today) * 100
    if total_fall_bps < cfg.CYCLE_EARLY_MAX:
        cycle_stage = "EARLY"
    elif total_fall_bps < cfg.CYCLE_MID_MAX:
        cycle_stage = "MID"
    else:
        cycle_stage = "LATE"

    tc("Veto",          scoring["veto_active"],       False)
    tc("Regime",        scoring["regime_label"],      "MODERATE")
    tc("Multiplier",    scoring["multiplier"],         1.0)
    tc("Yield signal",  scoring["yield_score"],        2.0)
    tc("Velocity bonus", scoring["yield_detail"]["bonus"], 0.0,
       f"30d={scoring['yield_detail']['change_30d']:.1f} bps, need {cfg.YIELD_VELOCITY_BPS}")
    tc("CPI signal",    scoring["cpi_score"],          2.0)
    tc("Stance signal", scoring["stance_score"],       2.0)
    tc("Raw score",     scoring["raw_score"],          6.0)
    tc("Adjusted score", scoring["adjusted_score"],    6.0)
    tc("Band",          scoring["band"],               "STRONG")
    tc("Conflict",      scoring["conflict"],           False)
    tc("Cycle stage",   cycle_stage,                  "MID")

    market_confidence, _ = get_market_confidence(
        scoring["band"], cycle_stage, scoring["conflict"])
    tc("Market confidence", market_confidence, "MEDIUM-HIGH")

    rec, rec_detail, instruments, _, _, _ = compute_recommendation(
        scoring["band"], cycle_stage,
        scoring["veto_active"], scoring["veto_reason"],
        True, "", scoring["conflict"],
    )
    tc("Recommendation", rec, "INCREASE LONG")

    # ── Scenario 2: Veto ─────────────────────────────────
    logging.info("")
    logging.info("SCENARIO 2: INR veto (4.2% depreciation)")
    logging.info("─" * 55)

    inr_30d_ago_veto = inr_today / (1 + 0.042)
    scoring_veto = run_scoring(
        yield_today, yield_30d_ago, yield_60d_ago,
        cpi_readings, core_cpi,
        stance, stance_changed, votes_for,
        inr_today, inr_30d_ago_veto,
        fed_direction, fed_consecutive,
    )
    tc("Veto ACTIVE",   scoring_veto["veto_active"],  True)
    rec2, _, _, _, _, _ = compute_recommendation(
        scoring_veto["band"], cycle_stage,
        scoring_veto["veto_active"], scoring_veto["veto_reason"],
        True, "", scoring_veto["conflict"],
    )
    tc("Recommendation HOLD", rec2, "HOLD")

    # ── Scenario 3: Conflict ──────────────────────────────
    logging.info("")
    logging.info("SCENARIO 3: Conflict (yield falling, CPI rising)")
    logging.info("─" * 55)

    cpi_rising  = [4.5, 4.7, 4.9]
    yield_hi    = 6.41   # today (lower — yield fell 30 bps in 60d)
    yield_60_hi = 6.71   # 60d ago
    yield_30_hi = 6.56   # 30d ago

    scoring_conflict = run_scoring(
        yield_hi, yield_30_hi, yield_60_hi,
        cpi_rising, None,
        stance, stance_changed, votes_for,
        inr_today, inr_30d_ago,
        fed_direction, fed_consecutive,
    )
    tc("Conflict detected", scoring_conflict["conflict"], True)
    tc("Score capped",
       scoring_conflict["raw_score"] <= 3.0, True)

    logging.info("")
    logging.info(f"Result: {passes}/{total} tests passed")
    logging.info("═" * 55)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION H: MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    global manual_entry_count

    parser = argparse.ArgumentParser(description="Bond Market Intelligence Monitor")
    parser.add_argument("--test", action="store_true", help="Run test mode (no fetching, no CSV write)")
    parser.add_argument("--report", action="store_true", help="Write HTML report to reports/run_YYYY-MM-DD.html")
    args = parser.parse_args()

    setup_logging()

    if args.test:
        run_test_mode()
        return

    today = date.today()
    logging.info("")
    logging.info("Bond Duration Monitor — starting up...")
    logging.info(f"Date: {today.isoformat()}")

    # ── Fetch all data ───────────────────────────────────
    manual_entry_count = 0

    yield_fr  = fetch_yield()
    cpi_fr    = fetch_cpi()
    stance_fr = fetch_stance()
    inr_fr    = fetch_inr()
    fed_fr    = fetch_fed()

    safe_mode = manual_entry_count > cfg.MAX_MANUAL_ENTRIES

    # ── Data confidence ──────────────────────────────────
    fetch_results = {
        "yield":  yield_fr,
        "cpi":    cpi_fr,
        "stance": stance_fr,
        "inr":    inr_fr,
        "fed":    fed_fr,
    }
    data_confidence_score, data_confidence_label = compute_data_confidence(fetch_results)

    # ── Unpack ───────────────────────────────────────────
    yv = yield_fr.value
    cv = cpi_fr.value
    sv = stance_fr.value
    iv = inr_fr.value
    fv = fed_fr.value

    cpi_readings = [cv["cpi_m1"], cv["cpi_m2"], cv["cpi_m3"]]

    # ── Scoring ──────────────────────────────────────────
    scoring = run_scoring(
        yv["yield_today"], yv["yield_30d_ago"], yv["yield_60d_ago"],
        cpi_readings, cv.get("core_cpi"),
        sv["stance"], sv.get("stance_changed", False), sv.get("votes_for"),
        iv["inr_today"], iv["inr_30d_ago"],
        fv["fed_direction"], fv["fed_consecutive"],
    )
    band = scoring["band"]

    # ── Band change check ────────────────────────────────
    last_row     = read_decision_log_last_row()
    previous_band = last_row.get("band") or None
    band_changed  = (previous_band is None) or (band != previous_band)
    no_action_note = ""
    if not band_changed:
        no_action_note = (
            f"Band unchanged from last run "
            f"({previous_band} → {band}). "
            f"No major allocation change needed."
        )

    # ── Cycle stage (auto-detected) ──────────────────────
    cycle_stage, cycle_note = compute_cycle_stage(yv["yield_today"])

    # ── Market confidence + recommendation ───────────────
    market_confidence, tranche_pct = get_market_confidence(
        band, cycle_stage, scoring["conflict"])

    recommendation, rec_detail, instruments_list, _, _, new_money_note = compute_recommendation(
        band, cycle_stage,
        scoring["veto_active"], scoring["veto_reason"],
        band_changed, no_action_note,
        scoring["conflict"],
    )

    # Underlying band action for HTML (ignores band_changed — always shows what to do)
    html_base_rec, html_base_detail, html_base_instruments, _, _, html_base_new_money = compute_recommendation(
        band, cycle_stage,
        scoring["veto_active"], scoring["veto_reason"],
        True, no_action_note,
        scoring["conflict"],
    )

    # ── Print output ─────────────────────────────────────
    print_output(
        today=today,
        safe_mode=safe_mode,
        manual_count=manual_entry_count,
        yield_fr=yield_fr,
        cpi_fr=cpi_fr,
        stance_fr=stance_fr,
        inr_fr=inr_fr,
        fed_fr=fed_fr,
        scoring=scoring,
        cycle_stage=cycle_stage,
        cycle_note=cycle_note,
        band=band,
        previous_band=previous_band,
        band_changed=band_changed,
        recommendation=recommendation,
        rec_detail=rec_detail,
        instruments_list=instruments_list,
        market_confidence=market_confidence,
        data_confidence_label=data_confidence_label,
        no_action_note=no_action_note,
        new_money_note=new_money_note,
    )

    # ── HTML report ──────────────────────────────────────
    if args.report:
        write_html_report(
            today=today, safe_mode=safe_mode, manual_count=manual_entry_count,
            yield_fr=yield_fr, cpi_fr=cpi_fr, stance_fr=stance_fr,
            inr_fr=inr_fr, fed_fr=fed_fr, scoring=scoring,
            cycle_stage=cycle_stage, cycle_note=cycle_note,
            band=band, previous_band=previous_band, band_changed=band_changed,
            recommendation=recommendation, rec_detail=rec_detail,
            instruments_list=instruments_list, market_confidence=market_confidence,
            data_confidence_label=data_confidence_label, no_action_note=no_action_note,
            html_base_rec=html_base_rec,
            html_base_detail=html_base_detail,
            html_base_instruments=html_base_instruments,
            html_base_new_money=html_base_new_money,
        )

    # ── User action ──────────────────────────────────────
    your_action = input("\nYour decision (press Enter to skip): ").strip()
    if your_action:
        logging.info("Logged.")
    else:
        logging.info("Run logged with no action recorded.")

    # ── Write CSV ────────────────────────────────────────
    csv_row = build_csv_row(
        today=today,
        yield_fr=yield_fr,
        cpi_fr=cpi_fr,
        stance_fr=stance_fr,
        inr_fr=inr_fr,
        fed_fr=fed_fr,
        scoring=scoring,
        cycle_stage=cycle_stage,
        band=band,
        previous_band=previous_band,
        band_changed=band_changed,
        market_confidence=market_confidence,
        data_confidence_score=data_confidence_score,
        data_confidence_label=data_confidence_label,
        safe_mode=safe_mode,
        recommendation=recommendation,
        your_action=your_action,
    )
    write_decision_log(csv_row)
    logging.info("Run saved to decision_log.csv")


if __name__ == "__main__":
    main()
