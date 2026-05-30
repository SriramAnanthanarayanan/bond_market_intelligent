# Bond Market Intelligence Monitor — CLAUDE.md

## What this is

CLI tool for Indian retail investors to decide bond duration allocation.
Single Python script. Manual run (no scheduler, no database, no web UI).
Target user: individual investor managing G-Sec / bond ETF exposure.

## File structure

```
bond_monitor.py              # Everything — 2478 lines, 8 sections (A–H)
config.py                    # Thresholds only — edit quarterly
requirements.txt             # Pinned deps
decision_log.csv             # Auto-created; persists run history
logs/run_YYYY-MM-DD.txt      # Auto-created daily log
reports/run_YYYY-MM-DD.html  # Auto-created with --report flag
how_to_run.txt               # Complete monthly operating guide (10 sections)
google_search_keyword.txt    # Pre-filled Google search terms for current run
.env                         # Optional: FRED_API_KEY=...
Bond_Market_Investors_Bible_V2.pdf  # Reference doc — not code
```

## How to run

```powershell
# Recommended — auto-installs deps via uv
uv run bond_monitor.py

# With HTML report saved to reports/run_YYYY-MM-DD.html
uv run bond_monitor.py --report

# Standard pip
python bond_monitor.py

# Test mode (no network, no CSV write, 18 assertions)
python bond_monitor.py --test
uv run bond_monitor.py --test
```

Test mode: 3 scenarios (Normal / INR veto / Conflict), 18/18 pass. Run before any scoring change.

## Architecture — bond_monitor.py sections

| Section | Purpose |
|---------|---------|
| A | Imports, logging setup (UTF-8 forced on Windows) |
| B | Helpers: `FetchResult` dataclass, validators, `safe_request()`, log readers |
| C | Fetchers: one per data source, each returns `FetchResult` |
| D | Scoring: `run_scoring()` → band → recommendation |
| E | `print_output()` — terminal report; `write_html_report()` — HTML report (--report flag) |
| F | CSV writer — `decision_log.csv` append |
| G | `run_test_mode()` — deterministic scenarios |
| H | `main()` — orchestrates fetch → score → print → log |

## Scoring system

**5 inputs → adjusted score → band → recommendation**

### Signal scores (raw total max ~7–8)

| Signal | Max | Key thresholds (config.py) |
|--------|-----|---------------------------|
| Yield (10Y G-Sec) | 3.0 | 60d drop ≥25 bps → 2.0; ≥10 bps → 1.0; 30d velocity ≥20 bps → +1.0 bonus |
| CPI trend | 2.0 | All 3 months falling → 1.0; 2 of 3 → 0.5; core CPI <4.5 bonus up to 1.0 |
| RBI stance | 2.0 | Accommodative → 1.0; Neutral → 0.5; change bonus +0.5; votes ≥5 → +0.5 |

### Regime multiplier (from CPI level)

| Regime | Multiplier | Condition |
|--------|-----------|-----------|
| HIGH | 0.6 | CPI >6.0% |
| HIGH_BUT_IMPROVING | 0.8 | CPI >6.0% but fell 50+ bps in 2m |
| MODERATE | 1.0 | 4.5% ≤ CPI ≤ 6.0% |
| LOW | 1.2 | CPI <4.5% |
| LOW_BUT_RISING | 1.0 | CPI <4.5% but rising |

### Bands (adjusted score)

| Band | Threshold |
|------|-----------|
| STRONG | ≥6.0 |
| MODERATE | ≥3.0 |
| WEAK | ≥1.0 |
| NEGATIVE | <1.0 |

### Vetoes (override all → HOLD)

- **INR veto**: INR/USD depreciation ≥3.0% in 30d
- **Fed veto**: Fed hiking ≥3 consecutive meetings
- Warning (no veto): INR depreciation ≥2.5%

### Conflict detection

Yield falling (>5 bps in 60d) AND CPI rising → cap raw score at 3.0. Wait for alignment.

### Cycle stage (auto-detected from decision_log.csv + countryeconomy.com)

| Stage | bps fallen from peak |
|-------|---------------------|
| EARLY | <50 bps |
| MID | 50–100 bps |
| LATE | >100 bps |

Peak auto-detected: `get_yield_peak_auto()` reads 18 months of history from log + countryeconomy.com gap-fill. No manual input needed after first run.

### Recommendations

| Band + Cycle | Action | Tranche |
|-------------|--------|---------|
| STRONG + EARLY | INCREASE LONG DURATION | 100% |
| STRONG + MID | INCREASE LONG DURATION | 80% |
| STRONG + LATE or conflict | HOLD / SMALL ADD | 50% |
| MODERATE | CONSIDER MODERATE ENTRY | 30% |
| WEAK | STAY SHORT DURATION | — |
| NEGATIVE | REDUCE LONG EXPOSURE | — |

### Instruments (config.py `INSTRUMENTS`)

Dynamically built from `_BHARAT_BOND_SERIES = [2030, 2031, 2032, 2033]`. ETFs <6 years away classified medium, ≥6 years = long.

| Zone | Suggested (as of 2026) |
|------|-----------|
| long_early | 10Y G-Sec (RBI Retail Direct), Bharat Bond ETF 2033 |
| long_mid | Bharat Bond ETF 2033, Bharat Bond ETF 2032 |
| long_late | Bharat Bond ETF 2032 |
| medium | Bharat Bond ETF 2032, SDL 5-7Y |
| short | T-Bills, Liquid Fund |

Add new Bharat Bond series to `_BHARAT_BOND_SERIES` when Edelweiss launches them.

## Data fetching — fallback chains

### DATA 1: 10Y G-Sec yield
1. yfinance: `^INBMK10Y`, `IN10Y=X`, `INBMK10Y`, `GIND10YR=RR` (all currently 404 — delisted)
2. **countryeconomy.com** — primary working source; plain HTML table, daily data; fetches current + 2 prior months to guarantee 60d history
3. stooq.com `10inbmk.b` — may need API key as of 2025
4. Investing.com scrape — JS-rendered, usually fails
5. Manual entry (prompts with CCIL zero-coupon hint + log lookback for 30d/60d ago)

### DATA 2: CPI inflation (3 monthly readings + optional core)
1. FRED `INDCPIALLMINMEI` — computes YoY%, lags 1–2 months
2. World Bank API — annual lag, backup
3. Cache from `decision_log.csv` — reused if <30 days old and before release window (day 13–20)
4. Manual entry

### DATA 3: RBI stance
1. RBI press release page scrape — parses stance keywords + vote split
2. Cache from last run — prompted if no new MPC meeting
3. Manual: 4-option menu (accommodative / neutral / withdrawal / calibrated tightening)

### DATA 4: INR/USD
1. yfinance `USDINR=X` — preferred (provides 30d history)
2. ExchangeRate-API (current only) + log for 30d ago
3. Frankfurter API (current only) + log for 30d ago
4. Manual entry

### DATA 5: US Fed funds rate
1. FRED direct CSV `FEDFUNDS` — no key needed
2. FRED API via `fredapi` — only if `FRED_API_KEY` in `.env`
3. Manual entry

## Safe mode

If >2 manual entries in a run → `safe_mode=True` → warning printed on report. Does not block execution.

## Data confidence scoring

Starts at 5.0. Deduct: manual −1.0, stale −0.5, fallback −0.25. Labels: HIGH / MEDIUM-HIGH / MEDIUM / LOW.

## decision_log.csv

Auto-appended each run. Columns include all inputs, scores, band, recommendation, your_action.
Used for: 30d/60d yield lookback, 30d INR lookback, previous band comparison, CPI cache, cycle peak detection.

## HTML report (--report flag)

`write_html_report()` in Section E. Saves to `reports/run_YYYY-MM-DD.html`. Mobile-friendly responsive layout. Shows all sections: recommendation, veto check, score breakdown with bar charts, raw inputs, data freshness, upcoming events.

## Quarterly maintenance (config.py)

Must update after each MPC/FOMC cycle:
- `NEXT_MPC_DATE`
- `NEXT_CPI_DATE`
- `NEXT_FOMC_DATE`

Current values (as of 2026-05-30):
- MPC: 2026-08-06
- CPI: 2026-07-14
- FOMC: 2026-07-30

Also review yield thresholds if market regime has shifted significantly.
Update `_BHARAT_BOND_SERIES` in config.py when new Bharat Bond ETF series launch.

## Known issues

- All yfinance India yield tickers return 404 (delisted). `countryeconomy.com` is the de-facto primary.
- RBI press release scraper finds 0 MPC links — manual entry is the normal path for stance.
- FRED CPI lags 1–2 months; manual entry is often required for a current reading.
- `stooq.com` may require API key as of 2025.

## Windows-specific

- Logging forces UTF-8 via `io.TextIOWrapper` to avoid `cp1252` `UnicodeEncodeError`.
- Run commands use PowerShell syntax.

## Dependencies

```
requests==2.31.0
beautifulsoup4==4.12.2
pandas==2.1.0
yfinance==0.2.28
fredapi==0.5.1
python-dotenv==1.0.0
lxml==4.9.3
```

Optional: `FRED_API_KEY` in `.env` (only for FRED API fallback on Fed rate).

## Testing

No pytest. Built-in test mode via `--test` flag.
`run_test_mode()` in Section G: hardcoded inputs, `check()` assertions, printed pass/fail.
Always run `python bond_monitor.py --test` after any change to Section D (scoring).
