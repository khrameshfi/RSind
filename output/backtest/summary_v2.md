# RSind backtest v2 - RS crossing above 80

Window **2023-08-14 -> 2026-08-13** - universe **2124** NSE names (**1265** passed size/liquidity at least once)

## Rules tested

| Setting | Value |
|---|---|
| Initial stop | 2.0 x ATR(14) |
| Move to breakeven | off |
| Chandelier trail | off |
| Index regime filter | 200 DMA |
| Stock trend filter | 200 EMA |
| Max % off 52-week high | 15.0% |
| Signals kept per day | 10 (ranked by RS) |
| Market cap floor | Rs 2,000 Cr |
| Turnover floor | Rs 5 Cr (50-day median) |
| Round-trip cost | 0.4% (deducted from every trade) |
| Size per trade | Rs 100,000, unlimited positions |

## How the filters thinned the signal

| Stage | Signals |
|---|---:|
| RS crossings in window | 13,497 |
| after size + liquidity | 6,815 |
| after index > 200 DMA | 4,740 |
| after stock > 200 EMA | 4,626 |
| after within 15% of 52wk high | 3,630 |
| after keeping top 10 per day | 3,526 |

Benchmark - Nifty 50 buy & hold: **+25.5%**

## Results by exit rule (net of costs)

| | Close < EMA10 | Close < EMA20 | Close < EMA50 |
|---|---:|---:|---:|
| Trades | 2,942 | 2,688 | 2,273 |
| Win rate | 30.0% | 29.6% | 26.4% |
| Average win | +7.51% | +10.28% | +17.0% |
| Average loss | -3.82% | -4.48% | -5.67% |
| Expectancy BEFORE costs | -0.01% | +0.28% | +0.7% |
| **Expectancy AFTER costs** | **-0.41%** | **-0.12%** | **+0.3%** |
| Expectancy / trade (Rs) | Rs -413 | Rs -117 | Rs 302 |
| Profit factor | 0.85 | 0.96 | 1.07 |
| Total P&L | Rs -1,216,510 | Rs -313,310 | Rs 686,920 |
| Return on peak capital | -16.9% | -3.3% | +4.9% |
| Max drawdown | Rs -1,460,450 | Rs -1,074,340 | Rs -1,599,550 |
| Best trade | +77.1% | +127.8% | +155.3% |
| Worst trade | -36.9% | -36.9% | -74.8% |
| Median holding (bars) | 4 | 6 | 10 |
| Stopped out | 11.6% | 19.8% | 36.0% |
| Exited at breakeven | 0.0% | 0.0% | 0.0% |
| Exited on trail | 0.0% | 0.0% | 0.0% |
| Exited on EMA | 88.4% | 80.2% | 63.9% |
| Peak open positions | 72 | 96 | 140 |
| Peak capital deployed | Rs 7,200,000 | Rs 9,600,000 | Rs 14,000,000 |
| Average open positions | 18.4 | 25.5 | 40.8 |

## Read this before acting on the numbers

* **Survivorship bias.** The universe is NSE's *current* equity list. Companies delisted, merged or suspended during the window are missing, and those are disproportionately losers. Every figure is optimistic.
* **Costs are modelled at a flat 0.4% round trip.** Real impact cost on an illiquid smallcap is worse, and worse still when you are one of many chasing the same breakout.
* **Market cap is reconstructed** from today's share count x the price on each day. Issuance and buybacks are not modelled.
* **Adjusted prices.** Returns are right; absolute levels are not.
* A positive expectancy on a few hundred trades is not proof of an edge. Check that it holds in both halves of the window before sizing up.
