# Aggressive Spot v2 — preregistration

Frozen before any v2 backtest result is observed. v1 remains a failed NO-GO and is not modified.

## Research basis
- Crypto momentum is a documented factor, but plain cross-sectional momentum is unstable and tail-sensitive.
- Recent crypto evidence uses a 2-week formation / 1-week holding construction and finds risk management improves momentum economics.
- Recent survivorship-aware research finds lagged cross-sectional dispersion predicts weaker subsequent crypto momentum.
- This v2 adapts those ideas to the user's constraint: long-only spot, no leverage, cash allowed.

## Data and costs
- Binance USDT spot 4H UTC snapshot.
- Test 2021-01-01 through 2025-05-31 20:00 UTC.
- Start NAV $100.
- 2021–2024 development/history reporting; 2025 is OOS.
- 10 bps fee + 5 bps adverse slippage per side.
- No leverage, no shorts.
- No forward fill for signals.

## Dynamic point-in-time universe
At each weekly signal time:
- USDT crypto spot only; stablecoins / leveraged tokens / TradFi-like tickers excluded.
- At least 90 days of observed 4H history.
- 30-day average daily turnover proxy >= $5m/day, based on trailing sum(volume*close)/30. This is NOT exact quote turnover.
- From passing assets, retain the 30 highest-liquidity symbols at that date. This is dynamic/PIT, not today's top 30.

## Weekly schedule
- Signal every Sunday 20:00 UTC after that 4H candle closes.
- Rebalance at Monday 00:00 UTC open.
- Hold until next weekly rebalance unless a market-risk exit fires.

## Market / risk state
- BTC 30-day return (180 bars) must be > 0 to initiate/maintain risk.
- BTC 14-day return (84 bars) must also be > 0.
- If either is <=0 at any 4H close, schedule complete portfolio exit at next 4H open and remain cash until a later weekly rebalance signal qualifies. No mid-week re-entry.

## Momentum formation and selection
- Formation return: trailing 14-day return = close / close.shift(84) - 1.
- Candidate must have positive 14-day return.
- Candidate must be in the dynamic top-30 liquidity universe.
- Exclude BTC from alt ranking; BTC may still be selected only if it ranks naturally when included in the all-asset rank test described below.
- Rank all top-30 eligible assets by 14-day return descending.
- Select the top 2 assets with positive return.
- To reduce single-name lottery/tail domination without tuning a numeric return cap, require each selected asset's 14-day return to be below the contemporaneous cross-sectional 95th percentile of the top-30 universe. If the #1 name is above the 95th percentile, skip it and take the next eligible name. This is a state-relative tail filter rather than a fixed optimized threshold.

## Dispersion state filter
- Cross-sectional dispersion = std of 14-day returns across the current dynamic top-30 universe.
- Maintain a weekly history of dispersion.
- Block new weekly entries if current dispersion is above the trailing 26-week 90th percentile of dispersion, using only previous weeks to set the threshold.
- Existing positions are exited on the scheduled Monday rebalance if the new weekly signal is blocked.

## Position sizing / risk management
- Max 2 positions.
- Base allocation = 50% NAV per selected asset.
- No leverage; total gross <=100% NAV.
- Portfolio exposure multiplier based on BTC realized 4H volatility over the past 4 weeks (168 bars):
  - compute annualized-ish relative state only, not a return forecast;
  - compare current BTC rv168 to its trailing 26-week (1092 bars) median, excluding current observation;
  - if current rv168 <= trailing median: exposure multiplier = 1.00;
  - if current rv168 > trailing median: exposure multiplier = 0.50.
- Therefore each selected asset receives 50% NAV in normal-vol state or 25% NAV in high-vol state; remainder stays USDT.

## Rebalance accounting
- At Monday open, sell assets not in the new target set, resize retained assets to new target weights, then buy new assets.
- For simplicity and conservatism, every weekly target position is fully closed and reopened at Monday open, charging fee/slippage both ways even if the same symbol remains selected. This overstates turnover/cost versus an optimized rebalance implementation.

## Metrics / hurdle
Report:
- $100 final NAV, total return, CAGR, MDD, Sharpe diagnostic.
- Positions/weekly selections, total entry legs, fees.
- Weekly mean, median, positive/negative share, >=2% and >=3% share, best/worst week.
- Active-week mean/median.
- 2025 OOS return and OOS weekly mean/median.
- yearly returns.

The user's 2–3%/week target is a PASS only if net mean weekly return >=2%, median and OOS do not show that the mean is driven by rare tails, and MDD <=40%.

No v2 parameter changes after first v2 result. Any next hypothesis is v3 with a new preregistration.
