#!/usr/bin/env python
"""
backtest.py - RSind swing-trading backtest (v2).

v1 finding: RS crossing 80 with a 5% stop and a move-to-breakeven at 1 ATR
produced 5,454-5,918 trades over three years, a 20-25% win rate, and an
expectancy of roughly zero - which turns clearly negative once costs are
applied. Two things stood out:

  * 42.8% of trades reached +1 ATR but 27% exited at breakeven, so most
    would-be winners were scratched. On daily bars 1 ATR is noise.
  * Expectancy rose monotonically as the exit was loosened (EMA10 -> EMA50),
    which is the signature of a low-hit-rate trend follower being strangled
    by tight exits.

v2 therefore makes every one of those constraints optional and adds the
selectivity the raw signal lacks:

  --be-atr 0            disable the breakeven move (v1 used 1.0)
  --stop-atr 2          volatility-scaled stop instead of a flat percentage
  --trail-atr 3         chandelier trail from the highest high since entry
  --regime-ma 200       only trade when the index is above its own 200 DMA
  --trend-ema 200       only buy stocks trading above their 200 EMA
  --max-off-high 15     only buy within 15% of the 52-week high
  --max-per-day 10      keep the strongest N signals each day, ranked by RS
  --cost-pct 0.4        round-trip costs, deducted from every trade

Known limitations - read these before trusting the numbers
----------------------------------------------------------
  * Survivorship bias. The universe comes from NSE's CURRENT equity list, so
    companies delisted, merged or suspended during the test window are absent.
    Their trades - disproportionately losers - never appear.
  * Market cap is reconstructed as (today's share count x that day's price).
  * Prices are split- and dividend-adjusted, so entry prices are not what you
    would have seen on screen at the time; returns are correct, levels are not.
  * Costs are a flat percentage. Real impact cost on a smallcap is worse.
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
YEAR = 252


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

def relative_strength(close, ref_close):
    """
    IBD-style RS, identical maths to rs_ranking.py, evaluated on every date.
      strength = 0.4*r(1q) + 0.2*r(2q) + 0.2*r(3q) + 0.2*r(4q)
      RS       = (1 + strength_stock) / (1 + strength_index) * 100
    """
    def perf(s, q):
        return s / s.shift(QUARTER * q) - 1

    strength = (0.4 * perf(close, 1) + 0.2 * perf(close, 2)
                + 0.2 * perf(close, 3) + 0.2 * perf(close, 4))
    ref_strength = (0.4 * perf(ref_close, 1) + 0.2 * perf(ref_close, 2)
                    + 0.2 * perf(ref_close, 3) + 0.2 * perf(ref_close, 4))
    return ((1 + strength).div(1 + ref_strength, axis=0) * 100).where(close.notna())


def to_percentile(rs, min_names=100):
    """
    The live dashboard buckets RS with pd.qcut(..., 100). A cross-sectional
    percentile rank gives the same 0-99 buckets for distinct values and is far
    faster over ~1,000 dates.
    """
    pct = (np.ceil(rs.rank(axis=1, pct=True, method="first") * 100) - 1).clip(0, 99)
    return pct.where(rs.notna().sum(axis=1) >= min_names, np.nan)


def atr(high, low, close, period=14):
    """Wilder's ATR, column-wise."""
    prev = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev).abs(), (low - prev).abs()])
    tr = tr.groupby(level=0).max().reindex(close.index)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


# -- Trade engine -------------------------------------------------------------

def simulate(signals, o, h, l, c, ema, atr_v,
             stop_pct=0.05, stop_atr=0.0, be_atr=0.0, trail_atr=0.0, cost_pct=0.0):
    """
    Walks each ticker independently. Within a day the order is:
        1. a pending EMA exit fills at this open
        2. gap check, then intraday stop
        3. breakeven upgrade, if enabled and the day traded be_atr above entry
        4. chandelier trail upgrade, if enabled
        5. close below the EMA arms an exit for the NEXT open
    Stops are checked BEFORE they are raised: daily bars never reveal the
    intraday sequence, so this takes the pessimistic reading.

    stop_atr > 0 replaces the fixed percentage stop with stop_atr x ATR at entry.
    be_atr = 0 and trail_atr = 0 switch those rules off entirely.
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
            entry, entry_atr = O[entry_i], A[s]
            if not np.isfinite(entry) or entry <= 0 or not np.isfinite(entry_atr):
                continue

            stop = (entry - stop_atr * entry_atr) if stop_atr > 0 else entry * (1 - stop_pct)
            if stop <= 0:
                continue
            init_stop_pct = (stop / entry - 1) * 100
            be_level = entry + be_atr * entry_atr if be_atr > 0 else np.inf

            at_be = False
            run_high = entry
            stop_kind = "stop"
            exit_i = exit_px = reason = None
            pending_ema = False
            mae = mfe = 0.0

            for j in range(entry_i, n):
                if not np.isfinite(C[j]):
                    continue

                if pending_ema:
                    exit_i, exit_px, reason = j, O[j], "ema"
                    break

                if np.isfinite(O[j]) and O[j] <= stop:          # gapped through
                    exit_i, exit_px, reason = j, O[j], "gap"
                    break
                if np.isfinite(L[j]) and L[j] <= stop:
                    exit_i, exit_px, reason = j, stop, stop_kind
                    break

                if np.isfinite(H[j]):
                    mfe = max(mfe, H[j] / entry - 1)
                    run_high = max(run_high, H[j])
                if np.isfinite(L[j]):
                    mae = min(mae, L[j] / entry - 1)

                if not at_be and np.isfinite(H[j]) and H[j] >= be_level:
                    if entry > stop:
                        stop, stop_kind = entry, "be"
                    at_be = True

                if trail_atr > 0 and np.isfinite(A[j]):
                    trail = run_high - trail_atr * A[j]
                    if trail > stop:
                        stop, stop_kind = trail, "trail"

                if np.isfinite(E[j]) and C[j] < E[j]:
                    pending_ema = True

            if exit_i is None:                                   # still open
                exit_i, exit_px, reason = n - 1, C[n - 1], "open"

            busy_until = exit_i
            gross = (exit_px / entry - 1) * 100
            trades.append({
                "ticker": tk,
                "signal_date": dates[s].date(),
                "entry_date": dates[entry_i].date(),
                "exit_date": dates[exit_i].date(),
                "entry": round(float(entry), 2),
                "exit": round(float(exit_px), 2),
                "reason": reason,
                "bars": int(exit_i - entry_i),
                "init_stop_pct": round(init_stop_pct, 2),
                "ret_pct": round(gross, 2),
                "net_pct": round(gross - cost_pct, 2),
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
    net = tr["net_pct"]
    pnl = net / 100 * capital
    wins, losses = tr[net > 0], tr[net <= 0]

    equity = pnl.cumsum()
    dd = equity - equity.cummax()

    held = pd.Series(0, index=sessions_index, dtype=int)
    for a, b in zip(pd.to_datetime(tr.entry_date), pd.to_datetime(tr.exit_date)):
        held.loc[a:b] += 1

    gw = wins.net_pct.sum()
    gl = abs(losses.net_pct.sum())
    peak_cap = max(int(held.max()) * capital, capital)

    return {
        "trades": len(tr),
        "win_rate": round(len(wins) / len(tr) * 100, 1),
        "avg_win": round(wins.net_pct.mean(), 2) if len(wins) else 0.0,
        "avg_loss": round(losses.net_pct.mean(), 2) if len(losses) else 0.0,
        "gross_exp": round(tr.ret_pct.mean(), 2),
        "expectancy_pct": round(net.mean(), 2),
        "expectancy_rs": round(pnl.mean(), 0),
        "profit_factor": round(gw / gl, 2) if gl else 999.0,
        "total_pnl": round(pnl.sum(), 0),
        "max_dd": round(dd.min(), 0),
        "return_on_peak": round(pnl.sum() / peak_cap * 100, 1),
        "median_bars": int(tr.bars.median()),
        "pct_stopped": round(tr.reason.isin(["stop", "gap"]).mean() * 100, 1),
        "pct_be_exit": round((tr.reason == "be").mean() * 100, 1),
        "pct_trail_exit": round((tr.reason == "trail").mean() * 100, 1),
        "pct_ema_exit": round((tr.reason == "ema").mean() * 100, 1),
        "best": round(net.max(), 1),
        "worst": round(net.min(), 1),
        "peak_positions": int(held.max()),
        "peak_capital": peak_cap,
        "avg_positions": round(held.mean(), 1),
    }


def write_report(results, bench, args, universe_n, tradeable_n, start, end, funnel):
    on = lambda v, unit="": f"{v}{unit}" if v else "off"
    lines = [
        f"# RSind backtest v2 - RS crossing above {args.rs_trigger}", "",
        f"Window **{start} -> {end}** - universe **{universe_n}** NSE names "
        f"(**{tradeable_n}** passed size/liquidity at least once)", "",
        "## Rules tested", "",
        f"| Setting | Value |", "|---|---|",
        f"| Initial stop | {'%s x ATR(%d)' % (args.stop_atr, args.atr_period) if args.stop_atr > 0 else '%s%%' % args.stop_pct} |",
        f"| Move to breakeven | {on(args.be_atr, ' x ATR')} |",
        f"| Chandelier trail | {on(args.trail_atr, ' x ATR')} |",
        f"| Index regime filter | {on(args.regime_ma, ' DMA')} |",
        f"| Stock trend filter | {on(args.trend_ema, ' EMA')} |",
        f"| Max % off 52-week high | {on(args.max_off_high, '%')} |",
        f"| Signals kept per day | {on(args.max_per_day)} (ranked by RS) |",
        f"| Market cap floor | Rs {args.min_mcap_cr:,.0f} Cr |",
        f"| Turnover floor | Rs {args.min_turnover_cr:,.0f} Cr (50-day median) |",
        f"| Round-trip cost | {args.cost_pct}% (deducted from every trade) |",
        f"| Size per trade | Rs {args.capital:,.0f}, unlimited positions |",
        "",
        "## How the filters thinned the signal", "",
        "| Stage | Signals |", "|---|---:|",
    ]
    for label, count in funnel:
        lines.append(f"| {label} | {count:,} |")

    lines += [
        "", f"Benchmark - Nifty 50 buy & hold: **{bench:+.1f}%**", "",
        "## Results by exit rule (net of costs)", "",
        "| | Close < EMA10 | Close < EMA20 | Close < EMA50 |",
        "|---|---:|---:|---:|",
    ]

    rows = [
        ("Trades", "trades", "{:,}"),
        ("Win rate", "win_rate", "{}%"),
        ("Average win", "avg_win", "{:+}%"),
        ("Average loss", "avg_loss", "{:+}%"),
        ("Expectancy BEFORE costs", "gross_exp", "{:+}%"),
        ("**Expectancy AFTER costs**", "expectancy_pct", "**{:+}%**"),
        ("Expectancy / trade (Rs)", "expectancy_rs", "Rs {:,.0f}"),
        ("Profit factor", "profit_factor", "{}"),
        ("Total P&L", "total_pnl", "Rs {:,.0f}"),
        ("Return on peak capital", "return_on_peak", "{:+}%"),
        ("Max drawdown", "max_dd", "Rs {:,.0f}"),
        ("Best trade", "best", "{:+}%"),
        ("Worst trade", "worst", "{:+}%"),
        ("Median holding (bars)", "median_bars", "{}"),
        ("Stopped out", "pct_stopped", "{}%"),
        ("Exited at breakeven", "pct_be_exit", "{}%"),
        ("Exited on trail", "pct_trail_exit", "{}%"),
        ("Exited on EMA", "pct_ema_exit", "{}%"),
        ("Peak open positions", "peak_positions", "{}"),
        ("Peak capital deployed", "peak_capital", "Rs {:,.0f}"),
        ("Average open positions", "avg_positions", "{}"),
    ]
    for label, key, fmt in rows:
        cells = []
        for n in (10, 20, 50):
            v = results[n].get(key)
            cells.append(fmt.format(v) if v is not None else "-")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")

    lines += [
        "", "## Read this before acting on the numbers", "",
        "* **Survivorship bias.** The universe is NSE's *current* equity list. "
        "Companies delisted, merged or suspended during the window are missing, "
        "and those are disproportionately losers. Every figure is optimistic.",
        f"* **Costs are modelled at a flat {args.cost_pct}% round trip.** Real "
        "impact cost on an illiquid smallcap is worse, and worse still when you "
        "are one of many chasing the same breakout.",
        "* **Market cap is reconstructed** from today's share count x the price "
        "on each day. Issuance and buybacks are not modelled.",
        "* **Adjusted prices.** Returns are right; absolute levels are not.",
        "* A positive expectancy on a few hundred trades is not proof of an "
        "edge. Check that it holds in both halves of the window before sizing up.",
    ]
    return "\n".join(lines) + "\n"


# -- Main ---------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--years", type=float, default=3)
    p.add_argument("--rs-trigger", type=int, default=80)
    p.add_argument("--stop-pct", type=float, default=5.0, help="flat stop, used when --stop-atr is 0")
    p.add_argument("--stop-atr", type=float, default=2.0, help="ATR-multiple stop; 0 = use --stop-pct")
    p.add_argument("--be-atr", type=float, default=0.0, help="move to breakeven after N ATR; 0 = off")
    p.add_argument("--trail-atr", type=float, default=0.0, help="chandelier trail in ATRs; 0 = off")
    p.add_argument("--atr-period", type=int, default=14)
    p.add_argument("--regime-ma", type=int, default=200, help="index must be above its N DMA; 0 = off")
    p.add_argument("--trend-ema", type=int, default=200, help="stock must be above its N EMA; 0 = off")
    p.add_argument("--max-off-high", type=float, default=15.0, help="max %% below 52wk high; 0 = off")
    p.add_argument("--max-per-day", type=int, default=10, help="keep N strongest signals daily; 0 = off")
    p.add_argument("--min-mcap-cr", type=float, default=2000)
    p.add_argument("--min-turnover-cr", type=float, default=5)
    p.add_argument("--cost-pct", type=float, default=0.4, help="round-trip cost per trade, %%")
    p.add_argument("--capital", type=float, default=100000)
    p.add_argument("--tag", default="v2", help="suffix for output filenames")
    p.add_argument("--limit", type=int, default=0, help="cap universe size (testing)")
    args = p.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    period = f"{int(args.years) + 2}y"

    print("*** Universe ***", flush=True)
    symbols = sorted(get_nse_tickers().keys())
    if args.limit:
        symbols = symbols[:args.limit]
    print(f"  {len(symbols)} NSE symbols")

    print(f"*** Downloading {period} of daily bars ***", flush=True)
    frames = download_panel(symbols + [REFERENCE_TICKER], period)
    if REFERENCE_TICKER not in frames:
        raise RuntimeError(f"No data for benchmark {REFERENCE_TICKER}; aborting.")
    print(f"  {len(frames)} series downloaded")

    o, h, l, c, v = (panel(frames, f) for f in ("Open", "High", "Low", "Close", "Volume"))
    ref = c[REFERENCE_TICKER].copy()
    for df in (o, h, l, c, v):
        df.drop(columns=[REFERENCE_TICKER], inplace=True, errors="ignore")

    idx = ref.dropna().index
    o, h, l, c, v = (d.reindex(idx) for d in (o, h, l, c, v))
    ref = ref.reindex(idx)

    print("*** Computing RS on every date ***", flush=True)
    rs = relative_strength(c, ref)
    pct = to_percentile(rs)

    print("*** Filters ***", flush=True)
    info = read_json(TICKER_INFO_FILE)
    mcap_now = pd.Series({t: (info.get(t, {}).get("info", {}) or {}).get("marketCap")
                          for t in c.columns}, dtype="float64")
    shares = (mcap_now / c.ffill().iloc[-1]).replace([np.inf, -np.inf], np.nan)

    liquid = ((c.mul(shares, axis=1) >= args.min_mcap_cr * CRORE)
              & ((c * v).rolling(50, min_periods=25).median() >= args.min_turnover_cr * CRORE)
              & c.notna())

    start = idx[-1] - pd.Timedelta(days=int(args.years * 365))
    in_window = pd.Series(idx >= start, index=idx)

    cross = (pct >= args.rs_trigger) & (pct.shift(1) < args.rs_trigger)
    funnel = [("RS crossings in window", int((cross & in_window.values[:, None]).values.sum()))]

    sig = cross & liquid & in_window.values[:, None]
    funnel.append(("after size + liquidity", int(sig.values.sum())))

    if args.regime_ma:
        bull = ref > ref.rolling(args.regime_ma).mean()
        sig &= bull.values[:, None]
        funnel.append((f"after index > {args.regime_ma} DMA", int(sig.values.sum())))

    if args.trend_ema:
        sig &= (c > c.ewm(span=args.trend_ema, adjust=False,
                          min_periods=args.trend_ema).mean())
        funnel.append((f"after stock > {args.trend_ema} EMA", int(sig.values.sum())))

    if args.max_off_high:
        off = c / c.rolling(YEAR, min_periods=YEAR // 2).max() - 1
        sig &= (off >= -args.max_off_high / 100)
        funnel.append((f"after within {args.max_off_high:g}% of 52wk high",
                       int(sig.values.sum())))

    if args.max_per_day:
        kept = sig.copy()
        for dt in sig.index[sig.any(axis=1)]:
            names = sig.columns[sig.loc[dt].values]
            if len(names) > args.max_per_day:
                # rank on raw RS, not the percentile, which saturates at 99
                best = rs.loc[dt, names].nlargest(args.max_per_day).index
                drop = names.difference(best)
                kept.loc[dt, drop] = False
        sig = kept
        funnel.append((f"after keeping top {args.max_per_day} per day",
                       int(sig.values.sum())))

    for label, n in funnel:
        print(f"  {label}: {n:,}")

    atr_v = atr(h, l, c, args.atr_period)

    results = {}
    for n in (10, 20, 50):
        print(f"*** Simulating EMA{n} exit ***", flush=True)
        ema = c.ewm(span=n, adjust=False, min_periods=n).mean()
        tr = simulate(sig, o, h, l, c, ema, atr_v,
                      stop_pct=args.stop_pct / 100, stop_atr=args.stop_atr,
                      be_atr=args.be_atr, trail_atr=args.trail_atr,
                      cost_pct=args.cost_pct)
        results[n] = metrics(tr, args.capital, idx)
        tr.to_csv(os.path.join(OUT_DIR, f"trades_{args.tag}_ema{n}.csv"), index=False)
        print(f"  {results[n].get('trades', 0)} trades, "
              f"net expectancy {results[n].get('expectancy_pct', 0)}%")

        if not tr.empty:
            eq = (tr.sort_values("exit_date")
                    .assign(pnl=lambda d: d.net_pct / 100 * args.capital)
                    .groupby("exit_date").pnl.sum().cumsum())
            eq.rename("cumulative_pnl").to_csv(
                os.path.join(OUT_DIR, f"equity_{args.tag}_ema{n}.csv"))

    bw = ref[ref.index >= start].dropna()
    bench = (bw.iloc[-1] / bw.iloc[0] - 1) * 100

    report = write_report(results, bench, args, len(c.columns),
                          int(liquid.any().sum()), start.date(), idx[-1].date(), funnel)
    with open(os.path.join(OUT_DIR, f"summary_{args.tag}.md"), "w") as f:
        f.write(report)
    with open(os.path.join(OUT_DIR, f"summary_{args.tag}.json"), "w") as f:
        json.dump({"params": vars(args), "benchmark_pct": round(float(bench), 2),
                   "funnel": funnel,
                   "results": {str(k): val for k, val in results.items()}}, f, indent=2)

    print("\n" + report)


if __name__ == "__main__":
    main()
