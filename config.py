# ── Yield signal thresholds ────────────────────────────────────────────────
YIELD_STRONG_BPS    = 25
YIELD_MODERATE_BPS  = 10
YIELD_VELOCITY_BPS  = 20
YIELD_MIN_VALID     = 5.5
YIELD_MAX_VALID     = 9.5
YIELD_MIN_ROWS      = 60

# ── CPI thresholds ─────────────────────────────────────────────────────────
CPI_HIGH            = 6.0
CPI_LOW             = 4.5
CPI_MIN_VALID       = 2.0
CPI_MAX_VALID       = 12.0
CPI_UPDATE_WINDOW   = (13, 20)  # Day range post-release

# ── Veto thresholds ────────────────────────────────────────────────────────
INR_VETO_PCT        = 3.0
INR_WARN_PCT        = 2.5
INR_MIN_VALID       = 70.0
INR_MAX_VALID       = 100.0

# ── Score bands ────────────────────────────────────────────────────────────
SCORE_STRONG        = 6.0
SCORE_MODERATE      = 3.0
SCORE_WEAK          = 1.0

# ── Cycle stage thresholds (bps fallen from peak) ──────────────────────────
CYCLE_EARLY_MAX     = 50
CYCLE_MID_MAX       = 100

# ── Safe mode ──────────────────────────────────────────────────────────────
MAX_MANUAL_ENTRIES  = 2

# ── Upcoming events — update when they pass ────────────────────────────────
NEXT_MPC_DATE       = "2026-08-06"
NEXT_CPI_DATE       = "2026-07-14"
NEXT_FOMC_DATE      = "2026-07-30"

# ── Data freshness thresholds (days) ──────────────────────────────────────
DATA_FRESHNESS = {
    "yield":  2,
    "cpi":    45,
    "stance": 60,
    "inr":    2,
    "fed":    45,
}

# ── Instruments by zone and cycle stage ───────────────────────────────────
# ETF list: add new Bharat Bond series here as Edelweiss launches them
_BHARAT_BOND_SERIES = [2030, 2031, 2032, 2033]


def _build_instruments() -> dict:
    from datetime import date as _d
    y = _d.today().year
    long_etfs   = [f"Bharat Bond ETF {e}" for e in _BHARAT_BOND_SERIES if e - y >= 6]
    medium_etfs = [f"Bharat Bond ETF {e}" for e in _BHARAT_BOND_SERIES if 3 <= e - y < 6]
    if not long_etfs:
        # All known series < 6 years away — use the two longest still active
        long_etfs = [f"Bharat Bond ETF {e}"
                     for e in _BHARAT_BOND_SERIES if e > y][-2:]
    return {
        "long_early": ["10Y G-Sec (RBI Retail Direct)"] + long_etfs[-1:],
        "long_mid":   long_etfs[-1:] + medium_etfs[:1],
        "long_late":  medium_etfs[:1] or long_etfs[-1:],
        "medium":     medium_etfs[:1] + ["SDL 5-7Y"],
        "short":      ["T-Bills", "Liquid Fund"],
    }


INSTRUMENTS = _build_instruments()
