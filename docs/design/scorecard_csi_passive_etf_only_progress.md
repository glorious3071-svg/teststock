# Scorecard CSI Passive ETF-only Progress

## Objective

Optimize the market scorecard and CSI selection scorecard under a domestic ETF-only mandate:

- start capital: 1,000,000
- backtest window: 2006 through 2025
- investable holdings: A-share market domestic passive index ETFs only
- excluded: overseas tools, options, futures, crypto, and other non-ETF instruments
- robustness: month-drift tests across annual, quarterly, and monthly rebalance phases
- pass gate: every tested case ends above 40,000,000 and max drawdown is better than -10%
- production need: generate concrete future target holdings automatically

## Data Fixes Completed

- Backfilled old ETF continuity after 2011 for 15 early listed ETFs.
- Imported exchange money ETF price history for separate defensive tests:
  - 511990.SH from 2013-01-28 through 2026-07-15
  - 511880.SH from 2013-04-18 through 2026-07-15
- `fund_daily` reached 454 ETFs and 505,304 rows after the old ETF continuity fill.

## Search And Audit Artifacts

- `scripts/search_scorecard_csi_passive_etf_only.py`
  - strict domestic passive ETF-only search
  - optional money ETF defensive sleeve
  - optional uninvested cash defensive state
  - random month-drift tests: 12 phase offsets x 4 execution lags
- `scripts/audit_scorecard_csi_passive_etf_only_feasibility.py`
  - 2008 investable universe drawdown audit
  - first defensive ETF availability audit
  - monthly oracle upper-bound audit
- `scripts/generate_scorecard_csi_passive_etf_only_targets.py`
  - automated target generation for a named ETF-only rule
  - current default is risk-controlled but does not pass the objective
- `scripts/map_csi_to_etf_proxy.py`
  - exact CSI ETF mapping first
  - if exact listed ETF is unavailable or lacks local `fund_daily`, use correlated domestic SH/SZ ETF proxy
  - if correlation data is insufficient, fall back to the early broad proxy pool: 510050.SH, 510180.SH, 159901.SZ, 159902.SZ, 510880.SH
- `scripts/run_scorecard_csi_passive_etf_only_pipeline.py`
  - one-command validation summary plus current target generation
  - exits non-zero when the 4000w and -10% objective is not passed
  - includes strict ETF-only TIPP validation, and does not count domestic ETF option package experiments as ETF-only passes
- `scripts/search_scorecard_csi_proxy_etf_backtest.py`
  - backtests annual CSI/SI scorecard recommendations mapped into domestic SH/SZ ETF proxies
  - supports early SI recommendations and broad SH/SZ proxy fallback
- `scripts/search_scorecard_csi_passive_etf_trend_state.py`
  - trend-state ETF rotation with uninvested cash exits
  - uses daily capital curves for max drawdown
  - tests monthly/quarterly rebalance drift plus execution lag
- `scripts/search_scorecard_csi_passive_etf_tipp.py`
  - phase-ensemble CSI selector plus TIPP sizing
  - invested returns come only from `fund_daily` domestic `.SH/.SZ` passive ETF proxies
  - excludes options, futures, overseas assets, crypto, and synthetic package returns

## Current Results

Historical CSI/SI recommendation coverage:

- `csi_annual_recommendation` now covers every apply year from 2006 through 2026.
- Missing years 2006-2017, 2019, and 2025 were generated with `rank_annual_csi.py --suffix all --full --save`.
- Existing saved years were not overwritten.

Strict passive index ETF-only, refreshed data:

- file: `data/backtests/scorecard_csi_passive_etf_only_strict_refreshed_quick_search.csv`
- pass count: 0/48
- best min final capital: 294.6w
- corresponding worst max drawdown: -69.8%
- no rule met max drawdown better than -10%

Money ETF defensive sleeve allowed:

- file: `data/backtests/scorecard_csi_passive_etf_only_moneydef_refreshed_quick_search.csv`
- pass count: 0/48
- best displayed worst max drawdown remained around -71%
- money ETFs start in 2013, so they do not solve the 2008 early-universe gap

Uninvested cash defensive state allowed:

- file: `data/backtests/scorecard_csi_passive_etf_only_cashdef_scoregate_focused_search.csv`
- pass count: 0/48
- highest min final capital found: 1762.5w, but worst max drawdown was -87.8%
- best min final capital with worst max drawdown better than -10%: 145.9w
- conclusion: cash solves drawdown only by giving up too much return

Feasibility audit:

- file: `data/backtests/scorecard_csi_passive_etf_only_feasibility_strict.json`
- 2008 full-year investable ETFs: 5, all equity ETFs
- best 2008 ETF drawdown: 159902.SZ at -65.6%, with calendar return -54.6%
- first strict defensive passive ETF: 511010.SH on 2013-03-25
- monthly realized-return oracle still had 0/48 pass count and max drawdown around -49.5% to -51.7%

Trend-state ETF rotation with daily stop exits:

- file: `data/backtests/scorecard_csi_passive_etf_trend_state_quick_search.csv`
- pass count: 0/48
- best min final capital: 111.2w
- best displayed worst max drawdown: -35.2%
- no rule met max drawdown better than -10%

Trend-state ETF rotation with global drawdown re-entry gate:

- file: `data/backtests/scorecard_csi_passive_etf_trend_state_gate_quick_search.csv`
- pass count: 0/48
- best min final capital: 96.2w
- best displayed worst max drawdown: -32.8%
- no rule met max drawdown better than -10%
- conclusion: monthly/quarterly re-entry after stop-outs still creates repeated losses; stricter gates reduce exposure and return without reaching the 10% drawdown target

Phase-ensemble + TIPP wrapper using only domestic SH/SZ passive ETF returns:

- file: `data/backtests/scorecard_csi_passive_etf_tipp_quick_search.csv`
- aggressive smoke file: `data/backtests/scorecard_csi_passive_etf_tipp_aggressive_smoke_search.csv`
- pass count: 0/48
- quick best drawdown-ranked rule: worst max drawdown about -9.4%, but minimum final capital only about 120.9w
- aggressive smoke result: wider stops and higher ETF exposure did not improve the frontier; best displayed minimum final capital was about 117.9w with worst max drawdown about -11.7%
- conclusion: phase diversification and TIPP sizing are useful controls, but without ETF-option package returns the strict ETF-only mapped sleeve has not compounded fast enough while respecting the 10% drawdown gate

CSI/SI scorecard recommendations mapped to SH/SZ ETF proxies:

- file: `data/backtests/scorecard_csi_proxy_etf_quick_search.csv`
- pass count: 0/48
- best full-invested rule: annual Top-5 proxy portfolio, min final capital 162.0w, worst max drawdown -74.8%
- best trend-cash rule: monthly CSI Top-5 proxy portfolio with 6m market trend gate, min final capital 142.8w, worst max drawdown -57.6%
- conclusion: SI and broad SH/SZ ETF proxies solve early ETF availability, but the proxies are still equity beta assets and do not solve the 2008 drawdown constraint

## Current Automated Target Example

Command:

```bash
.venv/bin/python scripts/generate_scorecard_csi_passive_etf_only_targets.py \
  --allow-cash-defense \
  --output-prefix data/portfolio/scorecard_csi_passive_etf_only_targets_latest
```

Latest generated target as of 2026-07-15:

- 10.0% `589020.SH` 科创半导体设备ETF鹏华
- 90.0% uninvested cash

Outputs:

- `data/portfolio/scorecard_csi_passive_etf_only_targets_latest.json`
- `data/portfolio/scorecard_csi_passive_etf_only_targets_latest.csv`

## CSI ETF Proxy Mapping

The production CSI target generator now supports `--allow-etf-proxy`.

Rules:

- exact listed `.SH/.SZ` passive ETF with local price data is preferred;
- `.OF` fund codes are excluded from generated holdings;
- if exact ETF is missing or lacks local prices, a correlated domestic passive ETF proxy is selected from SH/SZ ETF candidates;
- early CSI ETF scarcity can fall back to the broad SH/SZ proxy pool.

Generated 2026 target with proxy enabled:

```bash
.venv/bin/python scripts/generate_csi_portfolio_targets.py \
  --year 2026 --top 10 --as-of 2026-07-15 --allow-etf-proxy
```

Outputs:

- `data/portfolio/csi_portfolio_targets_2026.json`
- `data/portfolio/csi_portfolio_targets_2026.csv`
- `data/portfolio/csi_etf_proxy_mapping_2026.csv`

The 2026 generated CSI target uses only `.SH/.SZ` ETF codes. Two positions use correlation proxies (`159909.SZ`) because the exact CSI ETFs did not have usable local `fund_daily` data at the as-of date.

## Pipeline Output

Command:

```bash
.venv/bin/python scripts/run_scorecard_csi_passive_etf_only_pipeline.py \
  --output-prefix data/portfolio/scorecard_csi_passive_etf_only_pipeline_latest
```

Latest pipeline result:

- objective_met: false
- strict ETF-only: 0/48 pass, best displayed min capital 294.6w, worst max drawdown -69.8%
- cash-defense scoregate: 0/48 pass, best displayed min capital 108.2w, worst max drawdown -4.4%
- trend-state gate: 0/48 pass, best displayed min capital 86.9w, worst max drawdown -32.8%
- phase TIPP ETF-only: 0/48 pass, best displayed min capital 120.9w, worst max drawdown -9.4%
- CSI/SI proxy ETF: 0/48 pass, best displayed min capital 142.8w, worst max drawdown -57.6%

Outputs:

- `data/portfolio/scorecard_csi_passive_etf_only_pipeline_latest.json`
- `data/portfolio/scorecard_csi_passive_etf_only_pipeline_latest_targets.csv`
- `data/portfolio/scorecard_csi_passive_etf_only_pipeline_latest_validation.csv`

## Current Boundary

The original pass gate is not achieved. Cash is now explicitly allowed as the
uninvested allocation produced by the market scorecard; it is not treated as an
investment instrument. The current blocker is still structural:

- before 2013, the domestic passive ETF universe lacked a defensive ETF;
- 2008 available ETFs were all equity beta products with drawdowns far beyond 10%;
- early CSI ETF scarcity is now handled by SH/SZ ETF proxies, but those proxies are still equity beta products in 2008;
- fully ETF-invested strategies fail the drawdown gate;
- cash-defensive strategies can satisfy drawdown but fail the 40x capital gate by a wide margin;
- trend-state stop strategies did not improve the frontier; they failed both the 40x capital gate and the 10% drawdown gate.
- phase-ensemble TIPP controls can approach the drawdown gate, but the ETF-only mapped sleeve has not produced enough return; the local strict quick search remains 0/48.

Important boundary:

- `scripts/search_scorecard_csi_domestic_only_tipp.py` and `scripts/generate_csi_domestic_tipp_targets.py` use SSE ETF option package legs (`OP510050.SH` / `OP510300.SH`) as part of their research and target construction.
- Those candidates are domestic and can pass numerical backtest gates, but they are not valid evidence for this strict passive ETF-only objective because ETF options are not index-type passive ETF holdings.

## Market Scorecard Allocation Test

Added:

- `scripts/search_scorecard_csi_market_allocation_proxy_etf.py`

This test implements the corrected mandate interpretation:

- the market scorecard controls the actual ETF exposure percentage;
- cash is the uninvested remainder and earns 0% in the test;
- the invested sleeve is restricted to domestic SH/SZ passive ETF proxies;
- CSI/SI or regime/momentum hybrid scorecards choose the index sleeve;
- 12 rebalance-month phases and 0/1/3/5 execution-day lags are tested.

Commands:

```bash
.venv/bin/python scripts/search_scorecard_csi_market_allocation_proxy_etf.py \
  --quick --summary-only \
  --output-prefix data/backtests/scorecard_csi_market_allocation_proxy_etf_quick

.venv/bin/python scripts/search_scorecard_csi_market_allocation_proxy_etf.py \
  --quick --summary-only --selector hybrid \
  --output-prefix data/backtests/scorecard_csi_market_allocation_proxy_etf_hybrid_quick

.venv/bin/python scripts/search_scorecard_csi_market_allocation_proxy_etf.py \
  --quick --summary-only --selector hybrid --core --use-correlation-proxy \
  --output-prefix data/backtests/scorecard_csi_market_allocation_proxy_etf_hybrid_core_corr
```

Latest results:

- ordinary CSI/SI recommendation + ETF proxy: 0/48 pass; best drawdown-ranked rule min capital 140.2w, worst max drawdown -43.4%; highest min-capital rule 170.8w with -54.2% worst drawdown;
- regime/momentum hybrid selector + broad/exact ETF proxy: 0/48 pass; best drawdown-ranked rule min capital 319.4w, worst max drawdown -43.0%; highest min-capital rule 403.0w with -59.4% worst drawdown;
- regime/momentum hybrid selector + correlated ETF proxy: 0/48 pass; best drawdown-ranked rule min capital 182.4w, worst max drawdown -54.7%; highest min-capital rule 285.7w with -66.0% worst drawdown.

## Strict Exact-Three-Month Frontier (2026-07-17)

The current formal harness interprets quarterly drift as an arbitrary starting
month followed by rebalancing exactly every three months.  It tests all 12
starting months with execution lags 0/1/3/5, freezes ETF and exposure weights
inside each quarter, and uses daily returns only for mark-to-market drawdown.

The point-in-time ETF training set covers 376 compliant domestic passive ETFs
and 161 tracked indices.  The v3 scorecard added a small index ROE proxy.  The
new v5 research scorecard retains the low-parameter beta/distance/
autocorrelation/volatility core, adds a 12-month book-growth reversal term, and
uses small constituent earnings-yield and index-weight concentration terms
only where real historical constituent snapshots exist.  Missing early
constituent features remain neutral.

Historical constituents are now synchronized through
`scripts/backfill_index_constituent_history_tushare.py`.  It requests exact
quarter-end dates, rejects snapshots whose weights sum to less than 95%, and
never carries current constituents backwards.  The first backfill added 12,493
rows; 32 empty and 56 incomplete dates were skipped.  Point-in-time stock
valuation aggregates are rebuilt by
`scripts/build_index_constituent_fundamentals.py`.  The current table contains
5,658 qualified index snapshots with earnings yield, book yield, ROE proxy,
dividend yield, positive-earnings breadth, and weight HHI.

Current verified frontiers:

- return-first: `blend_index_weighted_stable_v3_top1_w32` with
  `q_scorecard_full125_bc0_fc25`, minimum final capital `761.9w`, worst maximum
  drawdown about `-26.63%`;
- drawdown-compliant: `blend_index_weighted_stable_v5_top1_w49` with
  `q_bdgrid_m308_bd05_e70_f893_m566_bc0_fc25` and
  `bondfine_105d_vp25_top1_min-50`, minimum final capital `506.4136w`, median
  final capital `616.9916w`, worst maximum drawdown `-9.976236%`;
- both reports contain a complete 48-case matrix and no quarterly spacing or
  weight-path violations; the repository test suite has 58 passing tests.

The compliant rule changes exposure only at the three-month boundary.  CPPI
remains the base risk budget.  When the point-in-time CSI 300 return is at least
8% over three months, non-negative over six months, and above its six-month
moving average, while the selected ETF basket is no more than 5% below its
six-month high, exposure may recover to 70%; 75% breached the drawdown gate.
The defensive sleeve uses a 105-trading-day, volatility-penalized ranking among
live domestic bond index ETFs.  Searches over 45 bond variants, v5 blend weights,
alternative CSI selectors, staged recovery, v5 top-three diversification, and a
new index-drawdown v6 feature all retained only improvements that passed the
complete 48-case matrix; the rejected variants are not used by automation.
An additional 12-phase, three-era point-in-time feature screen is written to
`data/backtests/strict_quarterly_market_feature_ic_report.json`.  Domestic
commodity-index ETFs tracking DCE, SHFE, and CZCE indices were also tested as
ETF-only defensive assets; all tested allocations either breached the 10%
drawdown gate or reduced the strict minimum return, so they remain excluded
from current holdings.

Current holdings are generated by
`scripts/generate_scorecard_csi_strict_quarterly_targets.py`.  It preserves the
chosen drift phase, schedules the next decision exactly three months later,
uses the v5 49% selector blend, the trend-confirmed recovery rule, and the strict
CPPI frontier, validates every
non-cash line against the point-in-time domestic passive-ETF universe, and
writes `data/portfolio/scorecard_csi_strict_quarterly_targets_latest.{json,csv}`.
The 2026-07-16 artifact applies the confirmed trend recovery and contains 70%
equity-index ETFs plus 30% in a domestic local-government-bond index ETF; asset
validation passes with no overseas or enhanced products.

The full target remains open.  Further searches stay inside the domestic
passive-index ETF boundary and must improve both point-in-time ETF selection
and quarterly market exposure without changing the three-month interval or the
`4000w / 10%` gates.

## Risk-Cluster and Corrected-Direction Frontier (2026-07-17)

The quarterly decision audit now computes the selected ETF basket's realized
return from a separate frozen counterfactual sleeve.  A zero actual equity
allocation therefore no longer writes a false zero training label.  The strict
engine also enforces the direction policy's declared pre-decision drawdown
guard.

Correlated risk flags are assigned exactly once to five independent clusters:
price cycle, leverage/crowding, breadth/leadership, macro/liquidity, and crisis.
A full exit from ordinary risk concurrence now requires at least two clusters
at two consecutive quarterly decisions; the original explicit crisis/exit
flags remain hard exits.  Tests verify complete and non-duplicated flag
assignment.

The current research-only strict frontier is:

- rule: `q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_return4m145_fc21`;
- CSI selector: `expanded_value_risk_top5`;
- direct/index ETF blend: `blend_index_weighted_stable_v5_top1_regime_w49_s75`;
- defense: `bondfine_105d_vp25_top1_min-50`;
- minimum final capital: `674.8572w`;
- median final capital: `818.0969w`;
- worst daily maximum drawdown: `-9.956967%`;
- median actual equity exposure: `19.8768%`;
- drawdown gate: `48/48` pass;
- capital gate: `0/48` pass.

The result is research progress, not a production-rule promotion.  Increasing
the positive-direction multiplier to 1.50 breached the drawdown gate, raising
the CPPI floor reduced the capital floor below this frontier, an 80% direct-ETF
blend reached `-10.04%`, and the v6 direct selector recheck reached only
`593.1w` with `-11.50%` drawdown.  These rejected variants remain excluded from
target generation.

## Exposure-Formation Audit and Bear-Cap Frontier (2026-07-17)

The strict engine now records every exposure layer at each quarterly decision:
the annual scorecard limit, CPPI cushion limit, bear cap, quality/crisis cap,
direction boost, recovery floor, risk-cluster cap, ETF-share crowding exit, and
hard exit.  The audit distinguishes a condition that was merely active from a
condition that actually changed exposure.  A separate summarizer writes the
binding-layer counts and positive/negative next-quarter attribution.

Across 3,840 decisions in the complete 48-path matrix, CPPI was the initial
binding limit 2,827 times and the annual scorecard 1,013 times.  The old binary
bear cap reduced exposure 1,286 times; 742 of those quarters subsequently had
positive ETF returns.  Independent date-level recalculation and a synthetic
test confirm that the signal itself correctly uses the last 60 observations
and 20 return intervals without lookahead.  The defect was treating every
technical bear state as a three-month zero allocation.

The current research-only frontier is now:

- rule: `q_etfsharecap_t55c00_e64_f900_cluster2persistent_exit_return4m145_fc21_bc36`;
- minimum final capital: `764.6941w`;
- median final capital: `918.0144w`;
- worst daily maximum drawdown: `-9.956967%`;
- median actual equity exposure: `26.6333%`;
- drawdown gate: `48/48` pass;
- capital gate: `0/48` pass.

The 38% bear cap narrowly failed at `-10.02%`.  Higher bear caps conditioned on
strong PBoC and direction scores, removal of caps from price/leverage clusters,
and a relaxed 24% price/leverage cap all failed the full drawdown matrix.  They
remain research ablations and are not used by target generation.

## Direction Aggregation and Path-Risk Audit (2026-07-17)

The low-parameter binned direction screen previously ranked policies by the
minimum selected-group return and a median within-path spread without requiring
enough rejected observations.  That aggregation was misleading: the apparent
`+4.91%` median spread policy selected almost every scored quarter, while the
pooled selected and rejected returns were `+3.58%` and `+8.43%`, respectively.
The screen now enforces balanced per-path samples and ranks the pooled return
spread.  None of the 135 tested binned return models has a positive pooled
spread; the best is still `-0.13%`.

The strict engine also now records each frozen counterfactual ETF basket's
within-quarter maximum drawdown.  This exposes quarters such as 2009-Q1 that
finish up roughly 26%--32% but suffer about 13.4% peak-to-trough risk.  A
strictly walk-forward path-risk gate using 24 completed quarters and six
domestic features separates safer direction-positive windows offline, but its
best compliant full-matrix result reaches only `744.3w` minimum capital with
`-9.93%` worst drawdown.  Raising the gated direction multiplier to 1.50
reaches `752.9w` but fails the drawdown gate at `-10.17%`.  The gate and the
return-bin variants therefore remain rejected research diagnostics and do not
replace the `764.69w / -9.957%` frontier or production target generation.
