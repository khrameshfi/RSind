# RSind — IBD-Style Relative Strength Rating for the Indian (NSE) Market

Daily-refreshed 1–99 Relative Strength percentile ranking for NSE-listed stocks,
computed the same way IBD/MarketSmith and the `Fred6725/relative-strength` project
do it for the US market — just re-targeted to India.

Adapted from [Fred6725/relative-strength](https://github.com/Fred6725/relative-strength)
(a fork of [skyte/relative-strength](https://github.com/skyte/relative-strength)),
licensed Apache-2.0.

## What this does differently from the US version

| | Original (Fred6725) | RSind |
|---|---|---|
| Universe | ~6,100 US tickers (NYSE/NASDAQ/NYSE ARCA/BATS) | NSE main-board equities (~2,000, `SERIES == EQ`) pulled from NSE's own `EQUITY_L.csv` |
| Reference / benchmark | `SPY` | `^NSEI` (Nifty 50) — configurable to `^CRSLDX` (Nifty 500) in `config.yaml` |
| Ticker format for Yahoo Finance | dot→dash escaping (`BRK.B`→`BRK-B`) | `.NS` suffix (`RELIANCE`→`RELIANCE.NS`) |
| Data source | Yahoo Finance | Yahoo Finance (same) |

The core RS math (`rs_ranking.py`) is untouched — it's currency- and market-agnostic:

```
strength(closes) = 0.4×Q1 + 0.2×Q2 + 0.2×Q3 + 0.2×Q4   (Q1 = most recent quarter's return)
RS = (1 + strength(stock)) / (1 + strength(reference)) × 100
```

Every stock's RS is computed the same day, then ranked into a 0–99 percentile via `pandas.qcut`.
That percentile is what makes "88" mean something specific: *this stock's momentum, relative to
Nifty 50, is stronger than 88% of the entire ranked NSE universe on this date.*

## Important limitation: this does NOT show up on your TradingView chart automatically

The original US version reaches TradingView via `request.seed()`, which reads from a
specially-registered "Pine Seeds" GitHub repo. **TradingView suspended new Pine Seeds
registrations**, so a fresh repo (this one) cannot be wired into `request.seed()` the
way `Fred6725/rs-log` is. This repo produces the same daily-ranked data, but you'll
consult it as a spreadsheet/table (see below), not as a label on the chart, unless:

- TradingView reopens Pine Seeds registration, or
- you get this specific repo grandfathered in by asking `pine.seeds@tradingview.com`
  (worth a try — no guarantee).

## Setup

1. Create a new GitHub repo and push this folder to it.
2. In the repo's **Settings → Actions → General**, under "Workflow permissions", select
   **"Read and write permissions"** (needed so the daily workflow can commit its output back).
3. That's it — `.github/workflows/update.yml` runs automatically on weekdays at 17:15 IST
   (~1h45m after NSE close), or trigger it manually from the **Actions** tab
   ("Update RSind rankings" → "Run workflow").

## Running locally (optional, e.g. to test before the first scheduled run)

```bash
pip install -r requirements.txt
python rs_data.py       # downloads ~18 months of price history for all NSE stocks + Nifty 50
python rs_ranking.py    # computes RS scores and percentiles, writes output/rs_stocks.csv
```

`rs_data.py` will take a while the first time (batches of 100 tickers via yfinance,
~2,000 tickers total — expect roughly 15–30 minutes). Subsequent runs are the same
cost since we re-pull the full window each time (matches the original project's design;
can be optimized to incremental updates later if needed).

## Output

`output/rs_stocks.csv` — every ranked NSE stock with:

- `Relative Strength` — raw score
- `Percentile` — the 0–99 IBD-style rating (this is the "RS Rating" number)
- `1M/3M/6M_RS_Percentile` — what the percentile was 1/3/6 months ago (momentum-of-momentum)
- `Price`, `MarketCap`, `PctFrom52WkHigh`, `AvgVol10/30/50`

### Reading it in Google Sheets (live, auto-refreshing)

Once pushed to GitHub, paste this into cell A1 of a Google Sheet:

```
=IMPORTDATA("https://raw.githubusercontent.com/<your-username>/RSind/main/output/rs_stocks.csv")
```

Replace `<your-username>` with your GitHub username. This re-pulls the latest file
every time you open the sheet, so it stays current with the daily Actions run.

## Known limitations (carried over from the original, plus a couple of new ones)

- Yahoo Finance close prices aren't always split-adjusted the instant a split happens.
- NSE's site has bot protection; `rs_data.py` does a homepage "warm-up" request first to
  pick up cookies before hitting the CSV archive. This works reliably from a GitHub Actions
  runner; if NSE changes their protection it may need a small patch.
- Recently listed stocks (IPOs within the last ~6 months) are excluded from ranking until
  they have at least 6 months of price history — same threshold the original uses.
- Sector/Industry data from `yfinance .info` is less consistently populated for NSE tickers
  than for US ones; treat those two columns as best-effort.
