# Aggressive Spot v1 — preregistration

Frozen before first backtest result is observed.

## Goal
Research whether a long-only spot strategy can plausibly approach a net average of 2–3% per week without leverage. The target is a research hurdle, not a promised return.

## Primary hypothesis: Regime + Momentum + 4H Breakout

Data/execution:
- Binance USDT spot, 4H UTC candles.
- Backtest start 2021-01-01; development/reporting through 2024-12-31; OOS 2025-01-01 through the end of the available Binance spot snapshot (currently ~2025-05).
- Start NAV: $100.
- Long-only spot; USDT otherwise.
- No leverage.
- Max 2 concurrent positions; 50% of current NAV per position at entry.
- Signal is computed only after a 4H candle closes; entry/ordinary exit at next 4H open.
- Intrabar stop can execute during the bar.
- Fee: 10 bps per side. Adverse slippage: 5 bps per side.
- No forward fill of missing bars for signal decisions.

Universe:
- USDT spot crypto pairs only.
- Exclude stablecoins, leveraged-token naming patterns, index/TradFi-like tickers using the same conservative filters as prior research.
- Minimum 90 calendar days of observed history before eligibility.
- 30-day average daily turnover proxy >= $5m/day, computed from sum(volume*close) across the trailing 180 four-hour bars / 30. This is a base-volume×price proxy, NOT exact quote turnover.

Market gate (BTC):
- BTC R7 > 0 and BTC R30 > 0, using 42 and 180 four-hour bars.
- If the gate is false, do not enter new positions; open positions are scheduled to exit at the next 4H open.

Candidate trend/momentum requirements:
- R7 > 0.
- R21 > 0 (126 four-hour bars).
- 90-day history and liquidity filter pass.

Breakout/participation trigger:
- Current 4H close > highest HIGH of the PREVIOUS 20 four-hour bars (current bar excluded).
- Current 4H volume >= 1.5 × median volume of the previous 20 four-hour bars.

Ranking:
- rv7 = std of 4H returns over 42 bars.
- rv21 = std of 4H returns over 126 bars.
- score = 0.60*(R7/rv7) + 0.40*(R21/rv21), requiring positive nonzero rv values.
- Among qualifying new-entry candidates, choose highest score for available slots.

Cross-sectional stress filter:
- Compute dispersion = cross-sectional std of R7 among currently trend/liquidity-eligible assets.
- Entries are blocked when current dispersion is above the trailing 180-bar 90th percentile of dispersion, with the current observation excluded from the threshold.
- Existing positions are not forced out solely by this filter.

Risk/exit rules:
- ATR14 is standard true-range rolling mean on 14 four-hour bars.
- At entry, initial stop = entry price - 2.0*ATR14(signal bar).
- Trailing stop after each close = max(old stop, close - 2.5*ATR14(current bar)).
- Stop is never loosened.
- Maximum hold = 42 four-hour bars = 7 days; exit next open.
- If BTC market gate becomes false at a signal close, exit next open.
- After exit, same symbol has a 42-bar (7-day) re-entry cooldown.

Accounting:
- One shared NAV.
- Position notional at entry = 50% of NAV immediately before the entry, capped by available cash.
- Entry and exit fees debited explicitly.
- Equity marked to 4H close for drawdown/statistics.

Primary reported metrics:
- Final NAV and total return from $100.
- CAGR.
- Max drawdown.
- Annualized Sharpe from 4H equity returns (diagnostic only).
- Number of positions, win rate, average hold time, fees paid.
- Weekly mean and median return; profitable-week share; negative-week share; share of weeks >= +2% and >= +3%; best/worst week.
- Active-week mean/median, where active week means at least one open position during the week.
- 2025 OOS return and OOS weekly statistics.

Research hurdle for the user's stated objective:
- The literal 2–3%/week goal is considered supported only if net mean weekly return is >=2% AND the result is not dependent on a few extreme weeks, judged by median/quantiles and OOS behavior.
- MDD >40% is a risk failure even if mean weekly return is high.
- A strategy may still be interesting below the 2% weekly hurdle, but it will be reported as failing that target.

No parameter changes are permitted after seeing the first primary backtest result. Any later modification becomes Aggressive Spot v2 and must be preregistered separately.
