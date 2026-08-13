#!/usr/bin/env python
"""
backtest.py — RSind swing-trading backtest.

Strategy under test
-------------------
  Entry      RS percentile crosses above the trigger (default 80) on day D.
             Filled at day D+1's OPEN. No overlapping trades in the same name;
             a fresh cross after an exit re-enters.
  Stop       Fixed % below entry (default 5%), moved to breakeven once price
             trades 1 ATR(14) above entry (ATR measured on the signal day).
  Exit       Daily close below the EMA, filled at the next open.
             Three variants are run side by side: EMA 10, 20 and 50.
  Universe   RS is ranked across the WHOLE NSE list, but only names above a
             market-cap floor (default Rs 2,000 Cr) and a liquidity floor
             (default Rs 5 Cr median 50-day turnover) are tradeable.
  Sizing     Fixed rupee amount per trade, unlimited concurrent positions.
             Peak capital deployed is reported rather than capped.

Known limitations - read these before trusting the numbers
----------------------------------------------------------
  * Survivorship bias. The universe comes from NSE's CURRENT equity list, so
    companies delisted, merged or suspended during the test window are absent.
    Their trades - disproportionately losers - never appear. Results are
    optimistic and this cannot be fixed with free data.
  * Market cap is reconstructed as (today's share count x that day's price).
    Share issuance and buybacks during the window are not modelled.
  * No slippage, brokerage, STT or impact cost. Add roughly 0.3-0.5% round-trip
    for a realistic Indian smallcap fill.
  * Prices are split- and dividend-adjusted, so entry prices are not what you
    would have seen on screen at the time; returns are correct, levels are not.
"""

import argparse
import json
import os
import time

import numpy as np
import pandas as pd
import yfinance as yf

from rs_data import (get_nse_tickers, escape_ticker, read_json,
                     REFERENCE_TICKER, TICKER_INFO_FILE)

DIR = os.path.dirname(os.path.realpath(__file__))
OUT_DIR = os.path.join(DIR, "output", "backtest")

CRORE = 1e7
QUARTER = 63          # trading days, matching rs_ranking.py's int(252/4)


# -- Data ---------------------------------------------------------------------

def download_panel(symbols, period, batch_size=100):
    """Daily OHLCV for every symbol, split/dividend adjusted."""
    frames = {}
    total = (len(symbols) + batch_size - 1) // batch_size

    for b in range(total):
        chunk = symbols[b * batch_size:(b + 1) * batch_size]
        yahoo = [escape_ticker(s) for s in chunk]
        back = dict(zip(yahoo, chunk))

        df = None
        for attempt in range(3):
            try:
                df = yf.download(yahoo, period=period, interval="1d",
                                 auto_adjust=True, progress=False,
                                 group_by="ticker", threads=True)
                break
            except Exception as e:
                print(f"  batch {b + 1}/{total} attempt {attempt + 1} failed: {e}")
                time.sleep(5)
        if df is None or df.empty:
            print(f"  batch {b + 1}/{total}: no data, skipped.")
            continue

        if isinstance(df.columns, pd.MultiIndex):
            for y in yahoo:
                if y not in df.columns.get_level_values(0):
                    continue
                sub = df[y].dropna(subset=["Close"])
                if len(sub):
                    frames[back[y]] = sub
        else:
            sub = df.dropna(subset=["Close"])
            if len(sub):
                frames[back[yahoo[0]]] = sub

        print(f"  batch {b + 1}/{total}: {len(frames)} series so far", flush=True)
        time.sleep(1)

    return frames


def panel(frames, field):
    return pd.DataFrame({t: d[field] for t, d in frames.items()}).sort_index()


# -- Indicators ---------------------------------------------------------------

def rs_percentile(close, ref_close, min_names=100):
    """
    IBD-style RS, identical maths to rs_ranking.py, evaluated on every date.

      strength = 0.4*r(1q) + 0.2*r(2q) + 0.2*r(3q) + 0.2*r(4q)
      RS       = (1 + strength_stock) / (1 + strength_index) * 100

    The live dashboard buckets RS with pd.qcut(..., 100). A cross-sectional
    percentile rank gives the same 0-99 buckets for distinct values and is far
    faster over ~1,000 dates, so that is what is used here.
    """
    def perf(s, q):
        return s / s.shift(QUARTER * q) - 1

    strength = (0.4 * perf(close, 1) + 0.2 * perf(close, 2)
                + 0.2 * perf(close, 3) + 0.2 * perf(close, 4))
    ref_strength = (0.4 * perf(ref_close, 1) + 0.2 * perf(ref_close, 2)
                    + 0.2 * perf(ref_close, 3) + 0.2 * perf(ref_close, 4))

    rs = (1 + strength).div(1 + ref_strength, axis=0) * 100
    rs = rs.where(close.notna())

    pct = np.ceil(rs.rank(axis=1, pct=True, method="first") * 100) - 1
    pct = pct.clip(0, 99)
    # A percentile is meaningless on a day with only a handful of listed names.
    return pct.where(rs.notna().sum(axis=1) >= min_names, np.nan)


def atr(high, low, close, period=14):
    """Wilder's ATR, column-wise."""
    prev = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev).abs(), (low - prev).abs()])
    tr = tr.groupby(level=0).max().reindex(close.index)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


# -- Trade engine -------------------------------------------------------------

def simulate(signals, o, h, l, c, ema, atr_v, stop_pct, atr_mult):
    """
    Walks each ticker independently. Within a day the order is:
        1. gap check, then intraday stop
        2. breakeven upgrade if the day traded 1 ATR above entry
        3. close-below-EMA, which exits at the NEXT open
    Checking the stop before upgrading it is deliberate - we never know the
    intraday sequence, so we take the pessimistic reading.
    """
    trades = []
    dates = c.index

    for tk in signals.columns:
        sig = np.flatnonzero(signals[tk].values)
        if not len(sig):
            continue

        O, H, L, C = o[tk].values, h[tk].values, l[tk].values, c[tk].values
        E, A = ema[tk].values, atr_v[tk].values
        n = len(C)
        busy_until = -1

        for s in sig:
            entry_i = s + 1
            if entry_i >= n or entry_i <= busy_until:
                continue
            entry = O[entry_i]
            signal_atr = A[s]
            if not np.isfinite(entry) or entry <= 0 or not np.isfinite(signal_atr):
                continue

            stop = entry * (1 - stop_pct)
            be_level = entry + atr_mult * signal_atr
            at_be = False
            exit_i = exit_px = None
            reason = None
            pending_ema_exit = False
            mae = mfe = 0.0

            for j in range(entry_i, n):
                if not np.isfinite(C[j]):
                    continue

                if pending_ema_exit:
                    exit_i, exit_px, reason = j, O[j], "ema"
                    break

                if np.isfinite(O[j]) and O[j] <= stop:          # gapped through
                    exit_i, exit_px, reason = j, O[j], "gap"
                    break
                if np.isfinite(L[j]) and L[j] <= stop:
                    exit_i, exit_px, reason = j, stop, "be" if at_be else "stop"
                    break

                if np.isfinite(H[j]):
                    mfe = max(mfe, H[j] / entry - 1)
                if np.isfinite(L[j]):
                    mae = min(mae, L[j] / entry - 1)

                if not at_be and np.isfinite(H[j]) and H[j] >= be_level:
                    stop = max(stop, entry)
                    at_be = True

                if np.isfinite(E[j]) and C[j] < E[j]:
                    pending_ema_exit = True

            if exit_i is None:                                   # still open
                exit_i, exit_px, reason = n - 1, C[n - 1], "open"

            busy_until = exit_i
            trades.append({
                "ticker": tk,
                "signal_date": dates[s].date(),
                "entry_date": dates[entry_i].date(),
                "exit_date": dates[exit_i].date(),
                "entry": round(float(entry), 2),
                "exit": round(float(exit_px), 2),
                "reason": reason,
                "bars": int(exit_i - entry_i),
                "ret_pct": round((exit_px / entry - 1) * 100, 2),
                "mfe_pct": round(mfe * 100, 2),
                "mae_pct": round(mae * 100, 2),
                "reached_be": bool(at_be),
            })

    return pd.DataFrame(trades)


# -- Reporting ----------------------------------------------------------------

def metrics(tr, capital, sessions_index):
    if tr.empty:
        return {"trades": 0}

    tr = tr.sort_values("exit_date")
    pnl = tr["ret_pct"] / 100 * capital
    wins, losses = tr[tr.ret_pct > 0], tr[tr.ret_pct <= 0]

    equity = pnl.cumsum()
    dd = equity - equity.cummax()

    # Capital tied up, day by day
    held = pd.Series(0, index=sessions_index, dtype=int)
    for a, b in zip(pd.to_datetime(tr.entry_date), pd.to_datetime(tr.exit_date)):
        held.loc[a:b] += 1

    gross_win = wins.ret_pct.sum()
    gross_loss = abs(losses.ret_pct.sum())

    return {
        "trades": len(tr),
        "win_rate": round(len(wins) / len(tr) * 100, 1),
        "avg_win": round(wins.ret_pct.mean(), 2) if len(wins) else 0.0,
        "avg_loss": round(losses.ret_pct.mean(), 2) if len(losses) else 0.0,
        "expectancy_pct": round(tr.ret_pct.mean(), 2),
        "expectancy_rs": round(pnl.mean(), 0),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else 999.0,
        "total_pnl": round(pnl.sum(), 0),
        "max_dd": round(dd.min(), 0),
        "median_bars": int(tr.bars.median()),
        "pct_stopped": round((tr.reason.isin(["stop", "gap"])).mean() * 100, 1),
        "pct_be_exit": round((tr.reason == "be").mean() * 100, 1),
        "pct_ema_exit": round((tr.reason == "ema").mean() * 100, 1),
        "reached_be": round(tr.reached_be.mean() * 100, 1),
        "peak_positions": int(held.max()),
        "peak_capital": int(held.max() * capital),
        "avg_positions": round(held.mean(), 1),
    }


def write_report(results, bench, args, universe_n, tradeable_n, start, end):
    lines = [
        "# RSind backtest - RS crossing above %d" % args.rs_trigger, "",
        f"Window **{start} -> {end}** - universe **{universe_n}** NSE names "
        f"(**{tradeable_n}** passed the size/liquidity filters at least once)", "",
        f"Stop **{args.stop_pct}%**, moved to breakeven at "
        f"**{args.atr_mult}x ATR({args.atr_period})** - "
        f"**Rs {args.capital:,.0f}** per trade - unlimited positions", "",
        f"Benchmark - Nifty 50 buy & hold: **{bench:+.1f}%**", "",
        "## Results by exit rule", "",
        "| | Close < EMA10 | Close < EMA20 | Close < EMA50 |",
        "|---|---:|---:|---:|",
    ]

    rows = [
        ("Trades", "trades", "{:,}"),
        ("Win rate", "win_rate", "{}%"),
        ("Average win", "avg_win", "{:+}%"),
        ("Average loss", "avg_loss", "{:+}%"),
        ("Expectancy / trade", "expectancy_pct", "{:+}%"),
        ("Expectancy / trade (Rs)", "expectancy_rs", "Rs {:,.0f}"),
        ("Profit factor", "profit_factor", "{}"),
        ("Total P&L", "total_pnl", "Rs {:,.0f}"),
        ("Max drawdown", "max_dd", "Rs {:,.0f}"),
        ("Median holding (bars)", "median_bars", "{}"),
        ("Stopped out", "pct_stopped", "{}%"),
        ("Exited at breakeven", "pct_be_exit", "{}%"),
        ("Exited on EMA", "pct_ema_exit", "{}%"),
        ("Reached +1 ATR", "reached_be", "{}%"),
        ("Peak open positions", "peak_positions", "{}"),
        ("Peak capital deployed", "peak_capital", "Rs {:,.0f}"),
        ("Average open positions", "avg_positions", "{}"),
    ]
    for label, key, fmt in rows:
        cells = []
        for n in (10, 20, 50):
            v = results[n].get(key, "-")
            cells.append(fmt.format(v) if v != "-" else "-")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")

    lines += [
        "", "## Read this before acting on the numbers", "",
        "* **Survivorship bias.** The universe is NSE's *current* equity list. "
        "Companies delisted, merged or suspended during the window are missing "
        "entirely, and those are disproportionately losers. Every figure above "
        "is optimistic.",
        "* **No costs.** Brokerage, STT, exchange charges and slippage are "
        "excluded. Budget 0.3-0.5% round trip; at this trade count that is a "
        "large deduction.",
        "* **Market cap is reconstructed** from today's share count x the price "
        "on each day. Issuance and buybacks are not modelled.",
        "* **Adjusted prices.** Splits and dividends are back-adjusted, so entry "
        "levels differ from what was on screen at the time. Returns are right; "
        "absolute prices are not.",
        "* Unlimited concurrent positions means the peak capital line is the "
        "money you would actually have needed at the worst moment.",
    ]
    return "\n".join(lines) + "\n"


# -- Main ---------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--years", type=float, default=3, help="years of trading to test")
    p.add_argument("--rs-trigger", type=int, default=80)
    p.add_argument("--stop-pct", type=float, default=5.0)
    p.add_argument("--atr-mult", type=float, default=1.0)
    p.add_argument("--atr-period", type=int, default=14)
    p.add_argument("--min-mcap-cr", type=float, default=2000)
    p.add_argument("--min-turnover-cr", type=float, default=5)
    p.add_argument("--capital", type=float, default=100000)
    p.add_argument("--limit", type=int, default=0, help="cap universe size (testing)")
    args = p.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)

    # Two extra years on the front so RS has its 12-month lookback on day one.
    period = f"{int(args.years) + 2}y"

    print("*** Universe ***", flush=True)
    secs = get_nse_tickers()
    symbols = sorted(secs.keys())
    if args.limit:
        symbols = symbols[:args.limit]
    print(f"  {len(symbols)} NSE symbols")

    print(f"*** Downloading {period} of daily bars ***", flush=True)
    frames = download_panel(symbols + [REFERENCE_TICKER], period)
    if REFERENCE_TICKER not in frames:
        raise RuntimeError(f"No data for benchmark {REFERENCE_TICKER}; aborting.")
    print(f"  {len(frames)} series downloaded")

    o = panel(frames, "Open")
    h = panel(frames, "High")
    l = panel(frames, "Low")
    c = panel(frames, "Close")
    v = panel(frames, "Volume")

    ref = c[REFERENCE_TICKER].copy()
    for df in (o, h, l, c, v):
        df.drop(columns=[REFERENCE_TICKER], inplace=True, errors="ignore")

    # Trade only on sessions the index traded - keeps the calendar clean.
    idx = ref.dropna().index
    o, h, l, c, v = (d.reindex(idx) for d in (o, h, l, c, v))
    ref = ref.reindex(idx)

    print("*** Computing RS percentile on every date ***", flush=True)
    pct = rs_percentile(c, ref)

    print("*** Applying size and liquidity filters ***", flush=True)
    info = read_json(TICKER_INFO_FILE)
    mcap_now = pd.Series({
        t: (info.get(t, {}).get("info", {}) or {}).get("marketCap")
        for t in c.columns
    }, dtype="float64")
    last_px = c.ffill().iloc[-1]
    shares = (mcap_now / last_px).replace([np.inf, -np.inf], np.nan)

    mcap_hist = c.mul(shares, axis=1)
    turnover = (c * v).rolling(50, min_periods=25).median()

    tradeable = (
        (mcap_hist >= args.min_mcap_cr * CRORE)
        & (turnover >= args.min_turnover_cr * CRORE)
        & c.notna()
    )
    print(f"  {int(tradeable.any().sum())} names pass at least once "
          f"(of {len(c.columns)}); {int(shares.notna().sum())} have market-cap data")

    trigger = args.rs_trigger
    signals = (pct >= trigger) & (pct.shift(1) < trigger) & tradeable

    # Trim to the requested trading window (RS needs the first year to warm up).
    start = idx[-1] - pd.Timedelta(days=int(args.years * 365))
    signals = signals & (signals.index.to_series() >= start).values[:, None]
    print(f"  {int(signals.values.sum())} raw entry signals "
          f"from {start.date()} to {idx[-1].date()}")

    atr_v = atr(h, l, c, args.atr_period)

    results = {}
    for n in (10, 20, 50):
        print(f"*** Simulating EMA{n} exit ***", flush=True)
        ema = c.ewm(span=n, adjust=False, min_periods=n).mean()
        tr = simulate(signals, o, h, l, c, ema, atr_v,
                      args.stop_pct / 100, args.atr_mult)
        results[n] = metrics(tr, args.capital, idx)
        tr.to_csv(os.path.join(OUT_DIR, f"trades_ema{n}.csv"), index=False)
        print(f"  {results[n].get('trades', 0)} trades, "
              f"expectancy {results[n].get('expectancy_pct', 0)}%")

        if not tr.empty:
            eq = (tr.sort_values("exit_date")
                    .assign(pnl=lambda d: d.ret_pct / 100 * args.capital)
                    .groupby("exit_date").pnl.sum().cumsum())
            eq.rename("cumulative_pnl").to_csv(
                os.path.join(OUT_DIR, f"equity_ema{n}.csv"))

    bench_window = ref[ref.index >= start].dropna()
    bench = (bench_window.iloc[-1] / bench_window.iloc[0] - 1) * 100

    report = write_report(results, bench, args, len(c.columns),
                          int(tradeable.any().sum()), start.date(), idx[-1].date())
    with open(os.path.join(OUT_DIR, "summary.md"), "w") as f:
        f.write(report)
    with open(os.path.join(OUT_DIR, "summary.json"), "w") as f:
        json.dump({"params": vars(args), "benchmark_pct": round(float(bench), 2),
                   "results": {str(k): val for k, val in results.items()}}, f, indent=2)

    print("\n" + report)


if __name__ == "__main__":
    main()
