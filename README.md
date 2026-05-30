# Bond Market Intelligence Monitor

Indian retail investor tool. Manual run. No database. No scheduler.

## Files

```
bond_monitor/
├── bond_monitor.py      # Everything here
├── config.py            # Thresholds only — edit this quarterly
├── decision_log.csv     # Auto-created on first run
├── logs/                # Auto-created folder
│   └── run_YYYY-MM-DD.txt
├── .env                 # You create this (optional)
└── requirements.txt
```

## Running the script

### Option A — with uv (recommended, auto-installs deps)

Install uv once:
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then run:
```powershell
uv run bond_monitor.py

# test mode (no network, no CSV write)
uv run bond_monitor.py --test
```

uv reads the `# /// script` block at the top of `bond_monitor.py` and installs
all dependencies automatically into an isolated cache. No venv setup needed.

### Option B — with pip (standard)

```powershell
pip install -r requirements.txt
python bond_monitor.py
python bond_monitor.py --test
```

## Optional: FRED API key

Create a `.env` file in this folder:
```
FRED_API_KEY=paste_your_key_here
```

The script runs fully without it. The key only enables a secondary Fed funds rate
source (FRED API). The primary source (FRED direct CSV) needs no key.

## Updating quarterly

Edit `config.py` to update:
- `NEXT_MPC_DATE`
- `NEXT_CPI_DATE`
- `NEXT_FOMC_DATE`
