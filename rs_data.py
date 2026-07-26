#!/usr/bin/env python
# rs_data.py — RSind (NSE / Indian market adaptation)
#
# Adapted from Fred6725/relative-strength (fork of skyte/relative-strength), Apache-2.0 license.
# Changes vs. the original US version:
#   1. Ticker universe pulled from NSE's own equity list (EQUITY_L.csv) instead of the
#      NASDAQ Trader file / Wikipedia S&P/Nasdaq-100 tables.
#   2. Tickers suffixed with ".NS" for Yahoo Finance instead of the US dot->dash escaping.
#   3. Reference ticker defaults to "^NSEI" (Nifty 50) instead of "SPY".
#   4. TD Ameritrade fallback removed (not relevant outside the US).
#
# NOTE: NSE's website sits behind bot protection — a plain requests.get() to the archive
# URL will usually return 403. We first hit the public homepage to pick up cookies, then
# reuse that session for the CSV download. This is the standard workaround for NSE scraping.
# It reliably works from a normal residential/datacenter IP (e.g. a GitHub Actions runner);
# it may still occasionally need a retry if NSE is rate-limiting.

import requests
import json
import time
import datetime as dt
import os
import yaml
import yfinance as yf
import pandas as pd
import re
from io import StringIO
from time import sleep
from datetime import date, datetime

DIR = os.path.dirname(os.path.realpath(__file__))

if not os.path.exists(os.path.join(DIR, 'data')):
    os.makedirs(os.path.join(DIR, 'data'))

# ── Config ──────────────────────────────────────────────────────────────────

try:
    with open(os.path.join(DIR, 'config.yaml'), 'r') as stream:
        config = yaml.safe_load(stream)
except FileNotFoundError:
    config = None
except yaml.YAMLError as exc:
    print(exc)
    config = None

def cfg(key):
    try:
        return config[key]
    except Exception:
        return None

def read_json(json_file):
    if not os.path.exists(json_file):
        return {}
    with open(json_file, "r", encoding="utf-8") as fp:
        return json.load(fp)

# ── Constants ────────────────────────────────────────────────────────────────

PRICE_DATA_FILE = os.path.join(DIR, "data", "price_history.json")
REFERENCE_TICKER = cfg("REFERENCE_TICKER") or "^NSEI"
ALL_STOCKS = cfg("USE_ALL_LISTED_STOCKS")
TICKER_INFO_FILE = os.path.join(DIR, "data_persist", "ticker_info.json")
TICKER_INFO_DICT = read_json(TICKER_INFO_FILE)
REF_TICKER = {
    "ticker": REFERENCE_TICKER,
    "sector": "--- Reference ---",
    "industry": "--- Reference ---",
    "universe": "--- Reference ---"
}

UNKNOWN = "unknown"
BATCH_SIZE = 100  # yfinance bulk-download batch size (speed vs. reliability)

NSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

NSE_EQUITY_LIST_URLS = [
    "https://nsearchives.nseindia.com/content/equity/EQUITY_L.csv",
    "https://archives.nseindia.com/content/equity/EQUITY_L.csv",
]

# ── Ticker list retrieval (NSE) ─────────────────────────────────────────────

def get_nse_tickers(universe="NSE"):
    """
    Downloads NSE's master equity list. Columns:
    SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE
    We keep SERIES == 'EQ' (regular equity, excludes some illiquid/rights/pref-share series).
    """
    session = requests.Session()
    session.headers.update(NSE_HEADERS)

    # Warm up: hitting the homepage first sets the cookies NSE's CDN expects.
    try:
        session.get("https://www.nseindia.com", timeout=15)
    except Exception as e:
        print(f"  Warning: NSE homepage warm-up failed ({e}) — continuing anyway.")

    df = None
    for url in NSE_EQUITY_LIST_URLS:
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            df = pd.read_csv(StringIO(r.text))
            break
        except Exception as e:
            print(f"  Could not fetch {url}: {e}")

    if df is None:
        raise RuntimeError("Could not download NSE equity list from any known URL.")

    df.columns = [c.strip() for c in df.columns]
    symbol_col = next(c for c in df.columns if "SYMBOL" in c.upper())
    series_col = next((c for c in df.columns if "SERIES" in c.upper()), None)

    if series_col:
        df = df[df[series_col].astype(str).str.strip() == "EQ"]

    secs = {}
    for _, row in df.iterrows():
        symbol = str(row[symbol_col]).strip()
        if not symbol or not re.match(r'^[A-Z0-9&\-]+$', symbol):
            continue
        secs[symbol] = {
            "ticker": symbol,
            "sector": UNKNOWN,
            "industry": UNKNOWN,
            "universe": universe,
        }
    print(f"  NSE equity list: {len(secs)} tickers loaded.")
    return secs

def get_resolved_securities():
    tickers = {REFERENCE_TICKER: REF_TICKER}
    try:
        tickers.update(get_nse_tickers())
    except Exception as e:
        print(f"⚠ Could not load NSE ticker list: {e}")
    return tickers

# ── File helpers ─────────────────────────────────────────────────────────────

def write_to_file(dict_data, file):
    with open(file, "w", encoding='utf8') as fp:
        json.dump(dict_data, fp, ensure_ascii=False)

def write_price_history_file(tickers_dict):
    write_to_file(tickers_dict, PRICE_DATA_FILE)

def write_ticker_info_file(info_dict):
    write_to_file(info_dict, TICKER_INFO_FILE)

def enrich_ticker_data(ticker_response, security):
    ticker_response["sector"] = security["sector"]
    ticker_response["industry"] = security["industry"]
    ticker_response["universe"] = security["universe"]

def escape_ticker(ticker):
    """NSE tickers -> Yahoo Finance symbols. Reference index already in Yahoo format (^NSEI)."""
    if ticker.startswith("^"):
        return ticker
    return f"{ticker}.NS"

# ── Ticker info (sector/industry/market data) ────────────────────────────────

def get_info_from_dict(d, key):
    return d[key] if key in d else "n/a"

def _safe_float(d, key):
    try:
        v = d.get(key) if hasattr(d, 'get') else getattr(d, key, None)
        return float(v) if v is not None else None
    except (TypeError, ValueError, AttributeError):
        return None

def load_ticker_info(ticker, info_dict):
    escaped = escape_ticker(ticker)
    result = {
        "industry": "n/a", "sector": "n/a",
        "marketCap": None, "floatShares": None,
        "fiftyTwoWeekHigh": None, "fiftyTwoWeekLow": None,
    }
    try:
        t = yf.Ticker(escaped)
        try:
            fi = t.fast_info
            result["marketCap"] = _safe_float(fi, "marketCap")
            result["fiftyTwoWeekHigh"] = _safe_float(fi, "yearHigh")
            result["fiftyTwoWeekLow"] = _safe_float(fi, "yearLow")
        except Exception:
            pass
        try:
            info_obj = t.info
            result["industry"] = get_info_from_dict(info_obj, "industry")
            result["sector"] = get_info_from_dict(info_obj, "sector")
            if result["marketCap"] is None:
                result["marketCap"] = _safe_float(info_obj, "marketCap")
        except Exception:
            pass
    except Exception:
        pass
    info_dict[ticker] = {"info": result}

def needs_refresh(ticker, info_dict):
    try:
        info = info_dict[ticker]["info"]
        return "marketCap" not in info
    except (KeyError, TypeError):
        return True

# ── Core: batch price download from Yahoo ─────────────────────────────────────

def parse_batch_download(df, batch_tickers):
    result = {}
    if df is None or df.empty:
        return result

    if not isinstance(df.columns, pd.MultiIndex):
        ticker = batch_tickers[0]
        df_t = df.dropna(subset=["Close"]) if "Close" in df.columns else df
        candles = []
        for ts, row in df_t.iterrows():
            candles.append({
                "open": float(row["Open"]) if not pd.isna(row.get("Open")) else None,
                "close": float(row["Close"]),
                "low": float(row["Low"]) if not pd.isna(row.get("Low")) else None,
                "high": float(row["High"]) if not pd.isna(row.get("High")) else None,
                "volume": float(row["Volume"]) if not pd.isna(row.get("Volume")) else None,
                "datetime": int(ts.timestamp()),
            })
        if candles:
            result[ticker] = candles
        return result

    df_swapped = df.swaplevel(axis=1)
    for original in batch_tickers:
        try:
            df_t = df_swapped[original] if original in df_swapped.columns.get_level_values(0) else None
            if df_t is None or df_t.empty:
                continue
            df_t = df_t.dropna(subset=["Close"])
            if df_t.empty:
                continue
            candles = []
            for ts, row in df_t.iterrows():
                candles.append({
                    "open": float(row["Open"]) if "Open" in row and not pd.isna(row["Open"]) else None,
                    "close": float(row["Close"]),
                    "low": float(row["Low"]) if "Low" in row and not pd.isna(row["Low"]) else None,
                    "high": float(row["High"]) if "High" in row and not pd.isna(row["High"]) else None,
                    "volume": float(row["Volume"]) if "Volume" in row and not pd.isna(row["Volume"]) else None,
                    "datetime": int(ts.timestamp()),
                })
            if candles:
                result[original] = candles
        except (KeyError, TypeError):
            pass
    return result

def load_prices_from_yahoo(securities):
    print("*** Loading NSE stocks from Yahoo Finance (batch mode) ***")
    today = date.today()
    start_date = today - dt.timedelta(days=1 * 365 + 183)  # 18 months of data
    end_date = today

    securities_list = list(securities)
    tickers_dict = {}
    failed_tickers = []

    ref_sec = next((s for s in securities_list if s["ticker"] == REFERENCE_TICKER), None)
    non_ref = [s for s in securities_list if s["ticker"] != REFERENCE_TICKER]
    ordered = ([ref_sec] if ref_sec else []) + non_ref

    batches, batch_securities = [], []
    for sec in ordered:
        batch_securities.append(sec)
        if len(batch_securities) >= BATCH_SIZE:
            batches.append(batch_securities)
            batch_securities = []
    if batch_securities:
        batches.append(batch_securities)

    total_batches = len(batches)
    global_start = time.time()

    for batch_idx, batch in enumerate(batches):
        batch_start = time.time()
        original_tickers = [s["ticker"] for s in batch]
        yahoo_tickers = [escape_ticker(t) for t in original_tickers]
        yahoo_to_original = dict(zip(yahoo_tickers, original_tickers))

        print(f"\n── Batch {batch_idx + 1}/{total_batches}: {len(batch)} tickers ──")
        try:
            df = yf.download(
                tickers=yahoo_tickers,
                start=start_date,
                end=end_date,
                auto_adjust=True,
                progress=False,
                threads=True,
                ignore_tz=True,
            )
        except Exception as e:
            print(f"Batch {batch_idx + 1} failed: {e}")
            failed_tickers.extend(original_tickers)
            continue

        # Re-key the parsed candles from Yahoo symbol back to plain NSE symbol
        candles_by_yahoo = parse_batch_download(df, yahoo_tickers)
        for yahoo_ticker, candles in candles_by_yahoo.items():
            original = yahoo_to_original.get(yahoo_ticker, yahoo_ticker)
            sec = next((s for s in batch if s["ticker"] == original), None)
            if sec is None:
                continue
            ticker_data = {"candles": candles}
            enrich_ticker_data(ticker_data, sec)

            if original not in TICKER_INFO_DICT or needs_refresh(original, TICKER_INFO_DICT):
                try:
                    load_ticker_info(original, TICKER_INFO_DICT)
                except Exception as e:
                    print(f"  Could not load info for {original}: {e}")
            try:
                ticker_data["industry"] = TICKER_INFO_DICT[original]["info"]["industry"]
                ticker_data["sector"] = TICKER_INFO_DICT[original]["info"]["sector"]
            except (KeyError, TypeError):
                ticker_data["industry"] = "Unknown"

            tickers_dict[original] = ticker_data

        for t in original_tickers:
            if t not in tickers_dict:
                failed_tickers.append(t)

        batch_elapsed = time.time() - batch_start
        total_elapsed = time.time() - global_start
        avg_batch_time = total_elapsed / (batch_idx + 1)
        remaining_s = avg_batch_time * (total_batches - batch_idx - 1)
        tickers_ok = sum(1 for t in original_tickers if t in tickers_dict)
        print(f"  {tickers_ok}/{len(batch)} tickers OK | Batch time: {batch_elapsed:.1f}s | "
              f"Remaining: ~{int(remaining_s // 60)}m {int(remaining_s % 60)}s")

        if (batch_idx + 1) % 10 == 0:
            print(f"  → Saving intermediate results ({len(tickers_dict)} tickers so far)...")
            write_price_history_file(tickers_dict)
            write_ticker_info_file(TICKER_INFO_DICT)

    write_price_history_file(tickers_dict)
    write_ticker_info_file(TICKER_INFO_DICT)

    total_time = time.time() - global_start
    print(f"\n✓ Done: {len(tickers_dict)} tickers downloaded in {int(total_time // 60)}m {int(total_time % 60)}s")
    if failed_tickers:
        unique_failed = list(dict.fromkeys(failed_tickers))
        print(f"✗ {len(unique_failed)} tickers had no data: {unique_failed[:20]}{'...' if len(unique_failed) > 20 else ''}")
        with open(os.path.join(DIR, "failed_tickers.txt"), "w") as f:
            f.write("\n".join(unique_failed))

    return tickers_dict

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    securities = get_resolved_securities()
    load_prices_from_yahoo(list(securities.values()))
    write_ticker_info_file(TICKER_INFO_DICT)

if __name__ == "__main__":
    main()
