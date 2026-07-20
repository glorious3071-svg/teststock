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

## Structural Adaptation Gate Progress

Latest strict quarterly ETF-only candidate:

- report: `data/backtests/scorecard_csi_strict_quarterly_v9_def_bond_gold65_structreentry50_multistate_s10_hcres100_structblend85_exhaustfallback_shockres100_earlyres50_report.json`
- direct policy: `blend_index_weighted_stable_v9_structural_multistate_repair_top3_s10_rotcond35_hcres100_structblend85_exhaustfallback_shockres100_earlyres50_regime_w49_s92`
- original hard gate: 48/48 pass, minimum final capital 4795.0w, worst max drawdown -18.55%
- structural adaptation validation: not passed
- recent survival: 39/48 pass, worst 2016-2025 annualized return 12.97%, worst 2021-2025 cumulative return 44.33%, worst rolling 5-year annualized return 3.26%
- structural capture: 0/48 pass under the current all-applicable-quarters capture gate

The new `_exhaustfallback` repair is point-in-time and stays inside the strict asset boundary. It triggers only when broad CSI/HS300 momentum is weak, the ETF basket still has a local winner, breadth is narrow, crisis flags are absent, and same-snapshot SHARE_V5 observations show a crowded broad-growth ETF with poor short-tail risk. Under that setup the direct ETF sleeve can switch to domestic broad-value, finance, and industrial ETF proxies scored from ETF price history only.

Observed effect:

- 2014-02-14 in the phase 2 / lag 5 worst rolling window improved from a risk-sleeve loss near -11.28% to -6.13%.
- The binding rolling 5-year window remains 2009-08-10 through 2014-05-12; the next largest drag is still 2011-05-10, followed by 2012-05/2012-08.
- The best idealized structural selector diagnostics remain far below the current all-quarter structural capture gate: `multistate_rotation_v1` top3 reached about 53.5% capture-pass, while top10 diluted to about 28.2%.

Conclusion:

- `_exhaustfallback` is a valid incremental candidate, not a solved strategy.
- The remaining recent-survival blocker is the 2009-2014 rolling window, especially 2011/2012.
- The remaining structural blocker is selector quality and weighting lag, not just exposure: applicable structural quarters usually have enough exposure and frequently beat HS300, but they do not consistently capture 30% of the forward top10 ETF basket.

Path-risk safe-gate block experiment:

- report: `data/backtests/scorecard_csi_strict_quarterly_v9_exhaustfallback_pathriskblock_report.json`
- change tested: keep the leverage-crowding risk cap active when `leverage_macro_divergence_flag` is present and the path-risk gate score is <= -0.10, instead of letting the safe gate relax the cap to 1.0
- original hard gate: 48/48 pass, minimum final capital 4960.8w, worst max drawdown -18.55%
- structural adaptation validation: not passed
- recent survival: 36/48 pass, worst 2016-2025 annualized return 12.77%, worst 2021-2025 cumulative return 40.66%, worst rolling 5-year annualized return 4.46%
- structural capture: 0/48 pass

This confirms that the 2011/2012 rolling-window failure is partly a safe-gate over-relaxation problem: blocking the leverage-crowding safe gate moves the worst rolling 5-year result close to the 5% floor. It is still not an accepted candidate because the later 2021-2025 survival distribution deteriorates and the structural-capture gate remains fully failed.

Additional structural selector experiments:

- `data/backtests/scorecard_csi_strict_quarterly_v9_exhaustfallback_pathriskblock_margin_report.json`
  - tightened the `_exhaustfallback` direct-share trigger by requiring selector score margin >= 0.03, so the 2014 crowded-growth fallback remains active while the 2021 narrow-new-energy episode is not forced into the value fallback
  - original hard gate: 48/48 pass, minimum final capital 5114.2w, worst max drawdown -18.55%
  - structural adaptation validation: not passed, recent 32/48, structural 0/48, worst rolling 5-year annualized return 4.50%
- `data/backtests/scorecard_csi_strict_quarterly_v9_cooling_rot100_structblend85_pathriskblock_report.json`
  - tested a cooling-rotation repair that gives the whole structural repair sleeve to same-snapshot technology/semiconductor cooling scores during broad-participation rotation
  - rejected: 28/48 pass, minimum final capital 4885.0w, worst max drawdown -21.40%
- `data/backtests/scorecard_csi_strict_quarterly_v9_cooling_rot50_structblend85_probe_report.json`
  - tested a milder 50% cooling-rotation repair through replay
  - rejected: 32/48 pass, minimum final capital 5140.6w, worst max drawdown -21.72%

The cooling route improves the targeted 2019 technology/semiconductor recognition in isolation, but it breaks the original max-drawdown hard gate. The next viable path should not simply increase structural-tech concentration; it needs a risk-aware mainline selector that captures local themes without raising full-path drawdown above 20%.

Risk-aware local-mainline diagnostic:

- `data/backtests/scorecard_csi_structural_selector_candidates_riskaware_top3.json`
- diagnostic-only recipe: `risk_aware_local_mainline_v1`
- result: top3 hit rate 33.8%, capture-pass 38.0%, median capture 1.65%, HS300 win rate 56.3%
- comparison: existing `multistate_rotation_v1` top3 remains better at hit rate 68.7%, capture-pass 53.5%, median capture 35.2%, HS300 win rate 81.7%

This first risk-aware selector is too defensive and should not be promoted. It confirms that the remaining work is not a simple tail-risk penalty; the scorecard needs a state switch that preserves technology/sector leadership recognition in 2019/2023-style quarters while avoiding the 2018 and 2020 drawdown paths that caused cooling concentration to fail the original hard gate.

State-switch diagnostic:

- compared `multistate_rotation_v1` and `cooling_rotation_v1` on applicable structural quarters from `scorecard_csi_strict_quarterly_v9_exhaustfallback_pathriskblock_margin_report.json`
- cooling was clearly better in some 2023-03 structural quarters, with top3 returns around 8.8%-11.3% versus multistate losses around -4.4% to -5.9%
- cooling was clearly worse in other 2023-01 and 2021-06 structural quarters, where multistate captured the true local mainline but cooling rotated into defensive/value proxies
- simple point-in-time filters using selector margin, 6-month basket max return, and 6-month breadth can lift ideal top3 capture-pass to about 64.8%, but still leave roughly two dozen structural rows with selected top3 returns below -8%

Conclusion: a single cooling-versus-multistate state switch is not reliable enough. The next diagnostic should classify *which* local mainline family is active (new energy/semiconductor/AI/healthcare/value) rather than switch between broad recipes. The scoring problem is family identification plus family-specific crowding control, not merely risk-on versus risk-off.

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

## Strict ETF-Only 4000w / 20% Drift Frontier (2026-07-19)

The mdd20 frontier now passes the full strict quarterly drift matrix:

- rule: `q_mdd20_qfree_stack_highdist800`;
- CSI selector: `expanded_value_risk_top7_power8_cap45`;
- direct/index ETF blend: `blend_index_weighted_stable_v9_roe050_top1_regime_w49_s92`;
- defense: `bondfine_91d_vp41_top1_min-50`;
- phase/lag matrix: `12 x 4 = 48` cases;
- minimum final capital: `4061.5153w`;
- median final capital: `4722.8414w`;
- maximum final capital: `6145.6097w`;
- worst daily maximum drawdown: `-19.718368%`;
- median maximum drawdown: `-18.532675%`;
- objective: `48/48` cases pass `final_capital >= 4000w` and `max_drawdown >= -20%`.

The final stack was built by repeatedly auditing the worst surviving sample:

- `q_mdd20_qfree_coldcap800` fixed early cold-start underexposure but left
  phase-0/lag-0 and phase-1/lag-5 return holes.
- `high_level_distribution_reentry` restored a capped position during
  2009-style policy-supported high-level distribution windows.
- `policy_repair_crisis_reentry` restored exposure in 2010-style crisis
  continuation windows when PBoC tone and direction turned positive.
- `feature_recovery_relaxation` relaxed the ETF-share growth cap only in
  no-flag repair windows with positive direction and path-risk gates.
- `neutral_downtrend_cap` capped scorecard-neutral downtrends; in audit it
  triggered 40 times, all in negative next-quarter risk-return windows.
- `crisis_strength_floor` restored capped exposure in non-early crisis windows
  with strong basket relative strength, positive breadth, and a positive
  basket 3-month moving-average distance.
- Re-testing the now-active high-level distribution cap at `0.80` removed the
  final phase-0/lag-0 capital shortfall.

The validation artifact is
`data/backtests/scorecard_csi_strict_quarterly_mdd20_qfree_stack_highdist800_decision_audit_report.json`.
Its summary has `objective_met=True`, `pass_count=48`, `count=48`,
`case_matrix.failed_cases=[]`, and constraints explicitly set
`domestic_passive_etf_only=True`, `no_overseas_assets=True`, `no_options=True`,
`no_futures=True`, `no_shorting=True`, and frozen quarterly weights.

Target generation now uses the same backtest functions instead of a separate
manual exposure replica.  `scripts/generate_scorecard_csi_strict_quarterly_targets.py`
loads the final rule and calls `build_daily_path` plus `evaluate_path`, then
writes:

- `data/portfolio/scorecard_csi_strict_quarterly_targets_latest.json`;
- `data/portfolio/scorecard_csi_strict_quarterly_targets_latest.csv`.

The current generated target file is based on snapshot `2024-11-30`,
execution date `2024-12-05`, exposure `56.25%`, and strict asset validation
passes with no violations.

Performance notes: subprocess-level parallelism helped CPU utilization but
was memory-inefficient because each worker loaded and decoded the same 2.8GB
path-cache JSON into roughly 9GB--12GB of Python objects.  The faster stable
search mode is to pass multiple `--rule` values to
`scripts/backtest_scorecard_csi_strict_quarterly_etf.py`, which loads the path
cache once and evaluates all selected rules in one process.  Further speedup
should come from a lighter binary or sharded path cache and shared in-process
rule evaluation, not from simply increasing subprocess count.

## Structural Adaptation Gate (2026-07-19)

The 20% frontier is now treated as the first layer, not the final objective.
`scripts/validate_scorecard_csi_structural_adaptation.py` adds a second hard
gate for recent survival and structural-market capture.  It consumes a strict
quarterly `--include-decision-rows` report, loads the point-in-time domestic
passive ETF universe from MySQL, and evaluates:

- 2016-2025 annualized return floor: `>=8%` for every phase/lag case;
- 2021-2025 cumulative return floor: `>=30%` and max drawdown `>=-20%`;
- every rolling 5-year window annualized return: `>=5%`;
- every rolling 3-year window max drawdown: `>=-18%`;
- no more than seven consecutive quarters below the inferred cash/defense leg;
- structural quarters where CSI 300 return is below `5%`, ETF top-20% average
  return minus median return is at least `8%`, top-10 ETFs are broadly positive,
  and the market is not in a systemic crash;
- in those structural quarters: median equity exposure `>=50%`, capture ratio
  versus the top-10 ETF equal basket `>=30%`, CSI 300 win rate `>=60%`, and no
  prolonged low-exposure state unless a strong risk-ban signal is active.

First run:

```bash
.venv/bin/python scripts/validate_scorecard_csi_structural_adaptation.py \
  data/backtests/scorecard_csi_strict_quarterly_mdd20_qfree_stack_highdist800_decision_audit_report.json \
  --output-prefix data/backtests/scorecard_csi_strict_quarterly_mdd20_qfree_stack_highdist800_structural_adaptation
```

Result:

- `adaptation_objective_met=False`;
- original 20-year hard gate: passed;
- recent-survival gate: `15/48` cases pass;
- structural-capture gate: `0/48` cases pass;
- worst 2016-2025 annualized return: `9.31%`, so the 10-year floor passes;
- worst 2021-2025 cumulative return: `20.43%`, below the `30%` floor;
- worst rolling 5-year annualized return: `2.69%`, below the `5%` floor;
- worst rolling 3-year max drawdown: `-17.68%`, inside the `-18%` floor;
- max consecutive quarters below defense: `5`, inside the eight-quarter limit.

The first failed structural diagnosis is not an original hard-gate failure.
The worst structural case is phase `10`, lag `0`: median structural exposure is
`56.25%` and CSI 300 win rate is `85.7%`, but capture pass rate is only `7.1%`.
Its worst missed quarter starts `2020-01-02`: the top-10 ETF equal basket
returned `+7.11%`, CSI 300 returned `-11.49%`, and the portfolio returned
`-7.02%` despite full exposure.  The selected ETF set had no overlap with the
ex-post top-10 ETF basket, so the diagnosis is `scorecard_missed_mainline`.

Aggregate failed structural-quarter attribution:

- `risk_control_low_exposure`: 349 quarters;
- `scorecard_missed_mainline`: 204 quarters;
- `rebalance_or_weighting_lag`: 180 quarters.

The first mainline-selector upgrade is now implemented in
`weighted_structural_mainline_scores`.  It uses point-in-time fields for
3m/6m relative strength, ETF cross-section participation, liquidity and ETF
share-flow changes, valuation and earnings repair, policy score, lower CSI 300
correlation, and crowding/high-level risk.  It is wired into these direct ETF
policies:

- `direct_structural_mainline_top3`;
- `direct_structural_mainline_top5`;
- `blend_index_structural_mainline_top3_regime_w49_s92`;
- `blend_index_structural_mainline_top5_regime_w49_s92`.

The diagnostic is useful but the direct overlay is not yet valid.  Replacing
the original `blend_index_weighted_stable_v9_roe050_top1_regime_w49_s92` direct
sleeve with the structural-mainline sleeve at the same `49% / 92%` blend broke
the first-layer hard gate:

- top3 structural-mainline blend: `0/48`, minimum final capital `1677.8w`,
  worst max drawdown `-32.28%`;
- top5 structural-mainline blend, execution-timing retest: `0/48`, minimum
  final capital `1780.4w`, worst max drawdown `-31.30%`.

Lower fixed weights and a point-in-time conditional trigger also failed the
first layer:

- top5 structural-mainline fixed `10%` blend: `0/48`, minimum final capital
  `2724.7w`, worst max drawdown `-21.56%`;
- top5 structural-mainline fixed `20%` blend: `0/48`, minimum final capital
  `2559.3w`, worst max drawdown `-22.06%`;
- top5 structural-mainline conditional `10%` blend, active only when prior
  market state showed weak CSI 300, high ETF dispersion, strong top-basket
  return, broad positive ETF participation, and no crisis/liquidity-stress
  flag: `0/48`, minimum final capital `2804.1w`, worst max drawdown `-21.06%`.

The next viable shape was a narrow repair sleeve that preserves the original
`blend_index_weighted_stable_v9_roe050_top1_regime_w49_s92` direct allocation
and diverts only `5%` of that direct sleeve into risk-filtered structural
mainline top5 ETFs:

- policy:
  `blend_index_weighted_stable_v9_structural_repair_top5_s05_regime_w49_s92`;
- first-layer hard gate remains valid: `48/48` cases pass, minimum final
  capital `4048.6w`, worst max drawdown `-19.75%`;
- recent-survival gate is unchanged at `15/48` cases pass;
- structural-capture gate remains `0/48` cases pass;
- worst 2016-2025 annualized return improves slightly to `9.34%`;
- worst 2021-2025 cumulative return slips to `20.12%`, still below the `30%`
  floor;
- worst rolling 5-year annualized return improves to `2.81%`, still below the
  `5%` floor;
- worst rolling 3-year max drawdown is `-17.66%`, inside the `-18%` floor;
- max consecutive quarters below defense stays at `5`.

The repair sleeve improves the attribution mix but not the hard objective.
Aggregate failed structural-quarter attribution moves to:

- `risk_control_low_exposure`: 349 quarters;
- `scorecard_missed_mainline`: 144 quarters;
- `rebalance_or_weighting_lag`: 244 quarters.

The worst repair structural case is phase `10`, lag `3`: median structural
exposure is `56.25%`, CSI 300 win rate is `94.1%`, and structural-mainline
top5 overlap is `52.9%`, but capture pass rate falls to `5.9%`.  Its worst
missed quarter starts `2018-01-05`: the top-10 ETF equal basket returned
`+4.06%`, CSI 300 returned `-6.91%`, and the portfolio returned `-5.45%`.
The diagnosis is `rebalance_or_weighting_lag`, not pure scorecard blindness.
This means the point-in-time structural score can identify more local
mainlines, but the current quarterly execution and tiny repair weight do not
translate that signal into enough captured return.

`scripts/analyze_scorecard_csi_structural_selector_candidates.py` now compares
point-in-time structural selector recipes against structural-quarter ex-post
top10 ETF labels.  The ex-post basket is used only for evaluation, not as an
input feature.  On the `s05` repair audit:

```bash
.venv/bin/python scripts/analyze_scorecard_csi_structural_selector_candidates.py \
  data/backtests/scorecard_csi_strict_quarterly_v9_structural_repair_s05_decision_audit_report.json \
  --output data/backtests/scorecard_csi_structural_selector_candidate_analysis_s05.json
```

Top5 diagnostic result:

- current structural-mainline score: hit `41.26%`, capture-pass `19.92%`,
  median capture `-15.78%`, CSI 300 win rate `54.27%`;
- `liquidity_flow_v2`: hit `45.33%`, capture-pass `24.59%`, median capture
  `-6.07%`, CSI 300 win rate `58.54%`;
- `momentum_breadth_v2`: hit `32.11%`, capture-pass `27.24%`, median capture
  `-7.75%`, CSI 300 win rate `50.41%`;
- `low_corr_trend_v2`: hit `44.31%`, capture-pass `23.98%`, median capture
  `-10.59%`, CSI 300 win rate `55.28%`.

The top3/top10 cross-check is mixed: `momentum_breadth_v2` has the best top3
capture-pass rate (`32.52%`), while the current score still has the best top10
hit rate (`56.91%`) and capture-pass rate (`21.54%`).  Because top5 is the
repair-sleeve shape and `liquidity_flow_v2` improves both hit rate and
capture-pass rate there, it is now implemented as
`weighted_structural_liquidity_flow_scores` and wired into the explicit policy
`blend_index_weighted_stable_v9_structural_flow_repair_top5_s05_regime_w49_s92`.
This is not yet a passed portfolio result; it is the next candidate to validate
through the 48-sample hard gate and then the structural-adaptation gate.

First full-backtest attempt for the flow-repair policy was interrupted before
any report or path cache was written.  The stack showed the run was still
inside CSI snapshot selector MySQL candidate preloading, not inside the new
ETF score.  Because the existing `s05` decision audit already contains
`index_target_weights`, `rebalance_anchor`, `decision_date`, `exposure`, and
`market_state`, the next engineering step should be a replay validator that
reuses the audited CSI/market-risk path and recomputes only ETF mapping,
direct-policy weights, realized ETF returns, transaction costs, and drawdown.
That will give authoritative 48-sample evidence for new direct ETF policies
without rebuilding the unchanged CSI selector path.

`scripts/replay_scorecard_csi_strict_quarterly_etf_direct_policy.py` now
implements that ETF-layer replay.  Calibration against the existing `s05`
policy is close enough for direct-policy iteration:

```bash
.venv/bin/python scripts/replay_scorecard_csi_strict_quarterly_etf_direct_policy.py \
  data/backtests/scorecard_csi_strict_quarterly_v9_structural_repair_s05_decision_audit_report.json \
  --direct-etf-policy blend_index_weighted_stable_v9_structural_repair_top5_s05_regime_w49_s92 \
  --output-prefix data/backtests/scorecard_csi_strict_quarterly_v9_structural_repair_s05_replay_calibration
```

- source audit: `48/48`, minimum final capital `4048.6w`, worst max drawdown
  `-19.75%`;
- replay calibration: `48/48`, minimum final capital `4055.7w`, worst max
  drawdown `-19.75%`.

Flow-repair replay:

```bash
.venv/bin/python scripts/replay_scorecard_csi_strict_quarterly_etf_direct_policy.py \
  data/backtests/scorecard_csi_strict_quarterly_v9_structural_repair_s05_decision_audit_report.json \
  --direct-etf-policy blend_index_weighted_stable_v9_structural_flow_repair_top5_s05_regime_w49_s92 \
  --output-prefix data/backtests/scorecard_csi_strict_quarterly_v9_structural_flow_repair_s05_replay

.venv/bin/python scripts/validate_scorecard_csi_structural_adaptation.py \
  data/backtests/scorecard_csi_strict_quarterly_v9_structural_flow_repair_s05_replay_report.json \
  --output-prefix data/backtests/scorecard_csi_strict_quarterly_v9_structural_flow_repair_s05_replay_structural_adaptation
```

- first-layer hard gate remains valid: `48/48`, minimum final capital
  `4045.1w`, worst max drawdown `-19.74%`;
- recent-survival gate remains `15/48`;
- structural-capture gate remains `0/48`;
- worst 2016-2025 annualized return: `9.33%`;
- worst 2021-2025 cumulative return: `19.75%`;
- worst rolling 5-year annualized return: `2.83%`;
- aggregate structural attribution: `risk_control_low_exposure` 349 quarters,
  `scorecard_missed_mainline` 132 quarters, and
  `rebalance_or_weighting_lag` 256 quarters.

The flow score improves the actual selected-mainline miss count, but the hard
adaptation objective is still not met.  The next search should focus on the
execution/weight-transfer problem: the signal can increasingly identify local
mainlines, but a small quarterly repair sleeve does not capture enough of the
subsequent top-basket return.

A broader flow-repair top10 replay reduced the missed-mainline attribution
again, but still did not meet either new hard gate:

```bash
.venv/bin/python scripts/replay_scorecard_csi_strict_quarterly_etf_direct_policy.py \
  data/backtests/scorecard_csi_strict_quarterly_v9_structural_repair_s05_decision_audit_report.json \
  --direct-etf-policy blend_index_weighted_stable_v9_structural_flow_repair_top10_s05_regime_w49_s92 \
  --output-prefix data/backtests/scorecard_csi_strict_quarterly_v9_structural_flow_repair_top10_s05_replay

.venv/bin/python scripts/validate_scorecard_csi_structural_adaptation.py \
  data/backtests/scorecard_csi_strict_quarterly_v9_structural_flow_repair_top10_s05_replay_report.json \
  --output-prefix data/backtests/scorecard_csi_strict_quarterly_v9_structural_flow_repair_top10_s05_replay_structural_adaptation
```

- first-layer replay remains valid: `48/48`, minimum final capital `4046.8w`,
  worst max drawdown `-19.74%`;
- recent-survival gate remains `15/48`;
- structural-capture gate remains `0/48`;
- worst 2016-2025 annualized return is `9.27%`;
- worst 2021-2025 cumulative return is `19.67%`;
- worst rolling 5-year annualized return is `2.80%`;
- aggregate structural attribution becomes `risk_control_low_exposure` 349
  quarters, `scorecard_missed_mainline` 120 quarters, and
  `rebalance_or_weighting_lag` 264 quarters.

This confirms that simply widening the repair basket helps selector overlap but
worsens the weight-transfer lag.  It is not a viable route to the new objective.

`scripts/analyze_scorecard_csi_structural_opportunity_triggers.py` now evaluates
point-in-time opportunity triggers against future structural-quarter labels.
The future ETF cross-section is used only as the evaluation label, not as an
input feature:

```bash
.venv/bin/python scripts/analyze_scorecard_csi_structural_opportunity_triggers.py \
  data/backtests/scorecard_csi_strict_quarterly_v9_structural_repair_s05_decision_audit_report.json \
  --output data/backtests/scorecard_csi_structural_opportunity_trigger_analysis_s05.json
```

Key trigger diagnostics on `3840` decision rows:

- existing narrow trigger: `128` fires, applicable structural recall `4.9%`,
  worst-case recall `0.0%`, systemic-crash false positives `4`;
- existing wide trigger: `944` fires, applicable structural recall `57.0%`,
  worst-case recall `36.4%`, systemic-crash false positives `72`, and it still
  does not catch the 2020Q1 worst miss;
- low-dispersion rotation setup: `192` fires, applicable structural recall
  `12.7%`, systemic-crash false positives `0`, and it does catch the 2020Q1
  worst miss;
- best simple 3-month grid trigger:
  `grid_3m_csi20_disp000_max04_br40_dd12`, `1512` fires, applicable structural
  recall `81.0%`, worst-case recall `66.7%`, but systemic-crash false positives
  are still `76`.

The 2020Q1 miss is informative: at the 2019-12-31 signal date, 3-month ETF
dispersion was only `0.92%`, but the top ETF had already returned `10.7%`,
3-month positive breadth was `100%`, selected ETF 3-month momentum was `6.3%`,
and selector score margin was `1.9%`.  That is an early rotation setup rather
than an already-dispersed structural market.

The early rotation setup was wired into three conditional top10 flow-repair
policies that raise the repair share from `5%` to `10%`, `15%`, or `20%` when
either the existing wide trigger or the early rotation setup is active:

- `rotcond10`: first-layer replay passes, `48/48`, minimum final capital
  `4048.5w`, worst max drawdown `-19.84%`; adaptation still fails with
  `recent=15/48`, `structural=0/48`, worst 2021-2025 cumulative return
  `18.97%`, and worst rolling 5-year annualized return `2.88%`;
- `rotcond15`: first-layer replay passes, `48/48`, minimum final capital
  `4050.0w`, worst max drawdown `-19.94%`; adaptation still fails with
  `recent=15/48`, `structural=0/48`, worst 2021-2025 cumulative return
  `18.28%`, and worst rolling 5-year annualized return `2.92%`;
- `rotcond20`: first-layer replay fails with `44/48`; minimum final capital is
  `4051.3w`, but worst max drawdown breaches the hard gate at `-20.04%`.

The rotation-trigger path can catch the 2020Q1 setup, but increasing the repair
share inside the existing direct ETF sleeve either leaves structural capture at
`0/48` or breaches the original max-drawdown gate.  This route is now bounded:
the next search needs a different transfer mechanism or a different recent
survival solution, not a larger conditional repair share.

An early-only variant then raised the top10 flow-repair share only when the
low-dispersion rotation setup fired, excluding the broader wide trigger:

- `earlyrotcond10`: first-layer replay passes, `48/48`, minimum final capital
  `4037.6w`, worst max drawdown `-19.78%`; adaptation still fails with
  `recent=15/48`, `structural=0/48`, worst 2021-2025 cumulative return
  `19.68%`, and worst rolling 5-year annualized return `2.88%`;
- `earlyrotcond20`: first-layer replay fails with `44/48`, minimum final
  capital `4019.2w`, worst max drawdown `-20.13%`;
- `earlyrotcond50`: first-layer replay fails with `39/48`, minimum final
  capital `3963.8w`, worst max drawdown `-21.23%`.

The early-only transfer improves the 2020Q1 miss mechanically (`earlyrotcond50`
cuts that quarter from roughly `-6.6%` to `-3.4%`), but it creates a worse
2018 drawdown cluster.  The failing cases bottom on 2018-10-18 after the
2018-08-06 rebalance, where the replacement sleeve is concentrated in ETFs such
as `510190.SH`, `159919.SZ`, `510880.SH`, and `510010.SH`.  The usable boundary
is therefore below `20%`, and the only passing point (`10%`) still leaves the
new hard gates unchanged.

Conditional repair-share tests tried to increase the structural-flow repair
share only when a point-in-time opportunity trigger was active.  The narrow
trigger reused weak CSI 300, high ETF dispersion, strong top-basket return,
broad positive participation, shallow basket drawdown, and no crisis/liquidity
stress.  It covered only `28/492` applicable structural-quarter rows, so
`cond20` and `cond35` changed the portfolio too rarely:

- `cond20`: first-layer replay `48/48`, minimum final capital `4041.8w`,
  worst max drawdown `-19.74%`, but adaptation still `recent=15/48` and
  `structural=0/48`;
- `cond35`: first-layer replay `48/48`, minimum final capital `4038.5w`,
  worst max drawdown `-19.74%`, but adaptation still `recent=15/48` and
  `structural=0/48`.

A wider point-in-time trigger was then fit only as an opportunity trigger,
not as a selector label: CSI 300 3-month return `<16%`, ETF 3-month dispersion
`>=3%`, top ETF 3-month return `>=6%`, positive breadth `>=50%`, basket
6-month drawdown `>-12%`, and no crisis/liquidity stress.  It covers `268/492`
applicable structural rows while firing on `944/3840` total decisions.  Replay
results:

- `widecond20`: first-layer replay remains valid at `48/48`, minimum final
  capital `4098.0w`, worst max drawdown `-19.91%`, but adaptation still
  `recent=15/48` and `structural=0/48`; worst 2021-2025 cumulative return
  falls to `17.73%`;
- `widecond35`: first-layer replay fails with `44/48`, minimum final capital
  `4149.3w`, worst max drawdown `-20.08%`.

The conditional-share path therefore does not solve the new objective.  It can
raise long-horizon compounding in some cases, but it either leaves structural
capture unchanged or worsens the recent-survival constraint.  The next viable
direction is not a larger repair share; it needs a different transfer
mechanism that changes which ETF sleeve receives weight without increasing
drawdown or degrading 2021-2025 survival.

Recent-survival diagnostics now use
`scripts/analyze_scorecard_csi_recent_survival_windows.py`:

```bash
.venv/bin/python scripts/analyze_scorecard_csi_recent_survival_windows.py \
  data/backtests/scorecard_csi_strict_quarterly_v9_structural_repair_s05_decision_audit_report.json \
  --output data/backtests/scorecard_csi_strict_quarterly_v9_structural_repair_s05_recent_survival_diagnosis.json
```

On the current `s05` hard-gate candidate, `33/48` cases fail recent survival.
The worst rolling 5-year windows are not only 2021-2025:

- phase `10`, lag `0`: worst rolling window `2015-04-01` to `2020-01-02`,
  annualized return `2.81%`, average exposure `36.4%`, median exposure `18.3%`,
  low-exposure quarters `13/20`;
- phase `2`, lag `5`: worst rolling window `2009-08-10` to `2014-05-12`,
  annualized return `2.97%`, average exposure `40.9%`, median exposure `50.0%`,
  low-exposure quarters `9/20`.

The 2015-2020 low-return window is mostly a low-exposure problem, with binding
decrease stages `risk_flag_cap` 3 times, `hard_exit` 2 times,
`feature_exposure_cap` 1 time, and `neutral_downtrend_cap` 1 time.  The
2009-2014 window is more about `direction_risk_gate_rejection_cap` 8 times plus
`risk_flag_cap` 6 times.  But a simple low-exposure re-entry floor is not viable:
`scripts/analyze_scorecard_csi_low_exposure_reentry_triggers.py` shows that
raising qualifying low-exposure quarters to a `50%` floor has negative aggregate
incremental return for all tested point-in-time triggers.  The least-bad trigger,
`selected_strength_no_hard_exit`, fires `369` times, has positive incremental
return only `56.4%` of the time, and still sums to `-2.79` incremental return
points, with worst misses around the June 2015 overheat collapse.

The replay script now accepts `--defensive-policy`, allowing audited exposure
and equity ETF decisions to be held fixed while only the domestic defensive ETF
sleeve changes.  Replacing the original `bondfine_91d_vp41_top1_min-50`
defensive policy produced the largest recent-survival improvement so far:

- `bondfine_91d_vp41_top3_min-50`: first-layer replay `48/48`, minimum final
  capital `4052.6w`, worst max drawdown `-18.75%`;
- `bond_126d_vp41_top3`: `48/48`, minimum final capital `4112.1w`, worst max
  drawdown `-18.75%`;
- `bond_gold35_252d_gold42`: `48/48`, minimum final capital `4330.3w`, worst
  max drawdown `-18.79%`, recent survival `16/48`;
- `bond_gold45_252d_gold42`: `48/48`, minimum final capital `4423.4w`, worst
  max drawdown `-18.80%`, recent survival `16/48`;
- `bond_gold65_252d`: `48/48`, minimum final capital `4502.7w`, worst max
  drawdown `-18.82%`, recent survival `24/48`, worst 2021-2025 cumulative
  return `33.0%`, but worst rolling 5-year annualized return remains only
  `2.76%`.

Combining `bond_gold65_252d` with stronger structural flow repair used that
extra drawdown budget:

- `bond_gold65 + flow_top10_s05`: first-layer replay `48/48`, minimum final
  capital `4492.9w`, worst max drawdown `-18.83%`, adaptation `recent=24/48`,
  `structural=0/48`;
- `bond_gold65 + rotcond20`: first-layer replay `48/48`, minimum final capital
  `4498.6w`, worst max drawdown `-19.20%`, adaptation `recent=28/48`,
  `structural=0/48`, worst 2021-2025 cumulative return `31.5%`, worst rolling
  5-year annualized return `2.69%`;
- `bond_gold65 + rotcond35`: first-layer replay `48/48`, minimum final capital
  `4502.2w`, worst max drawdown `-19.58%`, but adaptation remains
  `recent=28/48`, `structural=0/48`, and 2021-2025 worst cumulative return
  slips below the floor to `29.95%`;
- `bond_gold65 + rotcond50`: first-layer replay `48/48`, minimum final capital
  `4504.1w`, worst max drawdown `-19.96%`, but adaptation remains
  `recent=28/48`, `structural=0/48`, with 2021-2025 worst cumulative return
  `28.4%`.

The current best combined candidate is therefore `bond_gold65 + rotcond20`.
It solves the 2021-2025 cumulative-return floor and improves first-layer margin,
but it still fails the rolling 5-year survival floor and all structural-capture
cases.  Its worst structural case is phase `3`, lag `0`, with median structural
exposure `69.8%`, capture pass rate `9.1%`, benchmark win rate `81.8%`, and the
worst quarter starting `2020-03-02`; this is now mostly a weight-transfer and
scorecard-mainline problem, not a defensive-sleeve problem.

An oracle upper-bound diagnostic replaced the risky leg with the ex-post top10
ETF equal basket only in structural quarters, while keeping the audited
exposure and defense leg fixed.  This is not an implementable strategy; it is
a feasibility diagnostic.  Even this oracle still failed the new hard gates:

- first-layer gate would pass easily: `48/48`, minimum final capital
  `6659.4w`, worst max drawdown `-15.39%`;
- recent survival improves but still fails: `35/48`, worst rolling 5-year
  annualized return `2.97%`;
- structural capture still fails: `0/48`; worst case capture-pass rate is
  only `50.0%`;
- `148/492` applicable structural rows still fail the 30% capture threshold
  even with top10 ETF selection because the existing exposure/defense path is
  too defensive.

That result proves ETF selection alone cannot satisfy the structural
adaptation gate under the existing exposure path.

A low-capture profile split the `492` applicable structural-quarter rows by the
30% capture threshold.  Only `68` rows cleared the threshold, while `424` failed.
Among the failures, the dominant causes were `rebalance_or_weighting_lag` 170,
`risk_control_low_exposure` 165, and `scorecard_missed_mainline` 89.  Rows that
cleared the threshold already looked much stronger at the point-in-time signal
date: median exposure `100.0%` vs `56.25%`, CSI 300 3-month return `13.67%` vs
`5.50%`, ETF basket 3-month return `16.38%` vs `9.84%`, basket 6-month return
`31.05%` vs `19.71%`, selected ETF 3-month momentum `12.28%` vs `6.63%`, and
selector score margin `5.34%` vs `1.46%`.  The hardest misses are therefore
not just absent selector labels; they are weak or late signals under the current
quarterly transfer path.

Rule-level structural opportunity exposure floors were then tested using the
existing `s05` path cache, so the CSI selector path was not rebuilt.  A pre-cap
floor (`structfloor50/60`) was neutralized by later soft caps: `structfloor60`
was active 660 times, raised exposure 24 times, and all 24 raises were then
overridden by `direction_risk_gate_rejection_cap` or `risk_flag_cap`; the
portfolio result stayed identical to the original `s05`.

A post-cap floor was inserted after soft feature/risk caps but before raw and
hard exits:

- `structpost60`: first-layer fails, `46/48`, minimum final capital `3942.6w`;
- `structpost50`: first-layer fails, `47/48`, minimum final capital `3989.1w`;
- `structpost45`: first-layer fails, `47/48`, minimum final capital `3995.5w`;
- `structpost42`: first-layer fails, `47/48`, minimum final capital `3999.4w`;
- `structpost40`: first-layer passes, `48/48`, minimum final capital
  `4002.0w`, worst max drawdown `-19.75%`, but adaptation still
  `recent=15/48` and `structural=0/48`.

`structpost40` raised exposure 64 times without later override, but structural
capture attribution did not change (`risk_control_low_exposure` 349,
`rebalance_or_weighting_lag` 244, `scorecard_missed_mainline` 144).  The
post-cap exposure floor therefore consumes almost all long-horizon safety
margin without fixing the structural-capture gate.  The remaining failure is
not solved by ETF selection, repair-share sizing, or a broad exposure floor in
the current framework.

The same score remains useful as a missed-mainline diagnostic.  In the current
48/48 hard-gate candidate, structural-mainline top5 would overlap the ex-post
top10 ETF basket in `41.26%` of applicable structural quarters; in the worst
structural case, phase `10` lag `0`, it overlaps in `57.14%` of applicable
structural quarters.  For the worst missed quarter starting `2020-01-02`, the
new score selected `512480.SH` inside the ex-post top10 basket, while the
actual held sleeve had no overlap.  The next optimization should therefore
keep the original 20-year gate fixed and test the structural score as a
narrower diagnostic or a risk-filtered replacement candidate, not as a direct
blend sleeve.  Candidates should still be ranked by the worst recent rolling
window plus the worst structural capture case, not by average full-period
return.

The structural-capture validator was then tightened to match the stated
exception: low exposure is excluded only when a strong broad-market risk ban is
active.  The old validator recognized bear state plus systemic risk flags, but
missed scorecard-level hard defensive caps such as
`stagflation_defensive_cap`, `cycle_midpoint_scorecard_risk_reduce`,
`weak_momentum_exhaustion_cap`, `weak_repair_trap_cap`, and
`cycle_midpoint_weak_pmi_trailing6m_rally_cap` when `allocation_entry=false`.
Ordinary scheduled refreshes and actively allocated weak-repair caps are not
treated as strong-risk exemptions.

Revalidating the current best candidate
`bond_gold65 + structural_flow_repair_top10_s05_rotcond20` with that corrected
exemption still does not satisfy the new adaptation objective:

- first-layer gate remains passed: `48/48`, minimum final capital `4498.6w`,
  worst max drawdown `-19.20%`;
- recent survival remains `28/48`, with worst 2016-2025 annualized return
  `12.29%`, worst 2021-2025 cumulative return `31.51%`, worst rolling 5-year
  annualized return `2.69%`, worst rolling 3-year max drawdown `-16.83%`, and
  maximum consecutive quarters below defense `5`;
- structural capture remains `0/48`; non-exempt structural rows fall to `292`,
  and failure attribution becomes `rebalance_or_weighting_lag=137`,
  `scorecard_missed_mainline=40`, and `risk_control_low_exposure=28`;
- the worst structural case is phase `3`, lag `0`: `17` structural quarters,
  `8` non-exempt quarters, median structural exposure `78.1%`, capture-pass
  rate `12.5%`, CSI 300 win rate `87.5%`, and no low-exposure streak;
- the worst quarter is still the quarter starting `2020-03-02`: CSI 300
  `-2.42%`, ex-post top10 ETF basket `+14.82%`, portfolio `-3.29%`, exposure
  `100%`, selected ETF overlap only `512290.SH`, and structural-mainline top5
  overlap `0`.

This correction changes the diagnosis, not the pass/fail result.  The current
best path now clearly fails mainly because the quarterly selector/weighting path
does not transfer into the local mainline quickly enough in hard structural
quarters, while the recent-survival failure is still the rolling 5-year floor.

Three momentum-breadth repair candidates were tested after the corrected
validator.  The point-in-time `momentum_breadth_v2` recipe had the best offline
top3 structural-capture pass rate (`34.25%`), so it was promoted into the
direct-policy replay path as `structural_mombreadth_repair_top3`.  All three
variants kept the first-layer hard gate intact, but none changed the objective
result:

- `s05_rotcond20`: `48/48`, minimum final capital `4578.7w`, worst max
  drawdown `-19.08%`, recent `28/48`, structural `0/48`;
- `s10_rotcond35`: `48/48`, minimum final capital `4622.6w`, worst max
  drawdown `-19.37%`, recent `28/48`, structural `0/48`;
- `s20_rotcond50`: `48/48`, minimum final capital `4645.1w`, worst max
  drawdown `-19.86%`, recent `28/48`, structural `0/48`.

The worst 2020-03-02 quarter did not move in those candidates because the
existing structural repair filter rejected all 2020-02-29 structural top
candidates under the short-tail-loss and crowding rules.  Unfiltered
momentum-breadth also still favored technology/growth, not the ex-post
consumption/medicine resilience basket.

A narrow post-shock resilience repair branch was therefore tested with a
separate point-in-time score emphasizing lower 3-month tail loss, lower
6-month drawdown, positive participation, lower CSI 300 correlation, and ETF
liquidity/share flow.  It only raises the repair share when the ETF basket has
already drawn down moderately (`-12% < basket_drawdown_6m <= -5%`), the broad
index is not strong, ETF dispersion and breadth are positive, and systemic
crisis flags are absent.  This candidate also kept the first-layer hard gate:
`48/48`, minimum final capital `4480.5w`, worst max drawdown `-19.09%`.

The branch fixed the original 2020-03-02 worst quarter mechanically: portfolio
return improved from `-3.29%` to `+1.38%` by moving part of the direct sleeve
from `510030.SH` into `512170.SH`, `512290.SH`, `159938.SZ`, `510660.SH`, and
`159929.SZ`.  It still failed the new objective, with recent survival falling
to `24/48`, structural capture staying `0/48`, worst rolling 5-year annualized
return `2.53%`, and the worst structural case moving to the quarter starting
`2023-03-01`.  The resilience branch is therefore useful evidence that the
framework can recognize one shock-recovery mainline, but it is not robust
enough to satisfy the all-window structural-adaptation gate.

The next hybrid test made the resilience repair conditional instead of
constant: normal structural rotation used `momentum_breadth top3` with
`s20_rotcond50`, while only post-shock structural states used resilience repair
at `50%`.  This directly fixed the earlier 2020Q1 problem without forcing the
2023Q1 AI/online-consumption quarter into defensive red-chip or broad-index
ETFs.  A numeric filtering bug was also fixed in the repair selector:
`drawdown_6m=0.0` had been read as missing because the filter used
`row.get(...) or -1.0`; zero drawdown is now preserved as a valid point-in-time
feature value.

The corrected hybrid still does not pass:

- first-layer gate passes: `48/48`, minimum final capital `4647.2w`, worst max
  drawdown `-19.86%`;
- recent survival remains `28/48`, with worst 2016-2025 annualized return
  `11.83%`, worst 2021-2025 cumulative return `34.61%`, and worst rolling
  5-year annualized return `2.51%`;
- structural capture remains `0/48`; failure attribution becomes
  `rebalance_or_weighting_lag=123`, `scorecard_missed_mainline=54`, and
  `risk_control_low_exposure=32`;
- the worst structural case remains phase `3`, lag `0`, with capture-pass rate
  `12.5%`, benchmark win rate `100%`, and no low-exposure streak; the worst
  quarter moves again to `2013-12-02`.

A more aggressive attempt to loosen `momentum_breadth` tail-risk filters and
use top5 repair was rejected by first-layer validation: top3 loosened repair
fell to `28/48` with worst max drawdown `-20.09%`, and top5 loosened repair
fell to `44/48` with worst max drawdown `-20.02%`.  That path improves access
to high-volatility structural themes but violates the original drawdown gate,
so it should not be used as a compliant candidate.

The remaining worst structural quarter, `2013-12-02`, was then decomposed.  It
is not a pure selector miss: the point-in-time structural score already included
`159915.SZ` and `510500.SH` from the ex-post top10 basket, and the portfolio
outperformed CSI 300.  The failure comes from two interacting constraints:
CPPI cushion limited equity exposure to `46.1%`, while the unused sleeve was
cash because `bond_gold65_252d` had no eligible defensive ETF with a full
252-trading-day history.  The ex-post top10 basket also included newly listed
gold ETFs (`518800.SH`, `518880.SH`), so cash meaningfully reduced capture.

A named defensive stress policy was tested to determine whether early gold
eligibility could solve that specific failure without breaking the original
hard gate.  `bond_gold65_252d_earlygold63_min-120` keeps the main 252-day
defensive lookback but allows gold candidates with 63 trading days of history
and a trailing return no worse than `-12%`.  This does select `518800.SH` on
`2013-11-30`, but replay validation rejects it: only `44/48` drift samples pass
the first layer, with minimum final capital `4644.1w` and worst max drawdown
`-20.01%`.  The early-gold fallback is therefore not a compliant fix; buying
falling gold as a cash substitute improves one structural quarter but breaches
the original max-drawdown constraint.

An early-structural direct-repair trigger was then added for the 2013-style
case where 3-month ETF dispersion is still muted but 6-month ETF dispersion,
6-month basket excess return, and 6-month top-candidate strength are already
visible.  The hybrid policy
`structural_mombreadth_repair_top3_s20_rotcond50_shockres50_earlyres50` uses
normal momentum-breadth repair, switches to resilience repair in shock states,
and also switches to resilience repair when the early 6-month structural signal
is active.  The replay still passes the original hard gate (`48/48`, minimum
final capital `4577.8w`, worst max drawdown `-19.86%`) but fails the new
objective: recent survival remains `28/48`, structural capture remains `0/48`,
worst 2016-2025 annualized return is `11.73%`, worst 2021-2025 cumulative
return is `34.32%`, and worst rolling 5-year annualized return is `2.51%`.

The corrected hybrid moves the worst structural miss back to 2020Q1.  It
improves the quarter from a loss to `+1.38%`, but capture is still only `9.3%`
against the ex-post top10 ETF basket.  This confirms that repairing one
structural regime simply exposes another failure under the current exposure
and annual-scorecard path.

The structural oracle was rerun after the strong-risk-ban validator was
tightened to treat active hard exits as genuine risk-ban exemptions.  In this
oracle, every non-exempt structural quarter is replaced with the ex-post top10
ETF basket, while the audited exposure and inferred defensive/cash sleeve are
kept fixed.  Even this non-implementable upper bound reaches only `36/48`
structural cases.  Remaining failures are still low-exposure cases; the new
worst is `2022-12-06`, where exposure is `0%`, broad index return is only
`+3.55%`, and the ex-post top10 ETF basket returns `+15.67%`.  This is the
clearest current blocker: ETF selection alone cannot satisfy the structural
hard gate.  A compliant solution needs a point-in-time structural re-entry or
exposure rule that can override annual-scorecard zero exposure in local-mainline
quarters without breaking the original `MDD <20%` gate.

A replay-only structural re-entry floor was then tested to isolate that exposure
problem without rebuilding the full selector path.  The override activates only
when `wide_structural_opportunity_active` is true, the portfolio is not in bear
state, and there are no active risk flags; it then raises audited exposure to a
specified floor and replays the full daily capital path and drawdown.

All tested floors preserved the original first-layer hard gate:

- `10%` floor: `48/48`, minimum final capital `4610.0w`, worst max drawdown
  `-19.86%`;
- `15%` floor: `48/48`, minimum final capital `4603.5w`, worst max drawdown
  `-19.86%`;
- `30%` floor: `48/48`, minimum final capital `4583.0w`, worst max drawdown
  `-19.86%`;
- `50%` floor: `48/48`, minimum final capital `4514.4w`, worst max drawdown
  `-19.86%`.

The adaptation gate still fails for all of them.  The `30%` floor improves the
worst 2021-2025 cumulative return to `36.18%`, and the `50%` floor reduces
low-exposure structural failures from `28` to `20`, but structural capture
remains `0/48` and worst rolling 5-year annualized return remains `2.51%`.
The worst structural case remains phase `3`, lag `0`, quarter starting
`2020-03-02`: exposure is already `100%`, the portfolio return is `+1.38%`,
but capture is only `9.3%` of the ex-post top10 basket.  This proves that a
structural re-entry floor is necessary but not sufficient; the remaining hard
blocker is still regime-specific mainline transfer/weighting in 2020Q1-style
quarters.

The post-shock resilience sleeve was then raised from `50%` to `100%` while
keeping the early-resilience switch at `50%` and the replay-only structural
re-entry floor at `50%`.  This remains compliant with the original hard gate:
`48/48` drift samples pass, minimum final capital is `4587.9w`, and worst max
drawdown is `-19.86%`.  It also fixes the previous 2020-03 structural miss
enough that it is no longer the worst quarter.  The new adaptation result is
still rejected: recent survival is `28/48`, structural capture is `0/48`,
worst 2016-2025 annualized return is `11.80%`, worst 2021-2025 cumulative
return is `38.05%`, and worst rolling 5-year annualized return remains
`2.51%`.  Failure attribution is `risk_control_low_exposure=20`,
`rebalance_or_weighting_lag=123`, and `scorecard_missed_mainline=54`.

The worst structural miss after `shockres100` is phase `1`, lag `5`, from
`2018-01-09` to `2018-04-11`.  Exposure is already high at `81.25%`, but the
portfolio loses `5.20%` while the ex-post top10 ETF basket gains `3.14%`,
giving a capture ratio of `-165.5%`.  This is classified as
`rebalance_or_weighting_lag`: the point-in-time structural recipes already
ranked several eventual top10 ETFs in the top15, but the direct sleeve remained
anchored in the V9 base ETF and the old broad/large-cap allocations.  A
score-gap repair experiment that lifted the structural sleeve to `50%` when
the point-in-time structural score was far above the V9 base score preserved
the original hard gate (`48/48`, minimum final capital `4358.4w`, worst max
drawdown `-19.86%`) but barely changed this miss (`-5.07%` portfolio return),
left all structural failure counts unchanged, and reduced the worst
2021-2025 cumulative return to `35.38%`.  The score-gap rule was therefore
removed from registered strategy code.

Selector-side diagnostics show the deeper blocker.  On all non-exempt
structural quarters, an idealized point-in-time selector that simply buys the
top5 ETF basket from the best tested structural recipe still only reaches a
`32.39%` capture-pass rate.  The strongest tested recipe is
`liquidity_flow_v2` (`hit=57.75%`, `median_capture=4.76%`,
`benchmark_win_rate=61.97%`); top10 variants increase overlap but lower the
capture-pass rate.  A single-factor scan across the current point-in-time
feature set does not improve this ceiling: the best single feature,
low `index_fundamental_earnings_yield`, also reaches only `32.39%` capture
pass and has a negative median capture ratio.  ETF selection quality alone is
therefore not close to the current hard requirement that every applicable
structural quarter achieve at least `30%` of the ex-post top10 basket return.

Exposure-side diagnostics also reject simple re-entry fixes.  Candidate
low-exposure triggers based on broad recovery, selected ETF strength, and
rotation opportunity all have negative aggregate incremental return when used
to raise exposure to `50%`.  The least bad trigger,
`selected_strength_no_hard_exit`, fires on `321` low-exposure rows with a
`59.8%` positive incremental rate but still has `-1.8735` aggregate
incremental return and a worst single-row incremental hit of `-15.69%`.
This explains why broad exposure floors preserve the old hard gate but do not
solve the new structural gate.

Current status: no tested candidate satisfies the new hard constraints.  The
surviving compliant baseline is still the `shockres100 + earlyres50 +
structreentry50` replay candidate for the original gate, but it fails the
recent/structural adaptation objective.  The next viable research direction is
not another unconditional exposure floor or larger structural sleeve; it needs
new point-in-time evidence that separates predictable local-mainline quarters
from the many ex-post structural quarters that current ETF features do not
forecast at quarterly frequency.

A theme-breadth diagnostic was added to test that next direction.  It maps ETF
and tracked-index names into static domestic theme buckets, then adds a small
same-snapshot group-breadth component to the liquidity-flow score.  This uses
only point-in-time ETF features plus static metadata; it does not cluster on
future returns.  The best diagnostic setting, `liquidity_flow + 10% theme
breadth`, improves the idealized top5 structural selector capture-pass rate
from `32.39%` to `39.44%` (`hit=59.15%`, `win=60.56%`).  This is a genuine
selector signal improvement, but still far below the hard target that every
applicable structural quarter capture at least `30%` of the ex-post top10
basket.

The diagnostic recipe was then wired into a replayable direct ETF repair policy
to test whether the signal survives the real exposure, defense, transaction
cost, and drawdown path:
`structural_groupbreadth_repair_top5_s20_rotcond50_shockres100_earlyres50`.
It preserves the original hard gate (`48/48`, minimum final capital `4482.3w`,
worst max drawdown `-19.88%`) but still fails the adaptation objective:
recent survival is `28/48`, structural capture is `0/48`, worst 2016-2025
annualized return is `12.18%`, worst 2021-2025 cumulative return falls to
`32.60%`, and worst rolling 5-year annualized return is `2.45%`.  Failure
attribution becomes `risk_control_low_exposure=20`,
`rebalance_or_weighting_lag=135`, and `scorecard_missed_mainline=42`.  The
theme-breadth signal should remain available for future selector searches, but
this replay is not a new best candidate because it worsens recent survival
while leaving the structural hard gate at `0/48`.

The next selector upgrade targeted the repeated 2021-01 structural failure.
That quarter's ex-post top10 was a clear reflation/value basket: bank ETFs,
steel, energy chemicals, nonferrous metals, and coal.  The existing structural
recipes were still chasing prior high-momentum military, new-energy vehicle,
and consumption ETFs, many of which then sold off.  A point-in-time
`reflation_rotation_v1` recipe was added using only same-snapshot features:
low 1-month momentum rank, still-positive 3/6-month momentum, lower market
beta/correlation, ETF share growth, lower crowding, and shallower drawdown.
In idealized structural-quarter diagnostics, top3
`reflation_rotation_v1` is the strongest tested selector so far:
`hit=58.80%`, `capture_pass=49.30%`, `median_capture=24.06%`, and
`benchmark_win_rate=81.69%`.

The recipe was then wired into replayable direct ETF repair policies.  All
tested reflation sleeves preserved the original hard gate:

- `s10_rotcond35_shockres100_earlyres50`: `48/48`, minimum final capital
  `4785.3w`, worst max drawdown `-18.79%`;
- `s15_rotcond50_shockres100_earlyres50`: `48/48`, minimum final capital
  `4908.5w`, worst max drawdown `-18.83%`;
- `s20_rotcond50_shockres100_earlyres50`: `48/48`, minimum final capital
  `4858.1w`, worst max drawdown `-18.87%`.

The new objective still fails.  The most balanced reflation candidate is
`s10`: recent survival remains `28/48`, structural capture remains `0/48`,
worst 2016-2025 annualized return improves to `13.07%`, worst 2021-2025
cumulative return is `33.38%`, and worst rolling 5-year annualized return is
`2.59%`.  Structural failure attribution improves from
`rebalance_or_weighting_lag=123` / `scorecard_missed_mainline=54` to
`rebalance_or_weighting_lag=111` / `scorecard_missed_mainline=50`, while
`risk_control_low_exposure` remains `20`.  The blocker moves back to the
2018-01 structural quarter: reflation selects `510010.SH` and `159910.SZ`
instead of the ex-post healthcare/defensive basket, so capture is still
negative.  The reflation recipe is therefore a real scorecard improvement and
a better original-gate candidate, but it does not satisfy the added structural
hard gate.

The new worst miss was then decomposed.  The 2018-01 structural quarter was
not another reflation/value rotation: the ex-post top10 was mainly healthcare
ETFs plus a small TMT/technology and bond tail.  At the 2017-12-31 signal date,
healthcare ETFs had positive 1/3/6-month momentum, shallow drawdowns, medium
to low market correlation, and in some cases strong ETF share growth.  The
`resilience_v1` recipe already recognizes part of this pattern: in the
standard top3 structural selector diagnostic it is the second-best recipe
after reflation, with `hit=37.68%`, `capture_pass=40.14%`,
`median_capture=16.18%`, and `benchmark_win_rate=80.28%`.  It also ranks
`510660.SH` and `512120.SH` at the top for the 2018-01 signal.

Several defensive-healthcare and group-leader prototypes were tested as
diagnostics before any replay.  They intentionally used only same-snapshot
features such as shallow drawdown, low beta/correlation, positive 1/3-month
momentum, ETF share growth, lower crowding, and static theme labels.  They
did not beat the reflation selector on the full structural-quarter set:
`defensive_health top3` reached only `capture_pass=38.03%`, and the best
theme group-leader variant reached only `capture_pass=35.21%`.  These recipes
repair parts of 2018 but fail badly in the 2021 reflation/value quarter, so
they were not promoted into replayable strategy candidates.  The remaining
research problem is a conditional switch that can select healthcare/defensive
growth in 2018-like states and reflation/value in 2021-like states without
using ex-post structure labels.

A first conditional switch was implemented and replayed.  It activates the
resilience recipe only when the static healthcare theme bucket is the leading
same-snapshot group by 3-month momentum, positive breadth, and ETF share flow;
otherwise it uses the reflation recipe.  The filter was also aligned with the
diagnostic intent: when the healthcare condition is active, the repair sleeve
uses the looser resilience tail-risk filter instead of the stricter
non-resilience crowding cutoff.  In the idealized selector diagnostic,
`conditional_rotation_v1 top3` keeps the same `49.30%` capture-pass rate as
reflation but improves hit rate to `64.44%` and benchmark win rate to `87.32%`.

Replay results:

- `conditional_s10_resfilter`: `48/48`, minimum final capital `4799.8w`,
  worst max drawdown `-18.55%`, recent survival `28/48`, structural capture
  `0/48`, worst 2021-2025 cumulative return `33.38%`;
- `conditional_s15_resfilter`: `48/48`, minimum final capital `4929.5w`,
  worst max drawdown `-18.64%`, recent survival `28/48`, structural capture
  `0/48`, worst 2021-2025 cumulative return `31.97%`.

The conditional switch is the strongest original-gate candidate so far, and it
does reduce `scorecard_missed_mainline` failures to `34`.  It still does not
meet the new objective.  The 2018-01 worst structural quarter improves only
incrementally because the repair sleeve is still small relative to the base V9
ETF and broad allocation: `conditional_s10` selects `510660.SH` and
`512120.SH`, but capture remains `-162.2%`; `conditional_s15` improves that to
`-157.7%` while worsening recent survival.  Larger unconditional repair size
is therefore unlikely to be the missing piece; the remaining blocker is still
how to transfer enough weight into the local mainline only when the point-in-
time evidence is strong enough, without hurting 2021-2025 survival.

The next pass made that weight transfer conditional rather than unconditional:
`_hcresNN` raises the repair share only when the healthcare leadership trigger
is active.  This directly tests whether the 2018 healthcare mainline can get a
larger sleeve without perturbing 2021 reflation/value states.  The results
were monotonic and did not damage recent survival:

- `hcres30`: `48/48`, minimum final capital `4799.8w`, worst max drawdown
  `-18.55%`, recent survival `28/48`, structural capture `0/48`, worst
  2021-2025 cumulative return `33.38%`;
- `hcres50`: `48/48`, minimum final capital `4863.6w`, worst max drawdown
  `-18.55%`, recent survival `28/48`, structural capture `0/48`, worst
  2021-2025 cumulative return `33.38%`;
- `hcres100`: `48/48`, minimum final capital `5076.3w`, worst max drawdown
  `-18.55%`, recent survival `28/48`, structural capture `0/48`, worst
  2021-2025 cumulative return `33.38%`.

`hcres100` is now the strongest original-gate candidate among the tested
strict passive ETF-only variants.  It improves the 2018-01 worst-quarter
capture enough that the global worst structural quarter moves to 2023-04-07,
where the portfolio loses `5.36%` against an ex-post top10 basket gain of
`6.57%` (`capture=-81.6%`) despite full exposure.  The remaining blocker has
therefore shifted from healthcare/reflation switching to 2023-style
technology/AI structural capture: the strategy has exposure and some related
technology ETFs, but still fails to transfer enough weight into the actual
top10 local mainline.

The 2023-04 miss was then decomposed using only 2023-03-31 point-in-time
features.  The repair share had already been raised by `rotcond35` to `35%`;
the failure was selector composition, not an exposure problem.  The direct
repair sleeve selected `510880.SH`, `512660.SH`, `512330.SH`, and
`159933.SZ`, while the broader index-to-ETF sleeve still carried large online
consumption, AI, software, computer, and fintech weights.  The ex-post top10
was different: communication equipment (`515880.SH`) plus electric-power /
green-power ETFs and energy.  In other words, the strategy chased the prior
AI/software leadership after it had become crowded, while the quarter's local
mainline rotated into communication and power.

To keep this diagnosis reproducible, a static subtheme mapper and diagnostic
`cooling_rotation_v1` selector were added.  The selector uses only same-snapshot
features: 1/3-month momentum, relative strength, positive-day breadth, ETF
share growth, amount acceleration, crowding, drawdown/CVaR/max daily loss,
liquidity, and static ETF/index-name subtheme tags.  It correctly lifts
communication and power for the 2023-04 signal (`515880.SH`, communication
ETFs, `159611.SZ`, `159625.SZ`), but it is not a production candidate.  On the
`hcres100` structural-quarter diagnostic set:

- top3 `cooling_rotation_v1`: `hit=54.93%`, `capture_pass=46.48%`,
  `median_capture=8.59%`, `benchmark_win_rate=73.24%`;
- top5 `cooling_rotation_v1`: `hit=56.34%`, `capture_pass=40.85%`,
  `median_capture=22.08%`, `benchmark_win_rate=70.42%`.

This is weaker than `conditional_rotation_v1 top3` on the hard capture metric
(`capture_pass=49.30%`) and fails other regimes, especially 2017-11,
2019-04, and 2025-03.  The current conclusion is therefore negative: a simple
"cooling after hot AI/software" rule is useful as a failure attribution tag,
but replacing the production structural repair with it would overfit one
failure mode and reduce robustness.  The next viable search needs a higher
level conditional switch that can distinguish at least four states point-in-
time: healthcare resilience, reflation/resources, hot-theme cooling into
communication/power, and defensive/gold-led structural quarters.

That higher-level switch was implemented as `multistate_rotation_v1`.  The
point-in-time order is deliberately simple: healthcare leadership uses
resilience; resources/finance/industrial breadth uses reflation; crowded
digital/AI leadership with communication, utility, or semiconductor breadth
uses cooling rotation; otherwise it falls back to the prior conditional
rotation.  In the idealized top3 structural selector diagnostic this is the
best tested recipe so far:

- `multistate_rotation_v1`: `hit=68.66%`, `capture_pass=53.52%`,
  `median_capture=35.21%`, `benchmark_win_rate=81.69%`;
- prior `conditional_rotation_v1`: `hit=64.44%`, `capture_pass=49.30%`,
  `median_capture=24.13%`, `benchmark_win_rate=87.32%`.

The improvement did not translate into passing the full portfolio objective.
Three replayable multistate repair sleeves were tested, all with `hcres100`,
`shockres100`, and `earlyres50`:

- `multistate_s10_rotcond35`: `48/48`, minimum final capital `5104.3w`,
  worst max drawdown `-18.55%`, recent survival `28/48`, structural capture
  `0/48`, worst 2021-2025 cumulative return `33.38%`;
- `multistate_s15_rotcond50`: `48/48`, minimum final capital `5114.7w`,
  worst max drawdown `-18.64%`, recent survival `28/48`, structural capture
  `0/48`, worst 2021-2025 cumulative return `31.97%`;
- `multistate_s20_rotcond50`: `48/48`, minimum final capital `5089.8w`,
  worst max drawdown `-18.70%`, recent survival `28/48`, structural capture
  `0/48`, worst 2021-2025 cumulative return `31.91%`.

The `s10` version improves the 2023-04 full-portfolio return from `-5.36%` to
`-4.45%`, but not enough to pass the structural capture gate because the repair
sleeve is first limited to the direct ETF layer and then diluted by the
`49%` direct blend.  Raising the multistate repair share to `s15/s20` reduces
some structural lag counts (`rebalance_or_weighting_lag=123` instead of
`127`) but worsens the recent survival window.  Current best candidate remains
the conservative `multistate_s10` on original-gate capital, while the
adaptation objective is still unmet.  The next blocker is no longer just
"which top3 repair ETF" but how to let a verified structural mainline affect
more of the portfolio without breaking the 2021-2025 survival floor.

The next test therefore changed portfolio-layer influence rather than ETF
ranking.  A point-in-time `_structblendNN` override was added: when the same
market-state fields already used by `rotation_structural_opportunity_active`
detect a wide/broad structural opportunity, the direct ETF layer can rise
above the usual `49%` blend share.  This tests whether the good multistate
selector can matter at portfolio level without changing the underlying CSI
selector or using future labels.  Three multistate `s10` variants were replayed:

- `structblend70`: `48/48`, minimum final capital `5060.1w`, worst max
  drawdown `-18.55%`, recent survival `28/48`, structural capture `0/48`,
  worst 2021-2025 cumulative return `39.69%`, worst rolling 5-year
  annualized return `2.66%`;
- `structblend85`: `48/48`, minimum final capital `4943.2w`, worst max
  drawdown `-18.55%`, recent survival `36/48`, structural capture `0/48`,
  worst 2021-2025 cumulative return `44.33%`, worst rolling 5-year
  annualized return `2.71%`;
- `structblend100`: `48/48`, minimum final capital `4822.6w`, worst max
  drawdown `-18.55%`, recent survival `32/48`, structural capture `0/48`,
  worst 2021-2025 cumulative return `49.08%`, worst rolling 5-year
  annualized return `2.75%`.

This is real progress on the recent-survival layer: `structblend85` is the
best tested recent-survival candidate so far, raising the 2021-2025 worst
cumulative return from `33.38%` to `44.33%` and the pass count from `28/48` to
`36/48` while preserving the original hard gate.  It still fails the
adaptation objective because the worst rolling 5-year window is much earlier
(`2009-08-10` to `2014-05-12`, average exposure about `0.41`) and remains far
below the `5%-8%` annualized floor.

Low-exposure re-entry was then checked directly on `structblend85`.  Raising
low-exposure quarters to a `50%` or `60%` floor under point-in-time recovery,
selected-strength, or rotation triggers had negative aggregate incremental
returns.  For a `50%` floor the least bad trigger,
`selected_strength_no_hard_exit`, still had `sum_inc=-1.3996`; for a `60%`
floor it was `sum_inc=-2.4081`.  Therefore the old 2009-2014 rolling-window
failure should not be repaired by a broad re-entry floor.  The next search
should focus on why the equity sleeve itself under-earns in that window, not
on raising exposure mechanically.

The next structural selector diagnostic added a late-cycle growth-exhaustion
state.  It uses only same-snapshot ETF features and static subtheme tags.  The
trigger is deliberately narrow: digital/semiconductor/communication leadership
must show mature 3-month strength with high crowding, a 6-month blowoff with
large ETF share growth and high crowding, or a 6-month trend that has rolled
over into negative 1-month momentum and a deep 3-month drawdown.  When active,
the repair score rotates away from exhausted growth into resources, consumer,
finance, healthcare, and utilities using low crowding, lower broad-market
correlation, drawdown resilience, 6-month trend, breadth, flows, and
fundamental repair fields.

In the idealized top3 structural selector diagnostic on the current
`pathriskblock_margin` report, this improved the pure selector metrics:

- `late_cycle_defensive_rotation_v1`: `hit=74.30%`,
  `capture_pass=59.86%`, `median_capture=41.00%`,
  `benchmark_win_rate=90.14%`;
- prior `multistate_rotation_v1`: `hit=68.66%`,
  `capture_pass=53.52%`, `median_capture=35.21%`,
  `benchmark_win_rate=81.69%`.

The improvement came from point-in-time states such as `2019-03-31`,
`2019-12-31`, `2020-02-29`, `2023-03-31`, and `2025-01-31`.  Earlier broad
conditions that also triggered on `2019-02-28`, `2019-10-31`, and
`2025-02-28` were rejected because they reduced capture; the final trigger
excludes those false positives.

Full-portfolio tests show that this is still not enough to satisfy the new
hard adaptation gate:

- `latecycle_s10_rotcond35_structblend85_exhaustfallback`: original hard gate
  `48/48`, minimum final capital `5119.6w`, worst max drawdown `-18.55%`,
  recent survival `32/48`, structural capture `0/48`, worst rolling 5-year
  annualized return `4.49%`;
- `latecycle_s10_rotcond35_structblend90_exhaustfallback`: original hard gate
  `48/48`, minimum final capital `5057.3w`, worst max drawdown `-18.55%`,
  recent survival `32/48`, structural capture `0/48`, worst rolling 5-year
  annualized return `4.46%`;
- `latecycle_s10_rotcond35_structblend100_exhaustfallback`: rejected by the
  original hard gate, `44/48`, minimum final capital `4933.0w`, worst max
  drawdown `-20.00%`;
- `latecycle_s20_rotcond50_structblend85_exhaustfallback`: original hard gate
  `48/48`, minimum final capital `5172.9w`, worst max drawdown `-18.35%`,
  recent survival `36/48`, structural capture `0/48`, worst rolling 5-year
  annualized return `4.36%`.

The `s20/structblend85` variant is the best late-cycle candidate on the
original hard gate and recovers recent survival to `36/48`, but it still fails
the new adaptation objective.  The worst structural case moves to
`2020-01-02` through `2020-04-01`: broad return `-11.49%`, top10 equal basket
`+7.11%`, portfolio `-5.46%`, full exposure, and capture `-76.81%`.  The
portfolio held some related ETFs (`512290.SH`, `512120.SH`) but still carried
large weights in non-top10创业/宽基 and stale themes, so the failure remains
`rebalance_or_weighting_lag` rather than `risk_control_low_exposure`.

Current conclusion: the late-cycle selector is a valid incremental repair and
should remain as a candidate, but the hard adaptation target is still unmet.
Pushing structural influence to `structblend100` breaks the original drawdown
gate, while `structblend90` does not improve the worst structural cases.  The
next search should target the portfolio weighting layer for mixed structural
quarters such as 2020-01, where semiconductor/resources/healthcare are all in
the ex-post top10 and a single-family switch still leaves too much stale broad
growth exposure.

The next iteration followed a sample-first loop: list unmet samples, diagnose
the failure reason, inspect whether point-in-time features were sufficient,
then test only the new signal implied by that diagnosis.

For `latecycle_s20_structblend85_purestructcond`, the original hard gate
passed (`48/48`, minimum final capital `5252.5w`, worst max drawdown
`-18.35%`) but adaptation still failed (`recent=36/48`, `structural=0/48`,
worst rolling 5-year annualized return `4.43%`).  The unmet recent cases were
all rolling-5-year failures.  The structural failures were still mostly
`rebalance_or_weighting_lag` (`116` rows), plus `risk_control_low_exposure`
(`20`) and `scorecard_missed_mainline` (`24`).

The worst structural samples showed that the feature set was often sufficient
but underweighted:

- `2023-02-28` signal: ex-post top10 was AI/TMT/communication
  (`159725.SZ`, 科创/AI ETFs, `515880.SH`, `515980.SH`, `512930.SH`).  These
  ETFs had high point-in-time `index_policy_score` around `8`, positive 1/3
  month trend, and moderate crowding, while the selector preferred
  finance/resources with policy score `0`.
- `2021-04-30` signal: ex-post top10 was new-energy ETFs.  The available
  features already showed `index_policy_score=10`, strong 6-month momentum,
  positive 1-month repair, and strong fundamental earnings growth, but the
  scoring over-penalized 3-month drawdown and selected resources/finance.
- `2019-10-31` signal: the existing late-cycle selector already had partial
  overlap (`512480.SH`, `512330.SH`, `512220.SH`), so the remaining failure was
  weighting lag rather than complete signal absence.

A new point-in-time `policycat` candidate was therefore added as an explicit
policy/fundamental catalyst state.  It uses only existing same-snapshot fields:
`index_policy_score`, `index_fundamental_earnings_growth_6m`,
`index_constituent_roe_change_12m`, 1/6-month momentum, 6-month relative
strength, breadth, ETF share growth, correlation, crowding, drawdown, and
CVaR.  The lightweight diagnostic confirmed the intended behavior:
`2023-02-28` selected `515880.SH`, `515980.SH`, and `159725.SZ` in the top10;
`2021-04-30` selected `515700.SH`, `515030.SH`, and `515790.SH`; `2010-02-28`
did not trigger.

Full-portfolio validation rejected the policycat variants:

- `policycat_s20_structblend85`: original hard gate still passed (`48/48`),
  but minimum final capital fell to `4982.9w`, worst max drawdown worsened to
  `-19.23%`, recent survival stayed `36/48`, structural capture stayed
  `0/48`, and worst rolling 5-year annualized return stayed `4.36%`.
- `policycat_s20_structblend85_purestructcond`: rejected by the original hard
  gate (`32/48`, worst max drawdown `-25.29%`).  Failed samples concentrated
  around the `2020-02-06` and `2020-02-10` decisions, where the policy catalyst
  concentrated the portfolio in communication/TMT (`515050.SH`, `515880.SH`,
  `512930.SH`) immediately before the 2020-03 COVID crash.  The point-in-time
  risk state did not flag a strong crisis, so the signal is too aggressive to
  use as a pure structural override.

Current diagnosis: policy/industry/fundamental catalyst features are useful
for attribution and ranking, but the tested implementation is not robust
enough for the hard gate.  The next iteration should not simply boost policy
theme weights.  It should either add a shock-vulnerability filter for high
policy-growth baskets, or use policycat only as a small tie-breaker inside the
repair sleeve while preserving the safer late-cycle selector as the default.

The next iteration tested whether the structural misses were partly caused by
ETF candidate coverage rather than scoring alone.  For the worst structural
signals, many ex-post mainline ETFs were investable and had fresh point-in-time
ETF prices but were absent from the `SHARE_V5` candidate observations:

- `2023-02-28`: AI/TMT ETFs such as `512930.SH`, `515070.SH`,
  `515980.SH`, and `159725.SZ` had fresh prices and positive short momentum,
  but several were missing SHARE_V5 rows because of feature/history coverage.
- `2021-04-30`: new-energy ETFs such as `159824.SZ`, `159806.SZ`,
  `516660.SH`, `159857.SZ`, `516180.SH`, and `516160.SH` were investable and
  fresh, but many lacked enough history for the full candidate feature row.
- `2019-10-31`: early AI/technology ETFs such as `512930.SH`, `515860.SH`,
  and `515000.SH` were also fresh but missing from SHARE_V5.

A new `_coldstart` direct-policy suffix was added for structural repair
variants.  It supplements missing SHARE_V5 candidates with a price-only,
point-in-time score using 1/3/6-month ETF returns, drawdown, volatility, and
static ETF theme/subtheme tags.  It excludes domestic defensive ETFs and only
applies when the strategy name explicitly contains `_coldstart`.

The first full cold-start portfolio was rejected by the original hard gate:
`pass=44/48`, minimum final capital `4692.5w`, worst max drawdown `-21.91%`.
All four failed samples were execution-lag-5 cases with the same worst drawdown
date, `2021-03-08`.  The failing `2020-12-08` decision held about `85%` of the
risk sleeve in `512560.SH`, `512810.SH`, and `159806.SZ`, whereas the base
pure-structural conditional variant held lower-volatility broad/consumer/
resource ETFs and stayed under the drawdown gate.  Point-in-time features at
`2020-12-08` already showed a vertical extension: the failing ETFs had
6-month returns of roughly `42%` to `67%`, shallow drawdowns, and 3-month
volatility around `27%` to `33%`.

The cold-start signal was therefore filtered for high vertical extension:
6-month return above `40%`, 6-month drawdown shallower than `-6%`, and
3-month volatility above `25%`.  This removed the `2020-12-08` high-risk
military/new-energy cold-start picks while preserving the 2023 AI/TMT
candidates and leaving 2021 new-energy candidates available where not
vertically extended.  The path-cache name was also shortened with a hash and
the cache version bumped so that policy changes cannot reuse stale paths.

The filtered cold-start portfolio recovered the original hard gate:
`pass=48/48`, minimum final capital `4917.3w`, worst max drawdown `-19.76%`.
However, the new adaptation objective still failed: recent survival `36/48`,
structural capture `0/48`, worst 10-year annualized return `13.09%`, worst
5-year cumulative return `45.97%`, and worst rolling 5-year annualized return
`4.43%`.  The 12 recent failures were all rolling-5-year failures in windows
starting around `2009-06` to `2009-08` and ending around `2014-03` to
`2014-05`.  Structural failure reasons were still dominated by
`rebalance_or_weighting_lag` (`107` rows), followed by
`risk_control_low_exposure` (`24`) and `scorecard_missed_mainline` (`21`).

Current conclusion: ETF cold-start coverage is a real feature gap and the
price-only supplement is point-in-time valid, but by itself it does not solve
the adaptation objective.  The next iteration should prioritize quarterly
rebalance/weighting lag and the 2009-2014 rolling-window slow period, not
another unrestricted theme boost.

The following iteration split the remaining failures into the recent survival
window and the structural capture window.

For the filtered cold-start portfolio, the 12 recent-survival failures were all
rolling-5-year failures.  The common worst window was around `2009-06` to
`2014-03`, with cumulative return `24.2%`, annualized return `4.43%`, max
drawdown `-11.69%`, average exposure `43.1%`, and median exposure `50.0%`.
The risk sleeve itself compounded `10.3%` and the defensive sleeve compounded
`8.7%`; the failure was not a drawdown problem but persistent exposure
compression.  The main binding stages in the worst window were
`direction_risk_gate_rejection_cap` (`8` quarters), `risk_flag_cap` (`7`
quarters), and `hard_exit` (`2` quarters).  Low-exposure false negatives
included `2009-06-08` and `2013-06-13`, where the risk sleeve later returned
`+5.88%` and `+8.52%` respectively.

A diagnostic broad-participation re-entry signal was tested.  It is
point-in-time and requires ETF basket breadth to be broad, the best 3-month ETF
return to be strong, 6-month basket drawdown to be shallow, 3-month basket
volatility to stay bounded, and distribution/leverage risk flags to be absent.
Replay-only tests suggested floors around `45%` to `60%` could improve the
recent rolling window without breaking the original hard gate, but replay was
not accepted as final evidence because it only recomputes the ETF/direct-policy
and capital path, not the complete production path.

The signal was therefore added to the production rule family as
`_partreentry45`, `_partreentry50`, and `_partreentry55` and tested through the
main strict quarterly backtest:

- `partreentry50`: original hard gate passed (`48/48`, minimum final capital
  `4759.6w`, worst max drawdown `-19.76%`) but adaptation still failed
  (`recent=36/48`, `structural=0/48`, worst rolling 5-year annualized return
  `4.54%`).  It successfully lifted `2009-06-08` exposure from `16%` to `50%`,
  but it did not lift `2013-06-13` because that quarter was in bear-state by
  the 60-day moving-average/20-day return gate.
- `partreentry55`: original hard gate still passed (`48/48`, minimum final
  capital `4622.5w`, worst max drawdown `-19.76%`) but adaptation worsened
  (`recent=32/48`, `structural=0/48`, worst rolling 5-year annualized return
  `4.22%`).  Higher floors are therefore not robust.

For structural capture, the best diagnostic recipe remained the existing
late-cycle defensive rotation: top3 hit rate `74.30%`, capture pass rate
`59.86%`, median capture `41.00%`, and benchmark win rate `90.14%`.  The
current mainline recipe was far weaker (`29.58%` capture pass and negative
median capture).  The reason the full adaptation gate still reports
`structural=0/48` is that every drift sample still contains at least one
structural quarter below the required capture threshold.

The worst remaining structural sample family is around the `2012-12-31` signal
for the `2013-01` to `2013-04` holding period.  The ex-post top10 was fully
present in SHARE_V5 (`159909.SZ`, `159915.SZ`, `159907.SZ`, `159902.SZ`,
`510150.SH`, `159906.SZ`, `159901.SZ`, `510130.SH`, `159905.SZ`,
`159910.SZ`), so this is not a coverage problem.  Point-in-time features
already showed strong 1-month and 3-month rebound in those ETFs, but the
available recipes preferred the most overextended finance/SSE names
(`510230.SH`, `510030.SH`, `510050.SH`) or defensive/value names
(`510060.SH`, `510880.SH`, `510160.SH`), which then underperformed.  A
diagnostic lagged-rebound recipe that favored moderate rebound rather than
vertical extension was rejected: top3 hit rate `16.90%`, capture pass rate
`15.49%`, median capture `-27.33%`, and benchmark win rate `38.03%`.

Current conclusion: the broad-participation re-entry signal is a valid but
small production candidate (`partreentry50`), not a solution.  The structural
selector already uses the best tested recipe, but the remaining bad quarters
require a more precise distinction between overextended rebound leaders and
lagged rebound continuation, which the first diagnostic attempt failed to
generalize.

The next diagnostic pass tested whether the remaining structural gap could be
closed by a different selector family.

First, feature stratification compared ETFs selected by the late-cycle recipe
against ex-post top10 ETFs across all structural rows.  In bad rows where
late-cycle failed, the selected ETFs skewed toward finance/resources/industrial
with higher 3-month and 6-month momentum, shallower drawdowns, and lower tail
risk.  The missed top10 ETFs had higher 1-month rebound, lower 6-month
momentum, and more broad beta exposure.  This confirmed the `2012-12-31`
pattern: the selector was not missing data, but it mistook a mature rebound for
safer leadership.

Three candidate fixes were tested and rejected:

- A narrower `post_bear_beta` recipe, restricted to positive 1-month/3-month
  rebound, moderate 6-month momentum, shallow drawdown, non-resource groups,
  bounded crowding, and sufficient market correlation.  It was too sparse and
  weak: top3 hit rate `11.27%`, capture pass rate `9.30%`, median capture
  `-2.95%`, and benchmark win rate `33.80%`.
- A conditional `post_bear_beta` fallback to late-cycle.  It still underfit the
  structural set: hit rate `49.30%`, capture pass rate `37.32%`, median capture
  `5.74%`, and benchmark win rate `76.06%`, all below late-cycle.
- A point-in-time walk-forward structural selector using only labels whose
  `end_snapshot <= signal_date`.  Tested feature sets included momentum,
  relative strength, drawdown, crowding, correlation, ETF share flow, policy,
  and fundamental repair fields.  The representative configurations all
  underperformed late-cycle: hit rate `36.60%`, capture pass rate `19.70%`,
  median capture `-5.60%`, and benchmark win rate `66.20%`.

Finally, a top-N concentration check showed that late-cycle top2 and top3 have
the same diagnostic capture pass rate (`59.9%`), with top2 having a higher
median capture but worse robustness.  A replay-only top2 direct-policy test was
rejected by the original hard gate: `44/48` pass, minimum final capital
`4608.1w`, and worst max drawdown `-23.80%`.  The candidate was not promoted
to production.

Current conclusion: the tested feature set does contain useful structural
information, but the best robust production selector remains late-cycle top3.
The remaining structural failures appear to require either a new point-in-time
state variable that separates mature rebound exhaustion from lagged beta
continuation, or a different portfolio-level capture rule, not simple topN
concentration, static rebound scoring, or generic walk-forward regression.

The next iteration focused on the `2023-03-31` signal / `2023-04` to
`2023-07` holding-period failure.  The failure was not low exposure: the
portfolio had `100%` equity exposure but still returned `-2.54%` versus a
top10 structural basket return of `+8.56%`.  The selected basket had too much
digital-hot and semiconductor exposure after a vertical extension.  Two
cold-start ETFs were especially harmful: `588260.SH` and `588290.SH`, both
short-history 科创/芯片 candidates that ranked highly on price-only momentum
but then returned about `-9%` to `-10%` during the holding period.

A point-in-time cold-start hotguard was added to
`structural_price_cold_start_scores`.  It only blocks `digital_hot` and
`semiconductor` cold-start candidates when 1-month/3-month momentum is already
high, drawdown is very shallow, and 3-month volatility is elevated, including
short-history cases where the 6-month feature is not yet available.  It does
not block communication, utilities, dividend, or other defensive/local
mainline candidates.

Diagnostic results across structural quarters improved: the production-style
direct selector capture pass rate rose from `52.82%` to `58.45%`, median
capture from `36.43%` to `40.57%`, and the `2023-04-03` target quarter moved
from `-17.62%` capture to `45.12%` capture in direct-selector diagnostics.
The full strict quarterly production run with the hotguard preserved the
original hard gate: `48/48` pass, minimum final capital `4917.3w`, and worst
max drawdown `-19.76%`.

The adaptation gate still failed after the hotguard:

- recent survival stayed at `36/48`, with worst rolling 5-year annualized
  return still `4.43%` for the `2009-06` to `2014-03` window;
- structural capture stayed at `0/48`;
- structural failure reasons remained `rebalance_or_weighting_lag=107`,
  `risk_control_low_exposure=24`, and `scorecard_missed_mainline=21`.

The full adaptation validator now shows the worst structural case shifted back
to the `2012-12-31` signal / `2013-01` to `2013-04` holding period.  The
top10 basket was led by 创业板/TMT/中小成长 ETFs (`159915.SZ` returned
`+23.59%`, `159909.SZ` returned `+21.23%`, `159907.SZ` returned `+10.35%`,
and `159902.SZ` returned `+10.32%`).  The production selector did include
`159915.SZ` and `159902.SZ`, but at near-zero weights.  Most risk weight was
instead in `510060.SH`, `510880.SH`, `510160.SH`, and `510190.SH`, which
underperformed.  The root cause is therefore CSI/index scorecard weighting and
theme labeling, not cold-start ETF coverage.

A direction-risk recovery waiver was tested as a rejected candidate.  It
skipped `direction_risk_gate_rejection_cap` when broad participation and market
recovery were already present, lifting `2013-01-04` exposure from `50%` to
`100%`.  This preserved the original hard gate in the full backtest, but it
made the worst structural quarter worse because it simply added exposure to
the wrong basket: the `2013-01-04` portfolio return fell to `-3.77%` and
capture to `-51.86%`.  The candidate was not kept in production code.

Current conclusion: cold-start hotguard is a valid production-quality fix for
short-history overextended technology ETFs, but it is not enough to meet the
new adaptation objective.  The next iteration should upgrade the CSI/index
selection layer to identify small-growth/ChiNext/TMT leadership point-in-time.
The static structural labels should separate `创业板`, `成长`, `中小`,
`国证2000`, and related broad growth ETFs from generic `other`, then test
whether a low-weight, point-in-time small-growth breadth/relative-strength
signal can shift 2012-2013 weights without breaking the 20-year hard gate.

The following iteration implemented that diagnosis as a production candidate.
`structural_subtheme_group_for_text` now assigns `small_growth` to `创业板`,
`成长`, `中小`, `国证2000`, `中证500`, `1000`, and `双创` text buckets.  A new
`weighted_structural_late_cycle_small_growth_recovery_scores` score was added:
it only activates when lagged small-growth/digital ETFs show positive 1-month
recovery, still-lagging 6-month performance, and shallow 3-month drawdown
after a mature finance/industrial/value rebound.  When inactive, it falls back
to the existing late-cycle defensive rotation score.

Structural diagnostics were encouraging before production wiring.  With top3
selection, the small-growth fallback reached hit rate `81.34%`, capture-pass
rate `71.13%`, median capture `54.62%`, and benchmark win rate `95.77%`.
For the `2012-12-31` signal / `2013-01` to `2013-04` target quarter, it
selected `510090.SH`, `159915.SZ`, and `159906.SZ`, with an equal-weight
holding return of about `+7.39%`.

The candidate was promoted to a separate direct ETF policy rather than
overwriting the prior late-cycle policy:
`blend_index_weighted_stable_v9_structural_latecycle_sgrowth_repair_top3_s20_rotcond50_hcres100_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92`.
The full strict quarterly run preserved the original hard gate: `48/48` pass,
minimum final capital `4917.8w`, and worst max drawdown `-19.76%`.

The `2013-01-04` structural quarter was repaired in the full production path:
portfolio return improved from `-1.65%` to `+4.12%`, capture improved from
`-22.63%` to `56.62%`, and the row no longer has a structural failure reason.
The equity ETF weights shifted from mostly `510060.SH`/`510880.SH`/`510160.SH`
to a basket with material `159915.SZ` and `159907.SZ` exposure.

The full adaptation gate still failed:

- recent survival remained `36/48`, with worst rolling 5-year annualized
  return still `4.43%`;
- structural capture remained `0/48`;
- structural failure reasons improved but did not clear:
  `rebalance_or_weighting_lag` fell from `107` to `95`, while
  `risk_control_low_exposure=24` and `scorecard_missed_mainline=21` were
  unchanged.

The new worst structural sample shifted to the `2019-10-31` signal /
`2019-11` to `2020-02` holding period.  This is a different regime: the
successful ETFs were semiconductor/digital/communication names with strong
3-month relative strength and ETF share growth, but many had already pulled
back over 1 month.  The portfolio was at `100%` equity exposure and did beat
沪深300, but returned `-2.74%` versus `+10.34%` for the structural top10,
for a negative capture ratio.  The failure is therefore ETF selection and
weighting lag, not low exposure.

A diagnostic technology-catalyst continuation score was tested and rejected
for now.  It fixed the `2019-10-31` target quarter by selecting
`512480.SH`, `512330.SH`, and `512170.SH`, but it reduced broad structural
robustness: top3 capture-pass rate fell from the sgrowth candidate's `71.13%`
to `61.27%` for the raw score and `66.90%` for the blended score.  It also
misfired on resource/consumer and utilities-led structural quarters, including
`2019-04`, `2023-04`, and `2025-03`, so it was not promoted to production.

Current conclusion: the small-growth recovery score is a valid incremental
production candidate because it repairs the prior worst sample without
breaking the original 20-year hard gate, but the adaptation objective is still
not met.  The next iteration should isolate semiconductor/digital continuation
from false technology rebounds, likely requiring a stricter state variable
that combines 3-month relative strength, ETF share growth, policy/industry
catalyst tags, and a guard against cases where resources, finance, utilities,
or consumer are the actual structural top10.

The next iteration first fixed the structural validation return series.  The
main production backtest already compounds `fund_daily.pct_chg` for ETF return
series so that splits and share conversions do not create false 80%-90%
losses.  The structural adaptation validator had still used raw `close`
ratios.  It now compounds `pct_chg` for domestic ETF structural cross-sections
and keeps 沪深300 on `index_daily.close`.  A regression test covers the
`510210.SH`-style split case where raw close jumps from about `5.0` to `1.0`
while `pct_chg` remains continuous.

A narrow technology pullback-continuation state was then promoted.  It only
activates when semiconductor/digital/communication ETFs have strong 3-month
relative strength, clear but not excessive 3-month drawdown, negative average
1-month momentum, and semiconductor confirmation.  This fixed the
`2019-10-31` target in production: portfolio return improved from `-2.74%` to
`+5.83%`, capture improved from `-26.48%` to `56.41%`, and the failure reason
disappeared.  The full hard gate stayed valid at `48/48`, minimum final
capital `4917.8w`, and worst max drawdown `-19.76%`.  Structural pass count
improved from `0/48` to `8/48`.

The following production candidate added healthcare leadership handling:
`_hcblend85` raises the direct ETF sleeve when healthcare leadership is
point-in-time active, and `weighted_structural_healthcare_leadership_scores`
prevents low-relative-strength finance/value ETFs from diluting the healthcare
basket.  It repaired the `2017-12-31` signal / `2018-01` to `2018-04` target:
capture improved from `-11.10%` to `83.26%`.  The original hard gate remained
valid, and `rebalance_or_weighting_lag` structural failures fell from `75` to
`59`, although structural pass count stayed `8/48`.

A digital reacceleration state was then promoted for the `2023-02-28` signal.
It activates only when digital/communication breadth is broad, 3-month
momentum is positive but not blowoff-like, amount acceleration is high,
correlation with 沪深300 is lower, and crowding is not extreme.  It fixed the
`2023-03` to `2023-06` target in production: capture improved from about
`0.16%` to `69.32%`.  The full hard gate improved to minimum final capital
`5530.5w` with worst max drawdown still `-19.76%`; recent survival remained
`36/48`, but worst 10-year annualized return improved to `14.24%`, worst
2021-2025 cumulative return improved to `60.45%`, and the defense-lag streak
fell to `5`.  Structural pass count reached `9/48`, and
`rebalance_or_weighting_lag` fell to `43`.

A new-energy pullback-restart candidate was tested next.  The diagnostic score
was promising for the `2021-04-30` signal: it identified new-energy ETFs with
strong 6-month leadership, a 3-month pullback, and renewed 1-month strength,
lifting candidate top3 holding return to about `61.84%`.  However, the
production path did not change the target row because outer resilience /
healthcare logic still selected the same basket.  This candidate is therefore
not counted as a production improvement yet; it needs an explicit override
priority or a separate policy path before it can be evaluated as a real
portfolio fix.

Current status after the valid promoted fixes: the strict ETF-only hard gate
passes, but the new adaptation objective is still not met.  The best validated
production report is
`scorecard_csi_strict_quarterly_v9_latecycle_techpullback_hcblend85_digital_s20_structblend85_purestructcond_coldstart_hotguard_report.json`
with adaptation output
`scorecard_csi_strict_quarterly_v9_latecycle_techpullback_hcblend85_digital_s20_structblend85_purestructcond_coldstart_hotguard.json`.
Remaining failures are mainly `risk_control_low_exposure=24`,
`scorecard_missed_mainline=17`, and `rebalance_or_weighting_lag=43`.  The next
worst production sample is the `2021-04-30` signal / `2021-05` to `2021-08`
new-energy structural quarter unless the new-energy override is explicitly
promoted and retested.

The next production iteration tested an explicit new-energy override through
`_neres100` and `_neblend85`.  This branch is now treated as a superseded
overfit diagnostic, not a promotable result: it tied a risk-filter relaxation
and cold-start allowance to a single subtheme name.  Later code paths no longer
let `_neres/_neblend` automatically raise repair or direct exposure; the valid
replacement is the generic local-mainline trigger described below.

The following worst sample became the `2023-03-31` signal / `2023-04` to
`2023-07` holding period.  Diagnosis showed that the direct sleeve was already
large, but the repair score still kept too much red-dividend exposure and too
little communication exposure after a digital/AI blowoff.  A narrow
`structural_digital_blowoff_rotation_active` state was added.  It only
activates when digital-hot ETFs have high 1-month, 3-month, and 6-month
momentum, nearly no drawdown, high crowding, and lower market correlation.
The corresponding rotation score boosts communication and utilities, penalizes
overextended digital-hot ETFs, and `_drotres100/_drotblend85` raises the
repair/direct sleeves only in that state.  The `2023-03-31` target was fixed:
capture improved from `23.56%` to `37.51%`, portfolio return improved from
`2.02%` to `3.21%`, and the failure reason disappeared.

The current best validated production report is now
`scorecard_csi_strict_quarterly_v9_latecycle_techpullback_hcblend85_digital_newenergy_drotres_s20_structblend85_purestructcond_coldstart_hotguard_report.json`,
with adaptation output
`scorecard_csi_strict_quarterly_v9_latecycle_techpullback_hcblend85_digital_newenergy_drotres_s20_structblend85_purestructcond_coldstart_hotguard.json`.
It preserves the original hard gate: `48/48` pass, minimum final capital
`5530.5w`, and worst max drawdown `-19.76%`.  Recent survival remains
`36/48`, worst 10-year annualized return is `14.24%`, worst 2021-2025
cumulative return is `64.15%`, and worst rolling 5-year annualized return is
still `4.43%`.  Structural capture improves to `13/48`, with failure counts
`risk_control_low_exposure=24`, `rebalance_or_weighting_lag=31`, and
`scorecard_missed_mainline=17`.

The new worst structural case is now the `2022-11-30` signal / `2022-12` to
`2023-03` holding period.  The selected risk basket was directionally capable
of capturing the AI/digital rebound, but the actual portfolio exposure was
`0%` because the annual scorecard set `scorecard_limit=0.0` via
`annual_weight_overrides=[[0.6, 0.0]]`.  There were no active hard risk flags
and no bear state.  This is therefore a structural reentry problem after a
macro-weak annual scorecard veto, not another ETF-selection failure.  The next
iteration should test a narrowly gated structural reentry floor for strong
local digital/AI leadership under macro weakness, rather than reopening the
previous broad direction-risk waiver that already failed in 2013.

The next iteration added that narrow reentry floor as
`short_cycle_structural_reentry_signal`.  It requires point-in-time 1-month ETF
basket repair, strong 1-month cross-sectional dispersion, broad 1-month
participation, contained 3-month drawdown, positive selected-ETF 3-month
momentum, and no active risk flags.  A low-exposure diagnostic showed it only
triggered the December 2022 drift rows in the current report, with all 16
incremental returns positive.  In production the `2022-12-08` case moved from
`0%` to `50%` exposure and portfolio return improved from about `2.19%` to
`4.53%`.  The hard gate still passed at `48/48`, minimum final capital improved
to `5690.0w`, and worst drawdown stayed `-19.76%`.  Structural capture was
unchanged at `13/48`, but the low-exposure failure count fell from `24` to
`16`.

The following worst sample was the `2025-02-28` signal / `2025-03` to
`2025-05` holding period.  The top structural basket was dominated by domestic
gold/defensive ETFs.  The strategy already held gold through the defensive
sleeve, but `bond_gold65_252d` capped that exposure too low and the equity
sleeve dragged the portfolio return down.  Adding explicit defensive candidates
`bond_gold80_252d` and `bond_gold100_252d` tested this as a defensive-sleeve
capacity issue, not an equity selector issue.  `bond_gold80_252d` preserved the
hard gate (`48/48`, minimum final capital `5780.5w`, drawdown `-19.76%`) and
raised structural capture to `16/48`; the 2025 gold quarter improved but still
sat just below the capture hurdle.

The next worst sample became the `2023-01-31` signal / `2023-02` to
`2023-05` holding period.  The AI/online-consumption candidates were already
high in the point-in-time score, but `structural_digital_reacceleration_active`
did not turn on because ETF amount/share acceleration lagged price breadth.
An early digital reacceleration branch was added: it still requires at least
eight digital/communication candidates, strong 1-month price diffusion,
positive 3-month momentum, low 6-month correlation, controlled drawdown, and
low crowding, but lowers the flow-acceleration confirmation threshold.  A
snapshot scan showed this added only `2023-01-31`.  Production remained hard
gate compliant (`48/48`, minimum final capital `5780.5w`, drawdown `-19.76%`)
and structural capture improved to `20/48`; worst 2021-2025 cumulative return
rose to `73.16%`.

A state-priority fix was then tested so digital blowoff rotation is evaluated
before ordinary digital reacceleration.  This preserves the more specific
post-blowoff communication/utilities rotation state.  The hard gate still
passed and structural capture stayed `20/48`; the remaining worst case stayed
the `2023-03-31` signal / `2023-04` to `2023-07` holding period.  The strategy
was already full exposure and already allocated most of the repair sleeve to
communication/TMT, but the realized top structural basket broadened into
electric power / green power ETFs, including several newer ETFs without
SHARE_V5 feature rows.

A wider repair-sleeve experiment (`repair_top5`) was rejected.  It improved
the hard-gate headline (`48/48`, minimum final capital `6044.3w`, drawdown
`-18.35%`) but reduced structural capture from `20/48` to `16/48`.  The
`2023-03-31` worst case improved only slightly and remained below the capture
hurdle, while other structural windows became diluted.  The best current
adaptation branch is therefore the `repair_top3` version with
`screentry50 + bond_gold80_252d + early digital reacceleration + blowoff
priority`: hard gate `48/48`, minimum final capital `5780.5w`, worst drawdown
`-19.76%`, recent survival `36/48`, structural capture `20/48`, worst
10-year annualized return `14.90%`, worst 2021-2025 cumulative return
`73.16%`, and worst rolling 5-year annualized return `4.43%`.  The next
diagnosis should focus on 2023Q2 communication-to-utilities rotation and
whether cold-start price-only coverage for newly listed power/green-power ETFs
can be improved without broadening the repair sleeve globally.

That diagnosis showed the issue was not a broad cold-start gap.  Several new
green-power ETFs lacked SHARE_V5 rows, but at the `2023-03-31` signal their
1-month momentum was negative or their price history was too short.  The
point-in-time evidence was instead in older power / green-power ETFs already
covered by SHARE_V5: they had positive 3-month momentum, shallow drawdowns,
low crowding, and appeared just below the communication top tier in the
digital-blowoff score.  A narrow
`structural_digital_blowoff_utilities_rotation_active` state was added.  It
requires digital blowoff to already be active and at least two utilities ETFs
with positive 3-month momentum, controlled 6-month momentum, shallow 3-month
drawdown, and non-crowded trading.  A snapshot scan found this state only on
`2023-03-31` in the current SHARE_V5 history.  The score now lifts utilities
inside the existing blowoff rotation state; the `2023-04-07` equity basket
shifted to roughly `30.6%` power ETF, `28.0%` green-power ETF, and `27.8%`
communication ETF, raising that quarter's portfolio return to `4.52%`.

The new best validated adaptation branch is
`scorecard_csi_strict_quarterly_v9_latecycle_techpullback_hcblend85_digital_newenergy_drotres_s20_structblend85_purestructcond_coldstart_hotguard_screentry50_gold80_earlydigital_blowoffprio_utilrot`.
It preserves the original hard gate: `48/48`, minimum final capital
`5780.5w`, and worst drawdown `-19.76%`.  Structural capture improves to
`24/48`, with failure counts `risk_control_low_exposure=8`,
`rebalance_or_weighting_lag=24`, and `scorecard_missed_mainline=10`.  Recent
survival remains `36/48`; worst 10-year annualized return is `14.90%`, worst
2021-2025 cumulative return is `73.16%`, and worst rolling 5-year annualized
return remains `4.43%`.

The next structural bottleneck is the `2020-12-31` signal / `2021-01` to
`2021-04` holding period.  The strategy was fully invested and captured
`28.38%` of the top10 basket, just below the `30%` hurdle.  The future winners
were steel, banks, energy/resources, and value ETFs, while the actual direct
basket was concentrated in nonferrous metals plus broad/value ETFs.  A
resources/finance rotation state was not promoted because point-in-time group
evidence was weak at the signal date: the resources/finance group had negative
1-month momentum, only moderate 3-month momentum, a borderline drawdown, and
high crowding.  The next recent-survival bottleneck is separate: the worst
rolling 5-year window is still `2009-06` to `2014-03`, driven mostly by early
history low-exposure / zero-exposure risk controls.  Improving that window
requires a dedicated early-history opportunity-floor diagnosis rather than a
structural theme-selection change.

The recent-survival diagnosis focused on the failing rolling 5-year windows
around `2009-06` to `2014-05`.  A broad low-exposure floor was rejected because
it would have added exposure into losing 2010 windows.  The useful point-in-time
pattern was narrower: no active risk flags, a deep 6-month broad/basket
drawdown, poor 3-month breadth, controlled volatility, and either a positive
PBoC policy-outlook tone or an early 1-month breadth repair.  This became the
`policy_supported_oversold_reentry` stage, exposed through
`q_mdd20_qfree_stack_highdist800_screentry50_oversoldpol*`.

The best tested cap is `oversoldpol700`.  It triggers only on the November and
December 2012 drift rows in the current 48-sample report, raising exposure to
`70%` after the policy-supported oversold washout.  The strict hard gate still
passes: `48/48`, minimum final capital `6294.8w`, worst drawdown `-19.76%`.
Recent survival now passes `48/48`, and the worst rolling 5-year annualized
return improves from `4.43%` to `5.08%`.  The 2016-2025 and 2021-2025 floors
were already above threshold and remain above threshold.

Structural capture is still the remaining unsolved gate at `24/48`.  Two
structure-focused experiments were rejected in this round.  First, a crisis
dispersion reentry floor only fired on adjacent 2013 drift rows and did not
move the failing rolling-window samples, so it was removed from the rule code.
Second, replacing the current tech-pullback route with the existing pure
`latecycle_repair` route preserved the original hard gate but reduced minimum
final capital to `5597.2w` and collapsed structural capture to `0/48`.  The
standalone recipe diagnostic still shows `late_cycle_defensive_rotation_v1`
has better structural top5 hit-rate than the current score, but applying it
globally breaks other structural periods such as 2018 healthcare.  The next
iteration should therefore test a conditional resources/finance/gold rotation
inside the current direct policy, with explicit guards for 2018 healthcare and
2019/2022 digital or resources reversals, rather than globally switching to
late-cycle defensive scoring.

The next iteration tested that conditional finance-defensive idea as a named
suffix on the current tech-pullback policy:
`_findefres100`.  The point-in-time diagnosis was valid for the `2025-02-28`
signal: the direct sleeve moved from a mixed bank/software basket to a more
defensive finance/resources basket (`510650.SH`, `515290.SH`, `516020.SH`).
However the formal strict quarterly backtest rejected the candidate:
`36/48`, minimum final capital `6587.5w`, and worst drawdown `-20.35%`.  The
failing samples were all drawdown failures around the `2018-05` decision /
`2018-07-11` drawdown trough, where the market state already showed weak
3-month CSI 300 return, negative PBoC tone, negative M1-M2 scissors change,
and a selected ETF basket with only moderate volatility but too much residual
equity exposure.  This branch should not be promoted unless the finance
defensive override is paired with an explicit 2018-style macro/market damage
cap.

Execution efficiency also became a bottleneck.  The old exploratory workflow
used the full strict backtest with `--include-decision-rows`, which can take
more than ten minutes for a single direct-policy variant and writes multi-GB
path caches keyed by the full direct policy name.  The workflow should now be:
first replay against an existing audited decision-row report with
`scripts/replay_scorecard_csi_strict_quarterly_etf_direct_policy.py
--summary-only --fail-fast`.  The replay path keeps the audited CSI selector
decisions fixed, recomputes only the direct ETF overlay / defensive sleeve /
daily valuation layer, prints each phase/lag case, and stops on the first hard
gate miss.  On the rejected `_findefres100` candidate it stopped after
`108.6s`, at `phase=2, lag=0`, instead of waiting for the full strict backtest
to finish.  This is a first-pass rejection tool, not final proof.

For variants that cannot be evaluated from an existing audited report, use
`scripts/screen_scorecard_csi_strict_quarterly_etf_candidate.py` without
decision rows and, when diagnosing a known weak point, pass `--phase` and
`--lag` for that sample.  That path builds one phase/lag path at a time and
stores a small selector base-path cache under
`data/backtests/cache/strict_quarterly_base_paths` that is reusable across
direct ETF policies, but the first base-path build is still slower than replay
because it must run the CSI snapshot selector.  Only candidates that pass the
fast replay/screen layer should be rerun through the full strict quarterly
script with `--include-decision-rows` for structural-adaptation validation.

The replay/screen tools now also share a local raw market-data cache through
`scripts/strict_quarterly_data_cache.py`.  This cache stores the MySQL-loaded
historical price series and ETF universes under
`data/backtests/cache/strict_quarterly_market_data`; it does not cache strategy
features or ETF decisions, which are still recomputed point-in-time by the
caller.  Use `--refresh-data-cache` after a market-data sync, or
`--no-data-cache` for a fully uncached check.  On the same rejected
`_findefres100` replay, a single `phase=3, lag=3` sample fell from about
`46s` to `18.3s` after cache and selector-current-row memoization, while
the full `--summary-only --fail-fast` rejection fell from `108.6s` to
`44.1s` and still stopped at the same `phase=2, lag=0` drawdown failure
(`final=8942.4w`, `mdd=-26.15%`).  This makes the intended iteration loop:
diagnose worst structural/recent sample, replay candidate with cache, reject
hard-gate failures quickly, then spend full-run time only on candidates that
survive the fast layer.

The next replay iteration paired `_findefres100` with a replay-only
2018-style macro/market damage cap.  The first narrow cap used CSI 300 3-month
damage, negative PBoC tone, negative M1-M2 scissors change, ETF basket
drawdown, and controlled volatility; it improved the rejected `phase=2,
lag=0` sample from `mdd=-26.15%` to `-23.40%` at a 45% cap, but did not pass.
Adding a continuation branch for damaged 6-month broad/basket trend improved
the same sample to `-20.37%` at a 20% cap.  The remaining drawdown came from
the preceding `2018-02` full-exposure window, where the point-in-time state
already showed negative PBoC tone, weakening M1-M2 scissors, and an active
`low_vol_mature_trend_flag`; adding that pre-damage branch lifted the same
sample to `final=10859.2w`, `mdd=-18.18%`.  With cached replay, the full
`--summary-only --fail-fast` screen then passed `48/48`, with minimum final
capital `5931.2w` and worst drawdown `-18.35%`.

However, the finance-defensive overlay still failed the new objective once
structural adaptation was checked.  The broad `_findefres100 + macrocap20`
replay passed recent survival `48/48`, but structural capture fell to `12/48`;
the worst new miss was a `2023-02-01` / `2023-05-04` digital/科创 structural
quarter where finance defensives displaced the actual top-10 ETF winners.
The finance override was therefore narrowed with a market confirmation gate:
PBoC tone positive, M1-M2 scissors change nonnegative, CSI 300 3-month return
muted, weak 1-month basket participation, shallow 6-month basket drawdown, and
controlled 3-month basket volatility.  That narrower replay still passed the
hard gate `48/48` (`min=6122.6w`, `mdd=-19.76%`) and recent survival `48/48`,
but structural capture was only `19/48`, below the current baseline `24/48`.
Conclusion: the 2018 damage cap is useful as a diagnostic/replay tool, but
`_findefres100` should remain rejected.  The next structural work should
return to the original direct policy and target the remaining capture failures
directly, especially `2021-01` resources/finance/value undercapture and the
near-threshold `2019-03` / `2022-12` quarters, instead of applying a global
finance-defensive override.

The replay loop has since been optimized for repeated candidate iteration.
`CodeReturnCache` now carries a price cache, supervised ETF observations can
be loaded from an mtime/size-validated pickle cache under
`data/backtests/cache/passive_etf_candidate_observations`, current-snapshot ETF
cross sections are memoized and safely keyed, and replay shares equity/defense
weight caches across phase/lag cases.  This exploits the fact that the 3,840
quarterly decisions in the 48-case matrix use only 249 unique rebalance
anchors, with each anchor reused up to 16 times.  A 6-worker process replay was
tested but was slower (`103.3s`) because process startup and market-data copy
overhead dominated, so the recommended default remains single-process replay.
On the current baseline direct ETF policy, full 48-case replay stayed
numerically unchanged (`48/48`, minimum final `6293.0w`, worst drawdown
`-19.76%`) while hot-cache `--summary-only` runtime fell to `46.5s`; full
decision-row replay fell to `60.9s`.  The structural-adaptation validator
still consumes the optimized rows output normally; the replayed baseline rows
remain a structural-capture failure, as expected, and are suitable for the next
diagnostic iteration.

A repeat hot-cache baseline check on the same direct ETF policy produced the
same `48/48`, `6293.0w`, `-19.76%` result in `48.28s`.  Known weak-sample
replays such as `phase=1, lag=0` now run in roughly `14s` to `16s`, so the
iteration budget should start with targeted weak samples and reserve the
48-case replay for candidates that improve those samples.

The next candidate tested a narrow value/reflation overlay on top of the
current late-cycle tech-pullback baseline.  The first version
(`_valres100_valblend85`) used PBoC-positive, M1-M2-improving, non-crisis
market confirmation plus a new resources/finance/broad-value score.  It
initially over-selected industrial/resource proxies in `2021-01`, failing the
hard gate for `phase=1, lag=0` (`mdd=-20.67%`).  After constraining activation
to weak/muted CSI 300 3-month conditions (`-5%` to `+5%`) and requiring
positive 1-month ETF-basket participation, it passed the hard gate `48/48`
(`min=6702.4w`, `mdd=-18.35%`) and repaired several `2025-03`
scorecard-missed mainline quarters, but structural capture was still only
`23/48`.  A lower-strength version (`_valres70_valblend70`) also passed the
hard gate (`min=6714.3w`, `mdd=-18.35%`) and recent survival `48/48`, but
again reached only `23/48` structural capture.  The branch reduced replay
scorecard-missed mainline failures (`13` to `6` versus replay baseline) but did
not solve the net objective and should remain experimental.

The failed diagnosis is useful: `2025` muted-broad resources/finance rotation
is partly identifiable with the new confirmation gate, but `2021-01` is a
different problem.  At `2020-12-31`, the broad index was still strong, so the
muted-broad value gate should not fire.  The future top-10 structural basket
was finance/resource-heavy, and several investable finance ETFs missing
`SHARE_V5` rows were visible through point-in-time price cold-start scoring
(`512730.SH`, `515020.SH`, `512700.SH`, `512820.SH`).  The next iteration
should therefore test a separate finance catch-up/cold-start branch for strong
broad-but-rotating markets, rather than broadening the value/reflation overlay.

That finance catch-up branch was then tested only on the known weak sample
before spending a full 48-case run.  The market confirmation correctly fired
on the `2020-12-31` signal using positive PBoC tone, improving M1-M2 scissors,
strong but not overheated CSI 300 3-month return, broad 1-month ETF
participation, shallow drawdown, and controlled volatility.  The first version
allowed non-finance ETFs into the score at a discount and produced a
`2021-01-04` phase 1 / lag 0 quarter return of `-6.79%`.  After constraining
the finance-catchup scorer to finance ETFs only, the direct sleeve was pure
finance (`159905.SZ`, `515850.SH`, `512570.SH`) with an `85%` direct blend, but
the same quarter deteriorated to `-8.70%` and the sample final capital fell to
`7736.9w` despite still passing the original hard gate.  The branch was
therefore rejected without a full 48-case replay.

The diagnosis is now narrower: the point-in-time finance catch-up gate is
plausible, but the ETF selection features prefer broker/insurance and cold-start
finance products over the bank/dividend names that held up better in the next
quarter.  The next signal should add an explicit finance-subsector or style
feature, such as bank/dividend versus broker/insurance, and test it first on
`2021-01` before any full replay.  This confirms the faster iteration protocol:
single weak-sample replay first, then full cached replay only after the weak
sample improves for the right reason.

The follow-up implemented static finance substyle tags from ETF/index names:
`bank_dividend`, `broker_insurance`, `real_estate`, and `broad_finance`.  The
classifier deliberately checks broker/insurance before dividend keywords, so a
mixed `证券保险红利` name is not treated as a defensive bank/dividend ETF.  A
new `_fcbankres100_fcbankblend85` branch reused the same point-in-time finance
catch-up gate but restricted the direct sleeve and price cold-start candidates
to bank/dividend finance ETFs.  The targeted `phase=1, lag=0` weak-sample
replay improved the `2021-01-04` holding-period return from the rejected
finance-catchup result of `-8.70%` to `+2.55%`, with holdings concentrated in
`159905.SZ`, `512800.SH`, and cold-start `512730.SH`.  The one-sample final
capital was `8691.2w`, still inside the original hard gate.

Full cached replay then passed the original hard gate (`48/48`, minimum final
capital `6293.0w`, worst drawdown `-19.76%`), but structural adaptation fell
to `14/48`.  The incremental failures were all `2020-12-31` signal / `2021-01`
holding periods with execution lag `1` or `5`; the bank/dividend basket fixed
same-day execution but under-captured delayed-entry quarters where the ex-post
top10 was a broader bank/resource basket (`515210.SH`, `515220.SH`,
`159981.SZ`, `159980.SZ`, plus bank ETFs).  A broader
`_finres100_finresblend85` branch that allowed bank/dividend plus resources
was tested only on `phase=1` lags `0/1/5`; it did not improve the weak samples
enough (`8199.9w`, `8396.8w`, `7418.1w`) and was rejected without full replay.
A lower `fcbankblend50` variant was also rejected at the weak-sample layer.

Conclusion: the new static finance-substyle feature is useful diagnostic
infrastructure, but the accepted strategy should not promote the bank or
bank+resource catch-up branches yet.  The remaining 2021 structural failure is
partly an execution-lag problem: the selector needs either a more timing-aware
resource/bank confirmation or a lag-robust basket construction test, not simply
a stronger finance override.

The next improvement came from the defensive sleeve rather than the direct ETF
selector.  The replay baseline showed three `2019-03-01` structural failures
with capture ratio `29.96%`, just below the `30%` hurdle.  Those rows had no
active risk flags, but the feature exposure cap reduced equity exposure to
`10%`; the portfolio was already mostly defensive (`72%` gold ETF and `18%`
bond ETF).  Raising equity would have hurt that quarter because the ETF risk
sleeve lagged the defensive gold sleeve.  A defensive sensitivity check with
the existing `bond_gold100_252d` policy raised the same quarter's return from
`2.23%` to `2.74%`, moving the implied capture ratio to about `36.9%`.

The `bond_gold100_252d` candidate passed the cached replay hard gate:
`48/48`, minimum final capital `6486.9w`, and worst drawdown `-19.76%`; replay
structural capture improved from `22/48` to `32/48` and recent survival stayed
`48/48`.  Because changing defensive returns can alter CPPI path state, the
candidate was then rerun through the production-path screen rather than relying
on replay alone:

```bash
.venv/bin/python scripts/screen_scorecard_csi_strict_quarterly_etf_candidate.py \
  --rule q_mdd20_qfree_stack_highdist800_screentry50_oversoldpol700 \
  --defensive-policy bond_gold100_252d \
  --selector-policy expanded_value_risk_top7_power8_cap45 \
  --direct-etf-policy blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_rotcond50_hcres100_hcblend85_neres100_neblend85_drotres100_drotblend85_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92 \
  --full-matrix --include-decision-rows \
  --output-prefix data/backtests/screen_gold100_full_rows
```

The formal screen passed the original hard gate: `48/48`, minimum final capital
`6419.7w`, worst drawdown `-19.76%`.  The structural-adaptation validator was
extended to accept this screen report shape with top-level `cases`; validation
then showed recent survival `48/48`, structural capture `34/48`, worst 10-year
annualized return `15.36%`, worst 2021-2025 cumulative return `78.95%`, and
worst rolling 5-year annualized return `5.08%`.  The new candidate is a real
improvement but still fails the full structural objective.

Remaining structural failures under the formal `bond_gold100_252d` screen are
now concentrated in four clusters:

- `2020-12-31` / `2021-01-04` bank/resource catch-up: `4` cases, capture about
  `28.4%`, fully invested, failure reason `rebalance_or_weighting_lag`.
- `2022-03-31` / `2022-04-08` zero-exposure structural quarter: `4` cases,
  failure reason `risk_control_low_exposure`.
- `2017-10-31` / `2017-11-08` finance/bank lag quarter: `4` cases, capture
  about `20.2%`, fully invested.
- `2025-02-28` / `2025-03` gold/bank/香港创新药-style miss: `4` cases, failure
  reason `scorecard_missed_mainline`.

Next iteration should use `bond_gold100_252d` as the current best candidate and
target these remaining clusters in order, starting with the `2021-01` and
`2017-11` lag-robust finance/bank/resource selector problem.

The execution loop was tightened again after the formal screen became the new
best source report.  `scripts/screen_scorecard_csi_strict_quarterly_etf_candidate.py`
now supports explicit `--case PHASE:LAG` samples and
`--failed-structural-cases-from <adaptation.json>`, which extracts the
phase/lag pairs from `structural_capture.failed_structural_cases`.  Partial
screens now write a partial summary and return success when every selected
sample passes the original hard gate, without claiming the full 48-sample
objective.  This makes the recommended loop:

1. Validate a proposed rule change on one known weak sample with `--case`.
2. Run the structural-failure subset from the latest adaptation report.
3. Only then spend time on the full 48-case screen with decision rows.

On the current `bond_gold100_252d` source, a single failed structural sample
selected from `screen_gold100_full_rows_adaptation.json` ran in `13.9s`
(`phase=1`, `lag=0`, `final=9363.1w`, `mdd=-16.95%`, partial summary).  The
full failed-structural subset selected `14` phase/lag cases and completed in
`74.8s`, with all original hard gates still passing and a subset worst case of
`final=8803.7w`, `mdd=-18.26%`.  This replaces a default first-pass full
screen that previously took about `636s` with decision rows.

`scripts/replay_scorecard_csi_strict_quarterly_etf_direct_policy.py` now also
accepts both legacy `results[]` reports and the newer fast-screen top-level
`cases` report shape.  A direct single-sample replay from
`screen_gold100_full_rows_report.json` completed in `28.0s` and returned
success for the partial hard-gate smoke (`phase=1`, `lag=0`, `final=9303.3w`,
`mdd=-16.95%`).  Use this when testing direct ETF policy variants against the
latest formally screened rows rather than an older baseline report.

The next accepted micro-repair targets the near-threshold `2025-03` structural
miss without increasing equity risk.  The failing `phase=6/9, lag=3` rows had
`option_panic_after_rally_flag`, positive policy/liquidity support, strong
ETF 1-month participation, and elevated ETF volatility; the risk sleeve lost
money while the all-gold defensive sleeve led the top-10 structural basket.
A replay-only diagnostic showed that capping the post-rally option-panic
exposure from `56.25%` to `55%` raised those quarters from `3.28%` to `3.53%`.
Replay alone was not sufficient evidence because replay recomputes the direct
ETF layer and showed phase-specific side effects, so the rule was promoted
only as a formal candidate:

`q_mdd20_qfree_stack_highdist800_screentry50_oversoldpol700_opcap550`.

The formal 2025 probe on `phase=3/6/9` and `lag=3/5` passed both hard and
structural checks: hard gate `6/6`, structural `6/6`.  The old failed-case
subset then passed the hard gate `14/14` and structurally fixed exactly
`phase=6, lag=3` and `phase=9, lag=3`.  The complete formal full-row screen
passed the original hard gate `48/48`, with minimum final capital `6419.7w`
and worst drawdown `-19.76%`; structural adaptation improved from `34/48` to
`36/48`, recent survival stayed `48/48`, worst 10-year annualized return was
`15.36%`, worst 2021-2025 cumulative return was `78.95%`, and worst rolling
5-year annualized return stayed `5.08%`.

The remaining structural failures under `_opcap550` are now 12 cases in three
clusters:

- `2020-12-31` / `2021-01-04` bank/resource catch-up: `4` cases, capture
  `28.4%`, fully invested, still just below the `30%` capture gate.
- `2022-03-31` / `2022-04-08` healthcare/biotech rebound with zero exposure:
  `4` cases, still a `risk_control_low_exposure` failure.
- `2017-10-31` / `2017-11-08` finance/bank lag quarter: `4` cases, capture
  `20.2%`, fully invested, still a selector / rebalance-lag problem.

Next iteration should use `_opcap550` as the current best candidate and avoid
spending more time on 2025 gold-defense tuning unless a later full-row
validation shows a regression.

The next diagnostic pass focused on execution speed before another full
48-case validation.  A new direct ETF candidate was added:

`blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_rotcond50_hcres100_hcblend85_neres100_neblend85_drotres100_drotblend85_rbres100_rbblend85_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92`.

The `_rbres100/_rbblend85` repair is a point-in-time resource + pure-bank
catch-up path.  It explicitly excludes new-energy, photovoltaic, battery,
broker, insurance, securities, and non-bank-finance false positives, then
uses current 1m/3m/6m momentum, relative strength, drawdown, correlation,
crowding, volatility, ETF share growth, and `days_since_high_6m` to prefer
moderate catch-up candidates over resources already sitting near a 6-month
high.  The 2020-12-31 probe moved the 2021-01 direct sleeve from noisy
resource/new-energy exposure to `159981.SZ`, `159980.SZ`, and `512800.SH`.

During the same pass, the healthcare leadership trigger was widened for
defensive structural markets: healthcare no longer has to be the absolute
top 3-month momentum group when it is close to the leading groups and has
strong internal breadth plus ETF share confirmation.  This fixed a newly
exposed 2020-01 near-threshold structural quarter after the 2021 failure was
repaired.

Targeted validation results:

- 2021 bank/resource failure probe (`phase=1/4/7/10`, `lag=0`): hard gate
  `4/4`, structural `4/4`, worst 10-year annualized `21.24%`, worst
  2021-2025 cumulative `153.69%`, worst rolling 5-year annualized `7.04%`.
- Old `_opcap550` structural-failure subset: hard gate `12/12`, recent
  survival `12/12`, structural `4/12`, worst 10-year annualized `21.24%`,
  worst 2021-2025 cumulative `135.95%`, worst rolling 5-year annualized
  `5.60%`.

The remaining subset failures are still the same two unrepaired clusters:

- `2022-03-31` / `2022-04-08`: healthcare/new-energy rebound top10 with
  zero equity exposure, still `risk_control_low_exposure`.
- `2017-10-31` / `2017-11-08`: finance/resource/value rebound with full
  exposure but weak capture, still `rebalance_or_weighting_lag`.

Execution efficiency improved in the screen script as well.
`scripts/screen_scorecard_csi_strict_quarterly_etf_candidate.py` now accepts
`--jobs N` for case-level parallelism when `--full-matrix` is used.  The
parallel path keeps fail-fast behavior unchanged by staying disabled unless
`--full-matrix` is set, and writes cases back in deterministic selected-pair
order for the validators.  On this run, the old 12-case structural-failure
subset went from about `63.8s` serial to `39.9s` with `--jobs 4`, with the
same hard-gate and adaptation results.

Do not promote `_rbres100/_rbblend85` to the new best full candidate until a
full 48-case row validation confirms no hidden drawdown or recent-survival
regression.  The next efficient loop should run the remaining 2022 low-exposure
cluster first, then the 2017 lag cluster, before spending the full 48-case
budget.

The follow-up pass rejected the single-theme new-energy repair as a promotable
answer and replaced it with a generic local-mainline pullback / capitulation /
reentry detector.  The valid candidate is now:

`blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_rotcond50_hcres100_hcblend85_lmres100_lmblend85_drotres100_drotblend85_rbres100_rbblend85_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92`.

The local-mainline detector has no `new_energy` hard-coded activation.  It
selects whichever subtheme has at least two current point-in-time ETF candidates
with strong prior leadership or relative strength, visible 1-month / 3-month
pullback or restart, lower market correlation, controlled volatility, and
non-extreme crowding.  The active subtheme is chosen by candidate count and
6-month relative strength; cold-start repair is restricted to that active
subtheme set.  The 6-month drawdown filter is relaxed to `-30%` only when this
generic detector is active and the individual ETF itself satisfies the
local-mainline candidate test.  Legacy `_neres/_neblend` policy suffixes no
longer trigger automatic direct-blend or repair-sleeve upgrades, and
`new_energy` restart no longer turns on the wider resilience filter inside the
late-cycle tech-pullback scorer.

The 2017 finance-lag cluster was handled separately with a generic
finance/value breadth-rotation confirmation, not a theme exemption.  It requires
constructive 3-month and 6-month broad-market returns, high 1-month and 3-month
ETF breadth, low 6-month basket drawdown, low 3-month basket volatility, and no
crisis/liquidity/credit-stress flags.

Validation after the generic repair:

- Old `_opcap550` structural-failure subset with `--jobs 4`: hard gate `12/12`,
  minimum final capital `11690.9w`, worst drawdown `-18.26%`; adaptation
  diagnostics `recent=12/12`, `structural=12/12`, worst 10-year annualized
  `23.53%`, worst 2021-2025 cumulative `176.78%`, worst rolling 5-year
  annualized `5.60%`.
- Full 48-sample validation with `--jobs 4`: hard gate `48/48`, minimum final
  capital `5655.9w`, worst drawdown `-19.76%`, no partial output and no
  early-stopped cases.
- Full structural-adaptation validation:
  `adaptation_objective_met=True`, `recent=48/48`, `structural=48/48`, worst
  10-year annualized `13.89%`, worst 2021-2025 cumulative `77.60%`, and worst
  rolling 5-year annualized `5.08%`.

Regression coverage now includes tests that `neblend` no longer raises direct
share, `lmblend` raises only under the generic local-mainline active state, and
the same local-mainline detector can select non-new-energy subthemes such as
healthcare and semiconductor.
