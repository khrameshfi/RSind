# RSind backtest - RS crossing above 80

Window **2023-08-14 -> 2026-08-13** - universe **2124** NSE names (**1265** passed the size/liquidity filters at least once)

Stop **5.0%**, moved to breakeven at **1.0x ATR(14)** - **Rs 100,000** per trade - unlimited positions

Benchmark - Nifty 50 buy & hold: **+25.5%**

## Results by exit rule

| | Close < EMA10 | Close < EMA20 | Close < EMA50 |
|---|---:|---:|---:|
| Trades | 5,918 | 5,732 | 5,454 |
| Win rate | 24.9% | 23.1% | 20.1% |
| Average win | +7.12% | +8.11% | +11.22% |
| Average loss | -2.42% | -2.44% | -2.46% |
| Expectancy / trade | -0.05% | -0.01% | +0.29% |
| Expectancy / trade (Rs) | Rs -46 | Rs -5 | Rs 291 |
| Profit factor | 0.97 | 1.0 | 1.15 |
| Total P&L | Rs -272,270 | Rs -30,790 | Rs 1,585,600 |
| Max drawdown | Rs -1,825,340 | Rs -1,681,310 | Rs -1,852,080 |
| Median holding (bars) | 2 | 2 | 2 |
| Stopped out | 23.7% | 28.7% | 33.6% |
| Exited at breakeven | 19.1% | 22.8% | 27.0% |
| Exited on EMA | 56.3% | 47.4% | 37.6% |
| Reached +1 ATR | 40.0% | 41.3% | 42.8% |
| Peak open positions | 93 | 117 | 135 |
| Peak capital deployed | Rs 9,300,000 | Rs 11,700,000 | Rs 13,500,000 |
| Average open positions | 26.5 | 31.7 | 43.0 |

## Read this before acting on the numbers

* **Survivorship bias.** The universe is NSE's *current* equity list. Companies delisted, merged or suspended during the window are missing entirely, and those are disproportionately losers. Every figure above is optimistic.
* **No costs.** Brokerage, STT, exchange charges and slippage are excluded. Budget 0.3-0.5% round trip; at this trade count that is a large deduction.
* **Market cap is reconstructed** from today's share count x the price on each day. Issuance and buybacks are not modelled.
* **Adjusted prices.** Splits and dividends are back-adjusted, so entry levels differ from what was on screen at the time. Returns are right; absolute prices are not.
* Unlimited concurrent positions means the peak capital line is the money you would actually have needed at the worst moment.
