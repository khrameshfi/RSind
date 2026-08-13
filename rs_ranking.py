#!/usr/bin/env python
# rs_ranking.py — RSind (NSE / Indian market adaptation)
#
# Adapted from Fred6725/relative-strength (fork of skyte/relative-strength), Apache-2.0 license.
# Core RS math is untouched — it's currency/market agnostic. What changed is only the
# universe (NSE instead of NASDAQ/NYSE) and reference ticker (^NSEI instead of SPY),
# both handled upstream in rs_data.py.

import os
import json
import pandas as pd
from rs_data import cfg, read_json, REFERENCE_TICKER

DIR = os.path.dirname(os.path.realpath(__file__))
pd.set_option('display.max_rows', None)
pd.set_option('display.width', None)
pd.set_option('display.max_columns', None)

PRICE_DATA = os.path.join(DIR, "data", "price_history.json")
MIN_PERCENTILE = cfg("MIN_PERCENTILE") or 0
TICKER_INFO_FILE = os.path.join(DIR, "data_persist", "ticker_info.json")
TICKER_INFO_DICT = read_json(TICKER_INFO_FILE)

if not os.path.exists(os.path.join(DIR, 'output')):
    os.makedirs(os.path.join(DIR, 'output'))

TITLE_RANK = "Rank"
TITLE_TICKER = "Ticker"
TITLE_SECTOR = "Sector"
TITLE_INDUSTRY = "Industry"
TITLE_RS = "Relative Strength"
TITLE_PERCENTILE = "Percentile"
TITLE_1M = "1M_RS_Percentile"
TITLE_3M = "3M_RS_Percentile"
TITLE_6M = "6M_RS_Percentile"
TITLE_PRICE = "Price"
TITLE_MKTCAP = "MarketCap"
TITLE_PCT_52WH = "PctFrom52WkHigh"
TITLE_AVGVOL10 = "AvgVol10"
TITLE_AVGVOL30 = "AvgVol30"
TITLE_AVGVOL50 = "AvgVol50"

# ── RS calculation (identical to the original — this is the IBD-style formula) ─────────

def relative_strength(closes: pd.Series, closes_ref: pd.Series):
    rs_stock = strength(closes)
    rs_ref = strength(closes_ref)
    rs = (1 + rs_stock) / (1 + rs_ref) * 100
    return int(rs * 100) / 100

def strength(closes: pd.Series):
    """Yearly performance, most recent quarter weighted double."""
    try:
        q1 = quarters_perf(closes, 1)
        q2 = quarters_perf(closes, 2)
        q3 = quarters_perf(closes, 3)
        q4 = quarters_perf(closes, 4)
        return 0.4 * q1 + 0.2 * q2 + 0.2 * q3 + 0.2 * q4
    except Exception:
        return 0

def quarters_perf(closes: pd.Series, n):
    length = min(len(closes), n * int(252 / 4))
    prices = closes.tail(length)
    pct_chg = prices.pct_change().dropna()
    perf_cum = (pct_chg + 1).cumprod() - 1
    return perf_cum.tail(1).item()

# ── Market data helpers ───────────────────────────────────────────────────────

def avg_volume(candles, days):
    try:
        vols = [c["volume"] for c in candles[-days:] if c.get("volume") is not None]
        return int(sum(vols) / len(vols)) if vols else None
    except Exception:
        return None

def safe_info(ticker, field):
    try:
        return TICKER_INFO_DICT[ticker]["info"].get(field)
    except (KeyError, TypeError):
        return None

def pct_from_52wk_high(price, high):
    try:
        if price and high and high > 0:
            return round((price / high - 1) * 100, 2)
        return None
    except Exception:
        return None

# ── TradingView seed-format CSV (kept for future use / Pine Seeds if it ever reopens) ──

def generate_tradingview_csv(percentile_values, first_rs_values):
    import datetime
    lines = []
    trading_days = 0
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    for percentile in sorted(percentile_values):
        rs_value = first_rs_values[percentile]
        for _ in range(5):
            trading_date = yesterday - datetime.timedelta(days=trading_days)
            date_str = trading_date.strftime("%Y%m%dT")
            lines.append(f"{date_str},0,1000,0,{rs_value},0\n")
            trading_days += 1
    return ''.join(reversed(lines))

# ── Compact price history for the dashboard's hover chart ─────────────────────
# TradingView's free embeddable widget does not serve NSE data (it answers
# "This symbol is only available on TradingView" for every Indian ticker), so the
# dashboard draws its own chart and needs the bars to do it.
#
# ~12 months of daily OHLC per ticker, aligned to the Nifty 50's session dates so
# every series shares one x-axis. Written as one file per starting letter: the
# whole set is ~12 MB, but the dashboard only ever fetches the shard for the
# ticker being hovered (~450 KB, ~160 KB gzipped), so nothing stalls on load.

HISTORY_SESSIONS = 260   # ~12 months of NSE trading sessions
HISTORY_MIN_BARS = 40    # don't bother charting anything shorter
HISTORY_DIR = os.path.join(DIR, "output", "history")

def _round_price(v):
    """Precision scaled to price size — a ₹3,899 stock doesn't need paise on a
    preview chart, and dropping them meaningfully shrinks the files. Kept at four
    significant figures so candle bodies never collapse to nothing."""
    if v >= 1000:
        return round(v)
    if v >= 100:
        return round(v, 1)
    return round(v, 2)

def _shard_of(ticker):
    """Tickers starting with a digit or symbol (3MINDIA, 5PAISA, 63MOONS) go to '_'."""
    c = ticker[0].upper()
    return c if "A" <= c <= "Z" else "_"

def _series_from(candles, pos, n):
    """OHLC laid onto the reference calendar. Returns (start_index, o, h, l, c)."""
    o = [None] * n; h = [None] * n; l = [None] * n; c = [None] * n
    for cd in candles:
        i = pos.get(cd.get("datetime"))
        if i is None or cd.get("close") is None:
            continue
        close = _round_price(float(cd["close"]))
        c[i] = close
        o[i] = _round_price(float(cd["open"])) if cd.get("open") is not None else close
        h[i] = _round_price(float(cd["high"])) if cd.get("high") is not None else close
        l[i] = _round_price(float(cd["low"]))  if cd.get("low")  is not None else close

    # Forward-fill missing sessions as flat bars, then drop the leading empty run
    # (stocks listed part-way through the window simply start where they start).
    first, last = None, None
    for i in range(n):
        if c[i] is None:
            if last is not None:
                c[i] = o[i] = h[i] = l[i] = last
        else:
            last = c[i]
            if first is None:
                first = i
    if first is None:
        return None
    return first, o[first:], h[first:], l[first:], c[first:]

def build_history(price_data):
    ref_candles = price_data[REFERENCE_TICKER]["candles"]
    ref_dates = [c["datetime"] for c in ref_candles][-HISTORY_SESSIONS:]
    pos = {d: i for i, d in enumerate(ref_dates)}
    n = len(ref_dates)

    ref = _series_from(ref_candles, pos, n)
    ref_block = {"s": ref[0], "c": ref[4]} if ref else None

    shards = {}
    total = 0
    for ticker, blob in price_data.items():
        if ticker == REFERENCE_TICKER:
            continue
        built = _series_from(blob.get("candles", []), pos, n)
        if built is None or len(built[4]) < HISTORY_MIN_BARS:
            continue
        first, o, h, l, c = built
        shards.setdefault(_shard_of(ticker), {})[ticker] = {
            "s": first, "o": o, "h": h, "l": l, "c": c
        }
        total += 1

    # The benchmark line is drawn on every chart, so it rides along in each shard
    # (260 numbers — a rounding error next to the ticker data).
    files = {
        key: {"dates": ref_dates, "ref": REFERENCE_TICKER, "refc": ref_block, "series": series}
        for key, series in shards.items()
    }
    return files, total, n

def write_history(price_data):
    try:
        files, total, n = build_history(price_data)
    except Exception as e:
        print(f"⚠ Could not build price history: {e}")
        return

    os.makedirs(HISTORY_DIR, exist_ok=True)

    # Clear out shards from a previous run that no longer have any tickers, plus
    # the single-file layout this replaced, so nothing stale gets served.
    legacy = os.path.join(DIR, "output", "history.json")
    if os.path.exists(legacy):
        os.remove(legacy)
    for name in os.listdir(HISTORY_DIR):
        if name.endswith(".json") and name[:-5] not in files:
            os.remove(os.path.join(HISTORY_DIR, name))

    written = 0
    for key, payload in files.items():
        with open(os.path.join(HISTORY_DIR, f"{key}.json"), "w") as f:
            json.dump(payload, f, separators=(",", ":"))
        written += os.path.getsize(os.path.join(HISTORY_DIR, f"{key}.json"))

    print(f"✓ output/history/: {total} tickers × up to {n} sessions "
          f"across {len(files)} shards ({written / 1e6:.1f} MB total, "
          f"~{written / len(files) / 1e6:.2f} MB per hover).")

# ── Rankings ──────────────────────────────────────────────────────────────────

def rankings():
    price_data = read_json(PRICE_DATA)
    rows = []
    stock_rs = {}
    ref = price_data.get(REFERENCE_TICKER)
    if ref is None:
        raise RuntimeError(f"Reference ticker {REFERENCE_TICKER} missing from price_history.json — "
                            f"check that rs_data.py ran successfully.")

    for ticker in price_data:
        if ticker == REFERENCE_TICKER:
            continue
        try:
            candles = price_data[ticker]["candles"]
            closes = [c["close"] for c in candles]
            closes_ref = [c["close"] for c in ref["candles"]]

            # Need at least 6 months of history for a meaningful reading
            if len(closes) >= 6 * 20:
                cs = pd.Series(closes)
                csr = pd.Series(closes_ref)
                rs = relative_strength(cs, csr)
                m = 20
                rs1m = relative_strength(cs.head(-1 * m), csr.head(-1 * m)) if len(cs) > m else rs
                rs3m = relative_strength(cs.head(-3 * m), csr.head(-3 * m)) if len(cs) > 3 * m else rs
                rs6m = relative_strength(cs.head(-6 * m), csr.head(-6 * m)) if len(cs) > 6 * m else rs

                price = round(closes[-1], 2) if closes[-1] else None
                av10 = avg_volume(candles, 10)
                av30 = avg_volume(candles, 30)
                av50 = avg_volume(candles, 50)
                mktcap = safe_info(ticker, "marketCap")
                wk52h = safe_info(ticker, "fiftyTwoWeekHigh")
                pct52h = pct_from_52wk_high(price, wk52h)
                sector = price_data[ticker].get("sector", "unknown")
                industry = price_data[ticker].get("industry", "unknown")

                rows.append((
                    0, ticker, sector, industry,
                    rs, 0, rs1m, rs3m, rs6m,
                    price, mktcap, pct52h, av10, av30, av50
                ))
                stock_rs[ticker] = rs
        except KeyError:
            print(f'Ticker {ticker} has corrupted data — skipped.')

    cols = [
        TITLE_RANK, TITLE_TICKER, TITLE_SECTOR, TITLE_INDUSTRY,
        TITLE_RS, TITLE_PERCENTILE, TITLE_1M, TITLE_3M, TITLE_6M,
        TITLE_PRICE, TITLE_MKTCAP, TITLE_PCT_52WH,
        TITLE_AVGVOL10, TITLE_AVGVOL30, TITLE_AVGVOL50
    ]
    df = pd.DataFrame(rows, columns=cols)

    if df.empty:
        print("⚠ No tickers with enough history to rank yet.")
        return df

    df[TITLE_PERCENTILE] = pd.qcut(df[TITLE_RS], 100, labels=False, duplicates="drop")
    df[TITLE_1M] = pd.qcut(df[TITLE_1M], 100, labels=False, duplicates="drop")
    df[TITLE_3M] = pd.qcut(df[TITLE_3M], 100, labels=False, duplicates="drop")
    df[TITLE_6M] = pd.qcut(df[TITLE_6M], 100, labels=False, duplicates="drop")

    df = df.sort_values([TITLE_RS], ascending=False)
    df[TITLE_RANK] = list(range(1, len(df) + 1))

    out_tickers_count = int((df[TITLE_PERCENTILE] >= MIN_PERCENTILE).sum())
    df = df.head(out_tickers_count)

    # TradingView seed-format file (RSRATING.csv) — dormant until/unless Pine Seeds
    # registration reopens, or you self-host and read it a different way.
    percentile_values = [98, 89, 69, 49, 29, 9, 1]
    first_rs_values = {}
    for percentile in percentile_values:
        matching = df[df[TITLE_PERCENTILE] == percentile]
        if matching.empty:
            available = df[TITLE_PERCENTILE].dropna().unique()
            if len(available) == 0:
                continue
            nearest = min(available, key=lambda x: abs(x - percentile))
            matching = df[df[TITLE_PERCENTILE] == nearest]
        if not matching.empty:
            first_rs_values[percentile] = matching.iloc[0][TITLE_RS]

    if len(first_rs_values) == len(percentile_values):
        tv_csv = generate_tradingview_csv(percentile_values, first_rs_values)
        with open(os.path.join(DIR, "output", "RSRATING.csv"), "w") as f:
            f.write(tv_csv)
        print("✓ RSRATING.csv generated (seed-format, for future TradingView use).")
    else:
        print("⚠ Not enough percentile spread yet to generate RSRATING.csv (need more tickers/history).")

    df.to_csv(os.path.join(DIR, "output", "rs_stocks.csv"), index=False)
    print(f"✓ rs_stocks.csv: {len(df)} NSE tickers ranked vs. {REFERENCE_TICKER}.")

    write_history(price_data)

    return df

def main():
    df = rankings()
    if not df.empty:
        print(df.head(20))
    print("***\nOutput written to the output/ folder.\n***")

if __name__ == "__main__":
    main()
