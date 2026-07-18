# Scorecard + CSI Generalization Target

## Objective

Optimize the macro scorecard and CSI selection scorecard so a portfolio starting with
1,000,000 compounds above 40,000,000 over the 2006-2025 backtest while keeping max
drawdown within 10% across all rebalance timing variants.

The strict validation target is:

- final capital >= 40,000,000
- max drawdown >= -10%
- all execution-lag, frequency, annual month-drift, quarterly month-drift, and
  monthly pressure cases pass

## Current Validated State

The current natural-quarter strategy passes only the base quarterly case:

- base quarterly: 45,591,000 final capital, -9.0% max drawdown
- execution drift: 1 / 4 strict cases pass
- review frequency: 1 / 3 strict cases pass
- annual month drift: 0 / 48 strict cases pass
- quarterly month drift: 1 / 48 strict cases pass
- monthly pressure: 0 / 4 strict cases pass

The strict report is written to:

- `data/backtests/scorecard_csi_generalization_report.json`

## Diagnosis

The previous validation standard was too loose for the new target. It allowed:

- annualized return thresholds below the required 40,000,000 final-capital target
- drawdowns as deep as 18%-25% in stress cases
- only three quarterly month phases instead of all twelve possible month phases

The strict version now tests all 12 annual and quarterly rebalance start-month phases,
with 0/1/3/5 trading-day execution lags.

The weakest scenarios cluster around month-phase offsets 5 and 6. This suggests the
current portfolio is still too dependent on natural calendar-year and natural-quarter
cut points. A quick dynamic 12M-momentum selection probe did not solve the problem:
monthly, quarterly, and annual dynamic momentum variants all had 0 / 12 passing
month phases, with worst drawdowns around 30%-44%.

## 2026-07-15 Follow-Up

The drift validator now caches trade-date shifts, index period returns, and scorecard
snapshots. The strict full matrix runs in roughly 26-27 seconds instead of several
minutes, which makes it usable as a recurring QA gate and as a parameter-search
harness.

The quarterly drift logic was also made date-aware: when a shifted quarter crosses
into a new apply year, the CSI basket is rolled using the shifted snapshot date
instead of holding the prior natural-year basket. This did not solve the target:

- base quarterly remains around 45.6m final capital with -9.0% max drawdown
- annual month drift remains 0 / 48 strict passes
- quarterly month drift remains 1 / 48 strict passes
- worst quarterly month-drift drawdown is about -43%

A candidate rule that reduced exposure whenever the scorecard target fell below the
current exposure at any review point was rejected. It lowered the base case from
about 45.6m to 41.4m and worsened monthly-pressure returns without fixing drift
robustness.

Defensive asset audit:

- cash at 2% has full coverage but cannot close the return gap.
- a synthetic US 10Y treasury duration proxy built from `us_tycr_daily.y10` has
  95% annual-window coverage, about 2.7% annualized return, near-zero correlation
  to CSI 300, but its own drawdown is around -17.7%. It is useful as a hypothesis
  and feature input, not as a validated low-drawdown defensive asset.
- SGE gold spot has attractive 2017-2025 behavior, about 15.6% annualized and -4.1%
  max drawdown on covered annual windows, but only covers 45% of the 2006-2025
  annual sample.
- local 5Y treasury index/fund price coverage is missing from current tables, so a
  bond defensive leg cannot yet be validated over the full target window.
- S&P 500 has full coverage and higher return than cash, but its -38% drawdown makes
  it unsuitable as a low-drawdown defensive substitute.

## Overlay Search

`scripts/search_scorecard_csi_strict_overlays.py` reuses the strict validator and
injects low-dimensional `QuarterlyOverlay` candidates. The first quick search tested
weak-repair caps, earlier falling-knife caps, H1 rally take-profit caps, weak-momentum
exhaustion caps, post-stimulus caps, and stagflation caps.

Result: no candidate came close to the target.

- baseline quick screen: 4 / 33 strict cases pass; worst case about 258w; worst
  drawdown about -42%.
- best low-dimensional variants still had worst cases around 226w-271w and drawdowns
  around -39% to -45%.
- full lag/phase search confirms the same conclusion: baseline is 4 / 108 strict
  passes, and searched variants ranged from 0 / 108 to 5 / 108 strict passes. Worst
  final capital stayed around 210w-246w and worst drawdown stayed around -41% to
  -45%.
- relaxing weak-repair caps can improve the natural base case but worsens drift
  drawdown.
- tightening weak-repair caps can reduce some losses but gives up too much return
  and still fails the target.

This rejects the hypothesis that the current feature set can meet the objective
through small quarterly-overlay threshold changes alone.

## Dynamic Defense Experiment

`scripts/backtest_scorecard_csi_dynamic_defense.py` tests a structural alternative:
monthly review, date-aware annual CSI baskets, CS300 trend caps, portfolio drawdown
caps, and optional defensive legs (`cash`, `gold_if_up`, `spx_if_up`).

Result: this also does not meet the target.

- all tested candidates were 0 / 48 strict phase/lag passes.
- baseline monthly had a worst final capital around 526w and worst drawdown around
  -47%.
- stronger trend and drawdown stops reduced drawdown in some cases but drove worst
  final capital down to roughly 117w-431w.
- gold and SPX defensive legs improved some medians versus cash but still left worst
  final capital far below 4000w and drawdowns above the 10% target.

This rejects a second hypothesis: simple monthly trend stops plus a basic defensive
leg are not sufficient either.

## Volatility Target Experiment

`scripts/backtest_scorecard_csi_vol_target.py` tests another structural
alternative: monthly risk-budgeting based on trailing realized volatility of the
selected CSI basket, drawdown and CS300 trend caps, optional moderate leverage,
and defensive legs including cash, gold-if-up, and the US 10Y duration proxy.

Result: this does not meet the target either.

- all tested candidates were 0 / 48 strict phase/lag passes.
- low-volatility caps cut exposure too aggressively, leaving worst final capital
  around 227w-379w for cash-defense variants.
- the US 10Y proxy improved some worst final-capital outcomes versus cash, but
  still topped out around 482w in the tested set and left drawdowns far beyond the
  10% target.
- gold-if-up had the best worst final capital in this search, around 506w, but
  still failed both the 4000w capital target and the drawdown target.

This rejects a third hypothesis: simple volatility targeting and moderate
risk-budget leverage are not enough to turn the current selector into a
phase-stable strategy.

## Phase Ensemble Experiment

`scripts/backtest_scorecard_csi_phase_ensemble.py` tests the most direct
response to the user's month-drift requirement: a production portfolio is built
from multiple staggered month-end sleeves instead of a single natural January or
quarter-end cut point. The strict matrix remains 12 external month phases times
4 execution lags.

Result: phase diversification improves the worst return but still does not meet
the target.

- all tested candidates were 0 / 48 strict phase/lag passes.
- the 12-sleeve unlevered cash version lifted worst final capital to about 618w
  and reduced worst drawdown to about -30.6%, better than single-phase annual or
  quarterly drift but still far from target.
- the best return candidate, `phase12_lever120_us10y`, reached worst final
  capital around 964w and median final capital around 1437w, but worst drawdown
  remained around -39.6%.
- adding pre-entry guards for extreme rallies, weak repair, and stagflation-like
  conditions did not solve drawdown; guarded variants still had worst drawdowns
  around -37.6% to -37.9% and lower worst final capital than the best unguarded
  phase ensemble.

This partially validates the random-drift diversification idea but rejects it as
a complete solution. The binding problem is now clear: month-close signal
diversification can raise the return floor, but it cannot keep drawdown under
10% without either much stronger crash prediction, higher-frequency risk control,
or an explicit hedge/defensive asset with reliable full-window coverage.

## Phase Ensemble Target Automation

`scripts/generate_csi_phase_ensemble_targets.py` converts the phase-diversified
research path into concrete production target weights. It uses the previous
month-end scorecard snapshot, blends staggered sleeve snapshots from prior
month-ends, and writes both JSON and CSV outputs under `data/portfolio/`.

For future apply years that are not present in the historical hybrid holdings CSV,
the generator fills the missing sleeve holdings from saved
`csi_annual_recommendation` rows. This makes the production flow compatible with
the existing pipeline order: run `rank_annual_csi.py --save`, then generate
phase-ensemble targets.

The standardized pipeline now runs this target generator after the annual target
generation unless `--skip-phase-ensemble-targets` is passed. The default production
rule is `phase12_lever120_us10y`, with override parameters for rule name,
top-per-sleeve, and current portfolio drawdown.

Validation run for `as_of=2026-07-15`:

- snapshot: `2026-06-30`
- rule: `phase12_lever120_us10y`
- scorecard score/band: `-2`, `机会偏多`
- target equity: `120.0%`
- financing: `20.0%`
- sleeves: 12, with 2026 sleeves sourced from saved annual recommendations and
  2025 sleeves sourced from historical hybrid holdings
- output files:
  - `data/portfolio/csi_phase_ensemble_targets_20260715.json`
  - `data/portfolio/csi_phase_ensemble_targets_20260715.csv`

This is an automation milestone, not a validation pass. Before the modeled
defined-loss overlay, the tested frontier had 0 / 11243 candidates passing the
all-scenario target.

## Defined-Loss Target Automation

`scripts/generate_csi_defined_loss_overlay_targets.py` converts the first
cost-bearing strict-pass modeled rule into production-shape target rows. The
default rule is `defloss_mix95_8_floor010_prem075_up10`, which is the -1.0%
monthly loss floor, 0.75% monthly premium budget, and 10% upside haircut
representative.

The standardized pipeline now runs this target generator after the phase-ensemble
target generator unless `--skip-defined-loss-targets` is passed. It can be
overridden with `--defined-loss-target-rule`,
`--defined-loss-core-drawdown-pct`, and
`--defined-loss-satellite-drawdown-pct`.

Validation run for `as_of=2026-07-15`:

- snapshot: `2026-06-30`
- rule: `defloss_mix95_8_floor010_prem075_up10`
- model status: `cost_boundary_not_execution_validated`
- component budgets: 19.0% CSI phase sleeve, 76.0% QQQ protected option sleeve,
  8.0% satellite safe asset, -3.0% strategy financing
- crypto satellite state: 0.0% risky crypto and 8.0% SHY fallback because cached
  BTC/ETH momentum was not positive on the 2026-07-15 signal date
- defined-loss terms: -1.0% monthly loss floor, 0.75% monthly premium budget,
  10.0% upside haircut
- output files:
  - `data/portfolio/csi_defined_loss_overlay_targets_20260715.json`
  - `data/portfolio/csi_defined_loss_overlay_targets_20260715.csv`

This is now wired into target generation, but it is still not a fully executable
portfolio mandate. The QQQ option sleeve and total-portfolio defined-loss overlay
must be sourced as real broker/option/structured-product instructions before live
adoption.

## Defined-Loss Execution Audit

`scripts/audit_defined_loss_execution_feasibility.py` now checks whether the
modeled defined-loss target has enough local evidence to be treated as executable.
The standardized pipeline runs this audit after defined-loss target generation
unless `--skip-defined-loss-execution-audit` is passed. A stricter future gate is
available through `--require-defined-loss-execution-validated`, which exits
non-zero until real execution evidence is present.

Audit run for `as_of=2026-07-15`:

- target file: `data/portfolio/csi_defined_loss_overlay_targets_20260715.json`
- output file: `data/portfolio/csi_defined_loss_execution_audit_20260715.json`
- status: `not_execution_validated`
- available local evidence: `external_asset_daily` coverage for QQQ, SHY,
  BTC-USD, ETH-USD, VIX/VIX3M/VVIX, and 7 CBOE option-strategy indices; local
  `cboe_vix_daily` has 9,215 rows through 2026-07-14; local
  `us_option_chain_snapshot` has 2026-07-15 QQQ snapshots from Yahoo and CBOE
  delayed quotes
- current target rule: `defloss_spread95call108_mix95_8_floor010_prem075_up0`
- option-chain status: `target_option_executable_bid_ask_available`; CBOE
  delayed quotes map the modeled long put, short put, and short call strikes to
  non-zero bid/ask contracts
- estimated option package: buy `QQQ260731P00705000`, sell
  `QQQ260731P00684000`, and sell `QQQ260731C00775000`; conservative bid/ask net
  debit is about 5,356, or 0.54% of capital, within the 0.75% premium budget
- execution blockers: no broker or structured-product quote proving the -1.0%
  monthly total-portfolio loss floor; no skew, fill/slippage, margin, tax, or
  intramonth mark-to-market validation

This means the numerical target has a modeled strict-pass candidate, but the
production mandate remains blocked on portfolio-level protection sourcing and
execution frictions. The option-sleeve budget blocker has been resolved for the
current put-spread target, but the total portfolio monthly loss floor has not
been proven executable.

## Current Option-Chain Sync

`sql/us_option_chain_snapshot_schema.sql` and
`scripts/import_us_option_chain_snapshot.py` add a current US option-chain
snapshot cache. The importer uses the same local pattern as the other external
data syncs: create schema, fetch with `requests`, upsert rows, and print DB
coverage. The default source is CBOE delayed quotes; Yahoo remains available as
a fallback. This cache is intentionally scoped to current/future execution
evidence; neither endpoint provides a 20-year historical option-chain archive.

The standardized pipeline can refresh this cache with:

```bash
.venv/bin/python scripts/run_csi_research_pipeline.py \
  --sync-us-option-chain \
  --target-as-of 2026-07-15
```

Validation run:

- command: `.venv/bin/python scripts/import_us_option_chain_snapshot.py --provider cboe_delayed_quotes --symbols QQQ --quote-date 2026-07-15 --max-expirations 12`
- CBOE rows written: 3,874
- CBOE coverage: `QQQ`, quote date `2026-07-15`, expirations `2026-07-20`
  through `2026-08-21`
- target sleeve mapping:
  - 98% put target maps to `QQQ260731P00705000`, strike 705, DTE 16, bid/ask
    9.15 / 9.30
  - 108% call target maps to `QQQ260731C00775000`, strike 775, DTE 16, bid/ask
    0.49 / 0.53
- estimated QQQ option package cost: 950,000 underlying notional, buy 13.20
  put contracts and sell 10.56 call contracts on a 100-share equivalent basis;
  conservative bid/ask net debit about 11,758, or 1.18% of the 1,000,000
  portfolio, above the modeled 0.75% monthly premium budget
- audit status for the option sleeve:
  `target_option_executable_bid_ask_available`

This improved the execution evidence from "no option-chain table" to "target
contracts exist in the current chain with non-zero CBOE delayed bid/ask". The
original 98/108 two-leg collar failed the 0.75% premium budget, motivating the
budget-aware put-spread retest below.

## Executable Option Package Search

`scripts/search_executable_option_package_candidates.py` scans cached option-chain
snapshots for concrete collar or put-spread packages that fit the modeled premium
budget. It uses the target's current QQQ notional, put cover, short put if
present, call cover, and 0.75% premium budget, then prices long legs at ask and
short legs at bid.

Validation run:

- command: `.venv/bin/python scripts/search_executable_option_package_candidates.py --target-json data/portfolio/csi_defined_loss_overlay_targets_20260715.json --as-of 2026-07-15 --source cboe_delayed_quotes --top 20`
- output files:
  - `data/portfolio/executable_option_package_search_20260715.json`
  - `data/portfolio/executable_option_package_search_20260715.csv`
- candidates scanned after put-spread support: 4,104
- candidates within 0.75% budget: 2,726
- original modeled 98% put / 108% call package:
  - 2026-08-14 30DTE cost: 1.57% of capital, fails budget
  - 2026-07-31 16DTE cost: 1.18% of capital, fails budget
- current 98/95 put-spread + 108 call target:
  - 2026-08-21 37DTE cost: 0.36% of capital, passes budget
  - 2026-08-14 30DTE cost: 0.47% of capital, passes budget
  - 2026-07-31 16DTE cost: 0.54% of capital, passes budget
- budget-fitting alternatives around 30DTE:
  - 99% put / 102% call, 2026-08-14, net debit 0.71%
  - 98% put / 103% call, 2026-08-14, net debit 0.71%
  - 98% put / 102% call, 2026-08-14, net debit 0.39%

This narrowed the next research step: if live execution must stay within the
0.75% premium budget, the original 98/108 QQQ sleeve is too expensive on the
2026-07-15 CBOE delayed chain. The first simple budget-fitting collars reduced
the upside cap too much, so the next tested structure was a put-spread plus 108%
short call sleeve.

## Listed-Option Replication Stress

`scripts/stress_defined_loss_replication.py` tests a stricter execution question:
given the current target rows and mapped CBOE contracts, can the listed QQQ
option sleeve alone replicate the modeled portfolio-level -1.0% monthly floor?
The stress grid is deliberately mechanical: QQQ shocks from -50% to +30%, CSI
shocks from -40% to +25%, 5% annual financing cost, 5 bps per option leg of
slippage, and a 15% notional margin-reserve estimate.

Validation run:

- command: `.venv/bin/python scripts/stress_defined_loss_replication.py --as-of 2026-07-15`
- output files:
  - `data/portfolio/csi_defined_loss_replication_stress_20260715.json`
  - `data/portfolio/csi_defined_loss_replication_stress_20260715.csv`
- current 98/95 put-spread + 108 call target: 88/238 stress scenarios keep the
  total portfolio return above the -1.0% monthly floor
- worst grid scenario: QQQ -50%, CSI -40%, total return -54.55%
- closest failed grid scenario: QQQ +15%, CSI -40%, total return -1.14%
- cash-pressure estimate: about 149,281, including option net debit, slippage,
  and a 15% notional margin reserve

This is a deliberately conservative replication test, not a historical backtest
replacement. Its conclusion is still important: the listed QQQ option sleeve
solves the premium-budget problem but does not, by itself, prove the modeled
portfolio-level monthly loss floor. The remaining floor must come from a
broker/structured-product wrapper, a different executable replication design, or
a materially different portfolio construction.

`scripts/rank_option_package_stress_candidates.py` applies the same stress grid
to all budget-fitting packages from the executable option search. On the
2026-07-15 CBOE delayed chain, 2,726 packages fit the 0.75% premium budget, but
zero pass the full stress grid. The best budget-fitting package is a 97/91 put
spread + 110 call expiring 2026-07-31, with 91/238 stress scenarios passing and
a -51.83% worst stress return.

`scripts/diagnose_option_package_floor_cost.py` removes the 0.75% premium-budget
constraint and tests whether any listed QQQ package in the current chain can
support the total-portfolio -1.0% monthly floor. The diagnostic includes collars,
put spreads, and pure protective puts. It also supports over-hedging the put leg
to test whether buying extra QQQ puts can cover the CSI core's basis risk. The
same diagnostic now also supports a CSI-linked futures hedge proxy, which directly
targets the basis-risk failure.

Validation runs:

- base command: `.venv/bin/python scripts/diagnose_option_package_floor_cost.py --as-of 2026-07-15 --source cboe_delayed_quotes`
- over-hedge command: `.venv/bin/python scripts/diagnose_option_package_floor_cost.py --as-of 2026-07-15 --source cboe_delayed_quotes --put-cover-multipliers 1,1.5,2,3,4,5 --output-prefix data/portfolio/option_package_floor_cost_overhedge_20260715`
- CSI-hedge command: `.venv/bin/python scripts/diagnose_option_package_floor_cost.py --as-of 2026-07-15 --source cboe_delayed_quotes --put-cover-multipliers 1,1.5,2,3,4,5 --csi-hedge-pcts 0,10,20,23,25,30 --csi-hedge-cost-annual-pct 1.0 --output-prefix data/portfolio/option_package_floor_cost_csihedge_20260715`
- output files:
  - `data/portfolio/option_package_floor_cost_20260715.json`
  - `data/portfolio/option_package_floor_cost_overhedge_20260715.json`
  - `data/portfolio/option_package_floor_cost_csihedge_20260715.json`
- base search: 9,427 candidates, 0 pass the unrestricted stress grid. The best
  stress candidate is a 5 DTE ATM pure put with 146/238 stress scenarios passing,
  -10.11% worst total return, and 0.95% net debit.
- over-hedge search: 56,562 candidates, 0 pass the unrestricted stress grid. The
  best stress candidate is a 5 DTE 97.5/84.8 put spread with no call, 5.0x put
  cover, 220/238 stress scenarios passing, -10.77% worst total return, and 1.52%
  net debit.
- current CSI gross exposure: 22.8%.
- best stress candidate CSI capacity: 0.0% CSI gross, meaning the full current
  CSI core would need to be removed for that listed QQQ package to satisfy the
  floor grid.
- best listed-option CSI-capacity candidate: 2.5% CSI gross, still requiring a
  20.3 percentage-point reduction from the current CSI core exposure.
- critical basis-risk failure: the over-hedged best candidate's worst scenario is
  QQQ 0% and CSI -40%. In that state, QQQ puts do not pay, CSI core losses remain,
  and option premium worsens the total portfolio return.
- CSI-linked hedge search: 339,372 candidates, 331 pass the full stress grid when
  a short CSI-linked futures hedge proxy is allowed. The cheapest full-pass
  candidate is a 5 DTE 97.5/72.3 put spread + 100.0 call, 2.0x put cover, 23.0%
  CSI hedge, -0.13% net debit, and 238/238 stress scenarios passing. The best
  stress candidate is a 5 DTE 97.5 put + 100.0 call, 2.0x put cover, 23.0% CSI
  hedge, -0.12% net debit, and -0.07% worst stress return.

This rejects "buy more listed QQQ puts" as the current execution bridge for the
modeled total-portfolio floor. It also rejects a simple weight-only repair:
reducing CSI exposure enough to make QQQ-listed protection pass the stress grid
would effectively remove the CSI selection card from the portfolio. The first
stress-feasible execution bridge is QQQ listed options plus a CSI-linked hedge.
It is still not production-ready until the CSI-linked hedge is mapped from
continuous futures history to a contract-level execution plan with fills, margin,
roll, and tax assumptions.

`scripts/search_scorecard_csi_defined_loss_csi_hedge.py` performs the next
historical check: it keeps the same modeled monthly -1.0% floor, but subtracts a
CSI-linked hedge return and explicit hedge carry from every historical month
before the modeled floor is applied. This tests whether the stress-feasible
CSI hedge destroys the 20-year compounding target.

- command: `.venv/bin/python scripts/search_scorecard_csi_defined_loss_csi_hedge.py`
- output files:
  - `data/backtests/scorecard_csi_defined_loss_csi_hedge_report.json`
  - `data/backtests/scorecard_csi_defined_loss_csi_hedge_search.csv`
- rules tested: 60
- strict passes: 60
- CFFEX futures data now available in `fut_daily`:
  - `IF.CFX`: 3,941 rows, 2010-04-16 ~ 2026-07-14
  - `IH.CFX`: 2,730 rows, 2015-04-16 ~ 2026-07-14
  - `IC.CFX`: 2,730 rows, 2015-04-16 ~ 2026-07-14
  - `IM.CFX`: 960 rows, 2022-07-22 ~ 2026-07-14
- target bridge case:
  `defloss_csihedge_spread95call108_mix95_8_floor010_prem075_hedge23_cost100`
  passes 48/48 randomized timing cases, with 6,459.7w minimum final capital and
  -5.9% worst drawdown. This assumes a 23% CSI-linked hedge, 0.75% monthly
  premium, and 1.0% annual hedge carry. The hedge return uses `IF.CFX` continuous
  futures when available and falls back to `000300.SH` before IF history starts;
  the median timing case uses 193.5 IF-futures hedge months and 46.5 index
  fallback months.
- more conservative 30% hedge, 0.75% monthly premium, 2.0% annual hedge carry
  still passes 48/48, with 5,537.2w minimum final capital and -6.5% worst drawdown.

This is the first route that has both a full stress-grid execution bridge and a
20-year randomized historical modeled-floor pass after hedge drag. It is still
classified as research evidence, not production-ready evidence, because the
floor remains modeled and the CSI hedge uses continuous-futures history rather
than contract-level IF rolls, basis, margin, fills, and taxes.

The production target generator now emits an `index_futures_hedge` row for this
rule. At the original 100w starting capital, the 23% hedge requires only 23w of
short CSI notional, while one `IF.CFX` contract at the 2026-07-14 close of
4,690 has about 140.7w notional. The generated 2026-07-15 target therefore
rounds the IF hedge to 0 contracts and records the execution note that live use
needs a larger account, an ETF/options substitute, or acceptance of no futures
hedge. This contract-granularity blocker is separate from the historical
backtest pass/fail result.

To test that substitute path, `scripts/import_cn_etf_option_snapshot.py` now
imports Tushare `opt_basic` and `opt_daily` for SSE/SZSE ETF options into
`cn_option_basic` and `cn_option_daily`. The 2026-07-14 snapshot imported 309
contracts in `cn_option_basic` and 1,128 option quotes in `cn_option_daily`,
including `OP510300.SH` and `OP159919.SZ` CSI300 ETF options. The 2026-07-15
target file now also emits a `cn_etf_put_hedge_candidate` row:

- contract: `10011002.SH`, `OP510300.SH` 2026-09 4.70 put
- candidate size at 100w capital: 5 contracts
- protected notional candidate: about 23.5% of capital
- premium candidate: about 0.71% of capital

This candidate fixes the IF contract-granularity problem at the target-sizing
level, but it is not yet validated as equivalent protection. It still needs the
same stress replication, liquidity/fill, and historical option-cost audit before
it can replace the modeled CSI hedge in production readiness.

`scripts/stress_cn_etf_put_hedge_candidate.py` then tests the target-file
candidate against the same QQQ/CSI shock grid. The generated 5-contract
`10011002.SH` candidate passes 229/238 stress scenarios; the worst failure is
QQQ 0% and CSI -5%, with -1.34% total return versus the -1.0% floor. Increasing
that same contract to 6 contracts improves the pass count to 235/238 but raises
premium cost to about 0.86% of capital and still fails the floor.

`scripts/search_cn_etf_put_hedge_stress.py` searches all currently cached
`OP510300.SH` and `OP159919.SZ` put contracts plus contract-count variants.
The 2026-07-15 run finds 448 candidates, 7 full stress-grid passes, but 0 full
passes inside the 0.75% monthly premium budget. The cheapest full-pass candidate
is `10011004.SH` with 5 contracts, 24.5% protected notional, and about 1.26%
premium cost. This means China ETF puts can solve the 100w account granularity
problem, but the current simple long-put substitute does not satisfy the
original modeled cost budget.

`scripts/search_cn_etf_option_package_hedge.py` extends the China ETF option
search to packages: long put, optional lower-strike short put, and optional
short call financing. The 2026-07-15 run finds 35,648 package candidates, 71
full stress-grid passes, and 12 full passes inside the 0.75% monthly premium
budget after including the QQQ package net credit. The cheapest budget-pass
package is:

- buy 5x `10011002.SH` (`OP510300.SH` 2026-09 4.70 put)
- sell 5x `10010995.SH` (`OP510300.SH` 2026-09 4.90 call)
- protected notional: about 23.5% of capital
- China ETF package net debit: about 0.05% of capital
- total package net debit after QQQ package credit: about -0.07% of capital
- stress result: 238/238 pass, worst total return about -0.69%
- short-call +25% CSI stress loss / margin proxy: about 5.73% of capital

This is the first current-snapshot small-account bridge that satisfies the same
stress grid and the modeled premium budget. It is not yet production-ready:
the package search uses option close as the execution price for buys and sells
because bid/ask is not available in Tushare `opt_daily`; the short call creates
margin, assignment/exercise, and upside-loss constraints that require broker
rules and fill auditing; and the 20-year historical backtest has not yet been
re-run with a monthly executable China ETF option package roll.

`scripts/search_scorecard_csi_cn_option_package_history.py` performs the first
historical rolling-shape diagnostic for that package. It rolls the current
`OP510300.SH` package moneyness and net cost monthly against CSI300 returns
across the same 12 month offsets and 4 execution-lag settings:

- raw package payoff without modeled floor:
  - `cnpkg_raw_prem000`: 0/48 pass, 1,984.5w minimum final capital,
    -27.3% worst drawdown
  - `cnpkg_raw_prem075`: 0/48 pass, 332.4w minimum final capital,
    -34.7% worst drawdown
- package payoff plus modeled monthly floor:
  - `cnpkg_floor_prem000`: 48/48 pass, 18,784.5w minimum final capital,
    -3.9% worst drawdown
  - `cnpkg_floor_prem075`: 48/48 pass, 5,884.7w minimum final capital,
    -6.8% worst drawdown

This proves the current option package can be a plausible execution bridge for
the modeled floor budget, but it does not yet prove that the package alone
replaces the modeled floor. Production readiness still requires historical
listed-option chain data or another auditable method for monthly bid/ask,
exercise/assignment, and margin.

`scripts/audit_cn_option_history_coverage.py` audits whether Tushare can support
that next production step directly. A 2015-02 ~ 2026-07 half-year sample with
`--write-cache` finds:

- 23 sampled month-end trade dates
- 20 samples with historical `opt_daily` rows
- 0 samples with matching `cn_option_basic` contract terms for those historical
  option `ts_code`s
- `contract_terms_gap = true`

Additional direct API probes show that `opt_daily` can return expired-contract
prices, but `opt_basic` by `ts_code` or by `opt_code` only returns current active
contract basics.

`scripts/import_sse_option_contract_events.py` adds the first reconstruction path
for that missing contract master. It imports official SSE option contract event
records from `query.sse.com.cn/commonQuery.do` and writes both raw events and a
reconstructed `cn_option_contract_archive` keyed by historical option `ts_code`.
The archive stores strike, call/put, underlying option code, contract unit,
listing date, and maturity date.

Smoke validation:

- command:
  `.venv/bin/python scripts/import_sse_option_contract_events.py --start 2015-02-09 --end 2015-08-31 --security-codes 510050 --event-types new_listing --sleep 0.02 --timeout 12 --retries 1 --continue-on-error`
- archive after smoke run: 422 contracts, list dates from 2015-02-09 and
  maturities through 2016-03-23
- audit command:
  `.venv/bin/python scripts/audit_cn_option_history_coverage.py --start 2015-02 --end 2015-08 --step-months 6 --exchanges SSE --output-prefix data/portfolio/cn_option_history_coverage_audit_sse_archive_smoke_20260715`
- result: 2015-02-27 matched 64/64 historical option prices to archive terms;
  2015-08-31 matched 188/188; `contract_terms_gap = false`
- output files:
  - `data/portfolio/cn_option_history_coverage_audit_sse_archive_smoke_20260715.json`
  - `data/portfolio/cn_option_history_coverage_audit_sse_archive_smoke_20260715.csv`

This changes the blocker from "no historical contract master source" to
"full-history SSE event backfill and execution audit still required." Until the
event archive is extended across the full monthly backtest window, the historical
China ETF option package backtest remains an executable-shape proxy.

The archive has now also been extended for the current small-account bridge's
underlying, `OP510300.SH`:

- imported `510300` SSE new-listing events from 2019-12-23 through 2026-07-14
- events imported without errors: 2,844
- archive coverage: 2,844 `OP510300.SH` contracts, list dates 2019-12-23 through
  2026-07-14, maturities through 2026-12-23
- cached historical `cn_option_daily` sample dates with matched `OP510300.SH`
  archive terms: 11
- matched sample rows by date: 2020-02-28 100, 2020-08-31 130, 2021-02-26
  144, 2021-08-31 94, 2022-02-28 138, 2022-08-31 108, 2023-02-28 146,
  2023-08-31 94, 2024-02-29 48, 2025-02-28 90, 2026-07-14 102

The remaining historical-option work is now narrower: cache denser monthly
`opt_daily` rows, include delist/adjustment events where needed, and run the
actual package-selection/backtest logic against historical listed contracts with
explicit fill, exercise, assignment, and margin assumptions.

The first listed-contract diagnostic is now implemented in
`scripts/search_scorecard_csi_cn_option_package_real_history.py`. Supporting
data updates:

- `scripts/import_fund_daily.py` now supports explicit `--ts-codes`,
  `--start-date`, and `--end-date` imports.
- command:
  `.venv/bin/python scripts/import_fund_daily.py --ts-codes 510300.SH --start-date 20191223 --end-date 20260714`
- result: 1,588 `510300.SH` ETF daily rows, 2019-12-23 through 2026-07-14.
- monthly SSE `opt_daily` cache command:
  `.venv/bin/python scripts/audit_cn_option_history_coverage.py --start 2019-12 --end 2026-07 --step-months 1 --exchanges SSE --write-cache --output-prefix data/portfolio/cn_option_history_coverage_audit_sse_monthly_20260715`
- result: 80 sampled months, 53 with option daily rows and archive contract
  terms.

Real-history listed-contract diagnostic runs now support two modes: `510300_only`
and `switch_50_to_300`. The second mode uses `OP510050.SH` through 2019-12-22
and `OP510300.SH` from 2019-12-23 onward, which better matches actual China ETF
option availability over the backtest window.

Additional 50ETF data updates:

- command:
  `.venv/bin/python scripts/import_fund_daily.py --ts-codes 510050.SH --start-date 20150209 --end-date 20191222`
- result: 1,187 `510050.SH` ETF daily rows, 2015-02-09 through 2019-12-20.
- imported `510050` SSE new-listing events from 2015-02-09 through 2019-12-22.
- archive coverage: 2,116 `OP510050.SH` contracts, list dates 2015-02-09
  through 2019-12-16, maturities through 2020-06-24.
- monthly SSE `opt_daily` cache command:
  `.venv/bin/python scripts/audit_cn_option_history_coverage.py --start 2015-02 --end 2019-11 --step-months 1 --exchanges SSE --write-cache --output-prefix data/portfolio/cn_option_history_coverage_audit_sse_monthly_201502_201911`
- result: 58 sampled months, 58 with option daily rows and archive contract
  terms; `contract_terms_gap = false`.

Switch-mode diagnostic runs:

- misszero command:
  `.venv/bin/python scripts/search_scorecard_csi_cn_option_package_real_history.py --underlying-mode switch_50_to_300 --missing-package-policy zero --max-quote-stale-days 10 --slippage-bps-per-leg 5`
- missproxy command:
  `.venv/bin/python scripts/search_scorecard_csi_cn_option_package_real_history.py --underlying-mode switch_50_to_300 --missing-package-policy proxy --max-quote-stale-days 10 --slippage-bps-per-leg 5`
- output files:
  - `data/backtests/scorecard_csi_cn_option_package_real_history_switch_50_to_300_misszero.json`
  - `data/backtests/scorecard_csi_cn_option_package_real_history_switch_50_to_300_misszero.csv`
  - `data/backtests/scorecard_csi_cn_option_package_real_history_switch_50_to_300_missproxy.json`
  - `data/backtests/scorecard_csi_cn_option_package_real_history_switch_50_to_300_missproxy.csv`
- available quote dates: 117 total, 64 for `OP510050.SH` and 53 for
  `OP510300.SH`.
- used quote dates: 111
- median listed-contract months per 20-year path: 104.5
- median missing package months per 20-year path: 135.5
- misszero, no modeled floor: `cnreal_raw_prem000_misszero` has 0/48 passes,
  3,944.7w minimum final capital, and -32.6% worst drawdown.
- misszero, modeled floor retained: `cnreal_floor_prem000_misszero` has 48/48
  passes, 36,501.7w minimum final capital, and -4.4% worst drawdown.
- missproxy, modeled floor retained: `cnreal_floor_prem000_missproxy` has 48/48
  passes, 20,580.4w minimum final capital, and -3.9% worst drawdown.

Interpretation: the real listed-contract sample is now wired into the strict
48-case matrix, and 50ETF history materially improves listed-contract coverage.
It still covers too little of the 20-year path to prove production readiness.
Without the modeled monthly floor, the available real contracts still do not
satisfy the objective. With the modeled floor retained, the numeric target still
passes, but the floor remains the unproven component.

`scripts/search_scorecard_csi_cn_option_package_real_tipp.py` tests whether a
plain executable TIPP/CPPI wrapper over the raw real listed-contract return
stream can replace the modeled monthly floor.

- command:
  `.venv/bin/python scripts/search_scorecard_csi_cn_option_package_real_tipp.py --underlying-mode switch_50_to_300 --missing-package-policy zero --max-quote-stale-days 10 --slippage-bps-per-leg 5`
- output files:
  - `data/backtests/scorecard_csi_cn_option_package_real_tipp_switch_50_to_300_misszero_report.json`
  - `data/backtests/scorecard_csi_cn_option_package_real_tipp_switch_50_to_300_misszero_search.csv`
- rules tested: 736
- strict passes: 0 / 736
- raw return diagnostic: 48/48 timing cases include at least one raw monthly
  loss of 10% or worse; total such months are 192, with a global worst monthly
  return of -24.94%.
- best candidate with worst drawdown within 10%:
  `cnreal_tipp_f88_m030_x75`, 424.0w minimum final capital, -9.4% worst
  drawdown.
- best candidate with minimum final capital above 4,000w:
  `cnreal_cppi_f82_m020_x125`, 4,881.7w minimum final capital, -31.5% worst
  drawdown.
- highest-return candidate:
  `cnreal_cppi_f82_m030_x300`, 69,578.0w minimum final capital, -66.7% worst
  drawdown.

This rejects the simple monthly TIPP/CPPI repair path. It can cap drawdown only
by cutting exposure so far that the capital target fails; when it preserves the
capital target, drawdown remains far above the 10% limit. The raw monthly loss
diagnostic also shows why: month-end-only exposure control cannot guarantee a
10% drawdown cap when the underlying raw stream has repeated single-month losses
well beyond 10% before the next rebalance.

`scripts/search_scorecard_csi_cn_option_package_real_pre_guard.py` then tests
observable pre-month guards on the same raw stream. Guards can use only
pre-period information: previous raw package return, trailing raw 3M/6M return,
portfolio drawdown, CSI300 6M trend, and VIX where available.

- command:
  `.venv/bin/python scripts/search_scorecard_csi_cn_option_package_real_pre_guard.py --underlying-mode switch_50_to_300 --missing-package-policy zero --max-quote-stale-days 10 --slippage-bps-per-leg 5`
- output files:
  - `data/backtests/scorecard_csi_cn_option_package_real_pre_guard_switch_50_to_300_misszero_report.json`
  - `data/backtests/scorecard_csi_cn_option_package_real_pre_guard_switch_50_to_300_misszero_search.csv`
- rules tested: 612
- strict passes: 0 / 612
- no rule kept worst drawdown within 10%.
- best candidate among rules with minimum final capital above 4,000w:
  `preguard_tr3_02_cap50_lev114`, 4,725.6w minimum final capital, -27.5%
  worst drawdown.
- rules that eliminated median exposure to severe monthly losses had final
  capital far below target; the best such high-return example,
  `preguard_dd12_cap0_lev150`, reached only 606.3w and still had -30.3% worst
  drawdown.

This rejects the first observable pre-month guard set. The failures are now
specific: the guard either triggers too late to stop crash months, or triggers
often enough to kill compounding. Further progress needs either higher-frequency
daily loss control, richer external predictors, or a real quoted floor/structured
overlay rather than another monthly-only guard grid.

`scripts/search_scorecard_csi_cn_option_package_real_daily_stop_proxy.py` tests
that higher-frequency branch as a proxy diagnostic. It keeps the same raw
listed-contract monthly return stream, but uses daily ETF/index prices only to
approximate whether an in-month stop would have triggered before the monthly
loss was realized. It is not a daily option-MTM execution proof.

- command:
  `.venv/bin/python scripts/search_scorecard_csi_cn_option_package_real_daily_stop_proxy.py --underlying-mode switch_50_to_300 --missing-package-policy zero --max-quote-stale-days 10 --slippage-bps-per-leg 5`
- output files:
  - `data/backtests/scorecard_csi_cn_option_package_real_daily_stop_proxy_switch_50_to_300_misszero_report.json`
  - `data/backtests/scorecard_csi_cn_option_package_real_daily_stop_proxy_switch_50_to_300_misszero_search.csv`
- rules tested: 672
- strict passes: 0 / 672
- best candidate among rules with minimum final capital above 4,000w:
  `dstop12_x125_post0_shock10`, 4,797.9w minimum final capital, -49.2%
  worst drawdown.
- best drawdown candidate overall:
  `dstop10_x100_post0_shock10`, 1,880.3w minimum final capital, -35.8%
  worst drawdown.
- severe-loss capture diagnostic: raw monthly losses of 10% or worse total 192
  across timing cases; ETF/index proxy stops at -3% to -6% capture only 96 of
  those severe months, while -10% captures only 60.

This rejects the first daily proxy-stop repair path. The problem is not only
stop severity or leverage sizing; many severe raw package losses are not
preceded by enough ETF/index drawdown to be caught by a simple underlying-price
stop. A production-grade fix likely needs actual daily option package MTM,
explicit quoted/funded floor structure, or new features that predict the option
package tail directly rather than using ETF spot loss as the only trigger.

`scripts/search_scorecard_csi_cn_option_package_daily_mtm_stop.py` then replaces
the ETF proxy trigger with actual daily MTM for the selected listed option legs
where `cn_option_daily` quotes exist. Because the local historical option table
was originally populated mostly from monthly samples, this script is also a data
coverage diagnostic.

- command:
  `.venv/bin/python scripts/search_scorecard_csi_cn_option_package_daily_mtm_stop.py --underlying-mode switch_50_to_300 --missing-package-policy zero --max-quote-stale-days 10 --slippage-bps-per-leg 5`
- output files:
  - `data/backtests/scorecard_csi_cn_option_package_daily_mtm_stop_switch_50_to_300_misszero_report.json`
  - `data/backtests/scorecard_csi_cn_option_package_daily_mtm_stop_switch_50_to_300_misszero_search.csv`
- rules tested: 256
- strict passes: 0 / 256
- current MTM coverage after the 5-date smoke test plus 2,045 additional
  successfully fetched historical trade dates:
  5,304 listed periods, 5,120 with at least one MTM point, median daily MTM
  points 21.0, max daily MTM points 23.
- best candidate among rules with minimum final capital above 4,000w:
  `mtmstop04_x100_post0`, 5,755.7w minimum final capital, -32.6% worst
  drawdown.
- best drawdown candidate overall:
  `mtmstop04_x100_post0`, 5,755.7w minimum final capital, -32.6% worst
  drawdown.
- severe-loss capture diagnostic at a -5% MTM stop: 84 / 192 severe months,
  or 43.8%.

This does not yet reject actual daily option MTM as a concept, because the
current local option history is still too sparse for most months. It does reject
the current partially populated MTM dataset as proof. The new
`scripts/backfill_cn_option_daily_for_mtm.py` script closes this gap by
deriving the trade dates required by the selected option package legs and
backfilling those `opt_daily` snapshots in controlled batches.

- dry-run command:
  `.venv/bin/python scripts/backfill_cn_option_daily_for_mtm.py --underlying-mode switch_50_to_300 --missing-package-policy zero --max-quote-stale-days 10 --slippage-bps-per-leg 5 --dry-run`
- initial dry-run gap: 2,123 missing trade dates from 2015-03-02 to 2026-07-13.
- smoke-test backfill:
  `.venv/bin/python scripts/backfill_cn_option_daily_for_mtm.py --underlying-mode switch_50_to_300 --missing-package-policy zero --max-quote-stale-days 10 --slippage-bps-per-leg 5 --limit 5 --sleep 0.2`
- smoke-test result: five historical trade dates fetched successfully, 64
  option rows each, 320 rows upserted; subsequent dry-run gap fell to 2,118
  missing trade dates.
- follow-on backfills: a 100-date batch fetched 12,320 rows; a larger interrupted
  batch still persisted successful dates and moved the gap to 1,726; a controlled
  100-date batch fetched 9,628 rows; three subsequent 200-date batches fetched
  20,530, 27,446, and 28,954 rows respectively, all with `failed_dates=0`.
  A later 300-date batch fetched 299 dates with one transient 502 failure and
  114,666 rows; the next 300-date batch fetched 108,222 rows with
  `failed_dates=0`. A final wide backfill fetched 354 dates with 73 empty
  trade-date responses and 233,392 rows. The remaining dry-run gap is 146
  missing trade dates from 2023-10-09 to 2025-05-12.

`scripts/diagnose_scorecard_csi_cn_option_daily_mtm_drawdowns.py` diagnoses the
remaining drawdown bottleneck for the best daily-MTM stop rules.

- command:
  `.venv/bin/python scripts/diagnose_scorecard_csi_cn_option_daily_mtm_drawdowns.py --underlying-mode switch_50_to_300 --missing-package-policy zero --max-quote-stale-days 10 --slippage-bps-per-leg 5 --rules mtmstop04_x100_post0 mtmstop04_x300_post0`
- output files:
  - `data/backtests/scorecard_csi_cn_option_package_daily_mtm_drawdown_mtmstop04_x100_post0_mtmstop04_x300_post0.json`
  - `data/backtests/scorecard_csi_cn_option_package_daily_mtm_drawdown_mtmstop04_x100_post0_mtmstop04_x300_post0.csv`
- bottleneck: both rules hit their worst trough in `2008-11-05 ~ 2008-12-03`,
  after the `2008-09-03 ~ 2008-10-08` loss month.
- the trough window has `source=missing_zero` and `daily_points=0`, because
  China ETF listed options did not exist in 2008. Real option-leg MTM therefore
  cannot protect the worst historical drawdown unless the pre-option era uses a
  separate, executable proxy risk-control mechanism.

`scripts/search_scorecard_csi_cn_option_package_hybrid_mtm_proxy_stop.py` tests
that idea by using real option-leg MTM where available and CSI300 daily closes
as the fallback trigger before option MTM exists.

- command:
  `.venv/bin/python scripts/search_scorecard_csi_cn_option_package_hybrid_mtm_proxy_stop.py --underlying-mode switch_50_to_300 --missing-package-policy zero --max-quote-stale-days 10 --slippage-bps-per-leg 5`
- output files:
  - `data/backtests/scorecard_csi_cn_option_package_hybrid_mtm_proxy_stop_switch_50_to_300_misszero.json`
  - `data/backtests/scorecard_csi_cn_option_package_hybrid_mtm_proxy_stop_switch_50_to_300_misszero.csv`
- rules tested: 256
- strict passes: 0 / 256
- best drawdown candidate overall: `mtmstop04_x100_post50`, 1,122.3w minimum
  final capital, -43.2% worst drawdown.
- best candidate above 4,000w: `mtmstop15_x150_post50`, 5,346.2w minimum final
  capital, -74.1% worst drawdown.

This rejects the simple CS300 fallback-trigger repair. It confirms that the
remaining gap is structural: the pre-2015 era needs a better executable
defensive engine than a spot-index stop, while the post-2015 option MTM data is
now mostly dense enough to test that separately.

`scripts/search_scorecard_csi_pre_option_regime_defense.py` then searches the
combined repair that now clears the strict numeric target: listed-option
daily-MTM stops at 2%-4%, observable pre-option CS300 regime caps, and a
pre-option bubble-reversal signal for high-runup/early-rollover months. The
matrix still covers the full 12 month-start phases and 4 execution lags.

- command:
  `.venv/bin/python scripts/search_scorecard_csi_pre_option_regime_defense.py`
- output files:
  - `data/backtests/scorecard_csi_pre_option_regime_defense_switch_50_to_300_misszero_report.json`
  - `data/backtests/scorecard_csi_pre_option_regime_defense_switch_50_to_300_misszero_search.csv`
- rules tested: 5,184
- strict passes: 223 / 5,184
- best drawdown rule:
  `lststop02_preopt_norm100_risk30_cr0_r3n10_r6n18_r12n40_d60n15_d120n15_map00_sig1`,
  5,549.6w minimum final capital, -8.3% worst drawdown.
- best balanced rule:
  `lststop02_preopt_norm114_risk30_cr0_r3n10_r6n18_r12n40_d60n15_d120n15_map00_sig1`,
  6,484.7w minimum final capital, -9.6% worst drawdown.
- key data repair: `HistoricalCnPackagePricer` now searches backward within the
  allowed stale window for the first quote date whose option legs can cover the
  target holding period. This fixed the `2025-03-07 ~ 2025-04-08` case by using
  the `2025-03-03` 6-month 300ETF option legs instead of failing on the nearer
  but incomplete `2025-03-04` snapshot.
- key risk repair: the remaining `2007-12-28 ~ 2008-01-31` failure was an early
  bubble rollover: CS300 12m return still exceeded +100%, while 3m return had
  turned negative. The bubble-reversal signal catches this before the later 2008
  crash regime signal.

`scripts/generate_csi_pre_option_regime_targets.py` converts the strict-pass
rule into future target holdings.

- command:
  `.venv/bin/python scripts/generate_csi_pre_option_regime_targets.py --rule best_balance --as-of 2026-07-15 --capital 1000000`
- output files:
  - `data/portfolio/csi_pre_option_regime_targets_20260715.json`
  - `data/portfolio/csi_pre_option_regime_targets_20260715.csv`
- generated target: 24 rows, net position weight 99.99%, including phase CSI
  core rows, concrete QQQ listed-option sleeve rows, SHY satellite-safe
  allocation, and the current 510300 ETF option package legs.
- QQQ listed sleeve for the 2026-07-15 run: 95.0% QQQ underlying notional,
  `QQQ260821P00705000` long put x14, `QQQ260821P00685000` short put x14,
  `QQQ260821C00775000` short call x11, and -19.3878% option-sleeve financing
  to reconcile the 76.0% modeled sleeve budget.
- CN 510300 ETF option package for the 2026-07-15 run: `10011002.SH` long put
  x5 and `10010995.SH` short call x5, selected from the 2026-07-15 quote date.

Pipeline automation now exposes the backfill and re-test sequence:

- `--backfill-cn-option-daily-mtm`
- `--cn-option-daily-mtm-limit`
- `--cn-option-daily-mtm-sleep`
- `--cn-option-daily-mtm-dry-run`
- `--run-csi-pre-option-regime-defense`
- `--pre-option-regime-target-rule`
- `--skip-pre-option-regime-targets`
- `--run-cn-option-package-daily-mtm-stop`
- `--run-cn-option-package-hybrid-mtm-proxy-stop`
- `--diagnose-cn-option-package-daily-mtm-drawdowns`

## Executable Frontier Diagnostic

`scripts/diagnose_scorecard_csi_executable_frontier.py` consolidates all
non-`defined_loss_overlay` search CSVs and explicitly excludes the modeled
portfolio-floor candidates. This separates the executable portfolio frontier
from the research-only modeled floor frontier.

Validation run:

- command: `.venv/bin/python scripts/diagnose_scorecard_csi_executable_frontier.py`
- output files:
  - `data/backtests/scorecard_csi_executable_frontier_diagnostic.json`
  - `data/backtests/scorecard_csi_executable_frontier_diagnostic.csv`
- executable non-floor candidates scanned: 20,861
- strict passes: 0
- best candidate with worst drawdown at or above -10%: 1,486w minimum final
  capital
- best candidate with minimum final capital at or above 4,000w: -17.4% worst
  drawdown

This is the cleanest current diagnosis of the real optimization gap: without
the modeled portfolio-level floor, the existing features and executable sleeves
do not yet satisfy both sides of the objective. The remaining distance is not a
small threshold tweak; it is either a missing executable protection source or a
missing return source with full-window coverage and low correlation to the CSI
drawdown path.

`scripts/search_scorecard_csi_executable_tail_gap.py` ran a focused 2,304-rule
search around the closest non-floor family, using spread-based QQQ sleeves,
BTC CPPI satellites, and tighter drawdown-triggered TIPP overlays. It did not
improve the frontier:

- output files:
  - `data/backtests/scorecard_csi_executable_tail_gap_search.json`
  - `data/backtests/scorecard_csi_executable_tail_gap_search.csv`
- best minimum final capital in that search: 1,198w, with -17.3% worst drawdown
- best worst drawdown in that search: -14.1%, with only 391w minimum final
  capital

This rejects the most direct "increase satellite return but tighten TIPP"
extension. Further executable progress should prioritize new data/features or
a real structure quote rather than widening this local parameter grid.

`scripts/search_scorecard_csi_option_protection_tipp.py` tested a second
executable route: take the closest CBOE option-strategy sleeve
(`qqq_vxth_30_vix30`) and wrap it with daily TIPP/CPPI exposure control. The
focused 24-rule run also failed to improve the executable frontier:

- output files:
  - `data/backtests/scorecard_csi_option_protection_tipp_report.json`
  - `data/backtests/scorecard_csi_option_protection_tipp_search.csv`
- best minimum final capital: 591w, with -60.0% worst drawdown
- best worst drawdown: -13.7%, with 244w minimum final capital

This rejects the closest current CBOE-protection sleeve as a standalone
execution-ready substitute. The high-growth option-strategy sleeve compounds
well enough before risk control, but the daily TIPP wrapper cuts exposure after
damage has already occurred and does not restore enough return.

`scripts/search_scorecard_csi_cboe_blend.py` tested a third executable route:
blend the CSI phase-ensemble paths directly with priced CBOE option-strategy
sleeves, then apply plain, CPPI, or TIPP portfolio-level exposure control. This
keeps the protection source in historical CBOE index data instead of the
modeled monthly portfolio floor.

- command: `.venv/bin/python scripts/search_scorecard_csi_cboe_blend.py`
- output files:
  - `data/backtests/scorecard_csi_cboe_blend_report.json`
  - `data/backtests/scorecard_csi_cboe_blend_search.csv`
- rules tested: 210
- strict passes: 0
- best minimum final capital:
  `cboeblend_phase12_lever120_us10y_cboe_qqq_vxth_30_vix30_c35_b65_cppi_f86_m080_x150`,
  with 10,250w minimum final capital but -50.6% worst drawdown.
- best worst drawdown:
  `cboeblend_phase12_lever120_us10y_cboe_qqq_pput_40_vix30_c20_b80_tipp_f88_m080_x150`,
  with -12.0% worst drawdown but only 108w minimum final capital.

This rejects the current CSI + CBOE option-index blend as an executable
substitute. It improves the return frontier, but the same tradeoff remains:
rules that clear 4,000w still carry far more than -10% drawdown, while tighter
portfolio insurance destroys the compounding needed for the capital target.

`scripts/search_scorecard_csi_cboe_feature_guard.py` then tested observable
pre-month risk guards on the same executable CSI + CBOE blend family. The guard
signals use only locally cached, pre-execution data: VIX level/percentile,
VIX/VIX3M backwardation, VVIX, QQQ 3-month trend/drawdown, and VXTH 1-month
momentum. When a guard fires, the strategy scales the risky CSI+CBOE blend toward
SHY before the month is held.

- command: `.venv/bin/python scripts/search_scorecard_csi_cboe_feature_guard.py`
- output files:
  - `data/backtests/scorecard_csi_cboe_feature_guard_report.json`
  - `data/backtests/scorecard_csi_cboe_feature_guard_search.csv`
- rules tested: 5,400
- strict passes: 0
- best minimum final capital:
  `cboeguardbase_phase12_lever120_us10y_cboe_qqq_vxth_30_vix30_c35_b65_cppi_f86_m080_x150_term_backward_scale50`,
  with 6,965w minimum final capital but -49.3% worst drawdown.
- best worst drawdown:
  `cboeguardbase_phase12_lever120_us10y_cboe_qqq_vxth_25_vix25_c65_b35_tipp_f88_m080_x150_vix_ge_25_scale25`,
  with -15.1% worst drawdown but only 242w minimum final capital.
- candidates with worst drawdown at or above -10%: 0
- candidates with minimum final capital at or above 4,000w: 120; the best
  drawdown inside that subset was still -37.9%.

This rejects the current pre-month VIX/term-structure/trend feature guard as a
solution. It adds useful negative evidence: observable monthly risk signals on
the priced CBOE sleeve do not close the gap, so further work should shift toward
new return sources, true executable defined-loss terms, or materially different
crisis-alpha data rather than widening this particular signal grid.

Long-history futures and dollar-index series were then added to
`external_asset_daily` through the existing Yahoo chart importer and included in
the default external sync list:

- command:
  `.venv/bin/python scripts/import_external_asset_daily.py --start 2004-01-01 --end 2026-07-15 --sleep 0.1 --timeout 20 --symbols 'GC=F' 'SI=F' 'CL=F' 'NG=F' 'ZB=F' 'ZN=F' 'ZF=F' '6E=F' '6J=F' 'DX-Y.NYB'`
- rows upserted: 56,689
- coverage: all ten symbols cover January 2004 through 2026-07-15.

`scripts/search_scorecard_csi_futures_crisis_alpha.py` tested those series as
long-only and long/short futures trend sleeves blended with the CSI phase
ensemble under monthly TIPP/CPPI sizing.

- output files:
  - `data/backtests/scorecard_csi_futures_crisis_alpha_report.json`
  - `data/backtests/scorecard_csi_futures_crisis_alpha_search.csv`
- rules tested: 1,680
- strict passes: 0
- best minimum final capital:
  `fut_cppi_l120_us10y_commodity_fx_long_relative_c50_f50_fl90_m100_x150`,
  with 1,416w minimum final capital and -46.1% worst drawdown.
- best worst drawdown:
  `fut_tipp_l120_us10y_macro_ls_relative_c35_f65_fl90_m120_x150`,
  with -10.6% worst drawdown but only 231w minimum final capital.
- candidates with worst drawdown at or above -10%: 0
- candidates with minimum final capital at or above 4,000w: 0

This rejects the current continuous-futures trend sleeve as the missing
crisis-alpha source. It did not improve the existing executable frontier, and it
also carries additional production caveats because Yahoo continuous futures do
not model contract rolls, margin, commissions, tax, or slippage.

## Executable-Budget Historical Retest

The 98/103 and 99/102 QQQ collar candidates, plus the 98/95 and 98/94 put-spread
+ 108 call candidates, were added to the modeled option rule set and re-run
through `scripts/backtest_scorecard_csi_defined_loss_overlay.py`. This expands
the defined-loss search from 1,120 to 5,600 rules. After adding the executable
frontier diagnostic, focused tail-gap search, and CBOE option-protection TIPP
probe, the CSI + CBOE blend search, and the CBOE feature-guard search, the
futures crisis-alpha search, and the modeled CSI-hedged defined-loss check, the
consolidated frontier covers 26,521 candidates overall.

Retest output:

- `data/backtests/scorecard_csi_defined_loss_overlay_report.json`
- `data/backtests/scorecard_csi_defined_loss_overlay_search.csv`
- `data/backtests/scorecard_csi_executable_budget_retest_summary.json`

Result:

- original 98/108 core: 146 strict passes; at -1.0% floor and 0.75% premium,
  `defloss_mix95_8_floor010_prem075_up10` still passes 48/48 with 4202w worst
  final capital and -6.1% worst drawdown, but the current CBOE option package is
  over the 0.75% budget.
- executable 98/103 core: 89 strict passes in the abstract search, but at the
  0.75% premium boundary the best -1.0% floor case is only 30/48, with 3482w
  worst final capital. It does not replace the default rule.
- executable 99/102 core: 22 strict passes in the abstract search, but at the
  0.75% premium boundary the best -1.0% floor case is 0/48, with 1896w worst
  final capital. It does not replace the default rule.
- executable 98/95 put-spread + 108 call core: 281 strict passes; at the -1.0%
  floor and 0.75% premium boundary, the best case is
  `defloss_spread95call108_mix95_8_floor010_prem075_up0`, passing 48/48 with
  13,503w worst final capital and -5.4% worst drawdown.
- executable 98/94 put-spread + 108 call core: 265 strict passes; at the -1.0%
  floor and 0.75% premium boundary, the best case is
  `defloss_spread94call108_mix95_8_floor010_prem075_up0`, passing 48/48 with
  11,465w worst final capital and -5.4% worst drawdown.

This rejects the simplest executable-budget collar substitution and promotes the
98/95 put-spread + 108 call structure to the current budget-aware historical
candidate. The target generator now exposes
`defloss_spread95call108_mix95_8_floor010_prem075_up0` as an explicit rule, while
the research pipeline default `--defined-loss-target-rule` now points to that
budget-aware rule. The original 98/108 rule remains available in the target
generator for comparison. This is still not a live adoption approval: the
put-spread makes the listed QQQ option sleeve cheaper, but the portfolio-level
-1.0% monthly floor still needs broker/structured-product or replication
validation, including skew, fills, margin, tax, and intramonth mark-to-market
behavior.

## Daily Guard Experiment

`scripts/backtest_scorecard_csi_daily_guard.py` keeps the phase-diversified sleeve
construction but marks the portfolio daily and applies observable daily guards:
monthly stop-loss cuts, CS300 trailing drawdown cuts, and CS300 moving-average
breaks. Max drawdown in this experiment is measured on the daily equity curve,
which is stricter than the monthly/quarterly validation point series.

Result: simple daily stop and trend guards still do not meet the target.

- all tested candidates were 0 / 48 strict phase/lag passes.
- the unguarded daily-marked `phase12_lever120_us10y` baseline had worst final
  capital around 933w and worst daily max drawdown around -40.6%.
- stop-loss variants triggered frequently but did not protect the full portfolio:
  `daily_stop5_cap0` had worst final capital around 746w and worst drawdown around
  -44.2%; `daily_stop5_cap30` had worst final capital around 791w and worst
  drawdown around -40.0%.
- CS300 trailing-drawdown and MA guards over-traded and worsened the return floor,
  with worst drawdowns still around -45% to -59%.
- lower-risk phase sleeves with daily stop/MA guards reduced some drawdown cases
  but left worst final capital only around 468w-590w and worst drawdown around
  -37% to -42%.
- synthetic inverse-CS300 guard assets were also tested as hedge proxies after
  stop/trend triggers. They made the risk profile worse, not better:
  `daily_stop5_inverse` had worst final capital around 642w and worst drawdown
  around -61.9%, while `daily_stop5_ma60_inverse` collapsed to around 84w worst
  final capital and about -83.5% worst drawdown. This is a hedge-failure case:
  the proxy fires after losses and then whipsaws against recovery.

This rejects another narrow hypothesis: daily mechanical stop-loss and CS300 trend
rules alone are not enough, and naive trigger-then-short-index hedging is actively
harmful in this data. These rules either fire too late after the loss has happened
or fire often enough to destroy the compounding needed for the 4000w target.

## Daily TIPP/CPPI Experiment

`scripts/backtest_scorecard_csi_daily_tipp.py` tests daily portfolio-insurance
wrappers on phase-diversified CSI sleeves. It caches daily base returns for each
phase rule, month phase, and execution lag, then sweeps TIPP/CPPI floors,
multipliers, and max exposure levels. The pipeline can run it with
`--run-daily-tipp`.

Result: daily portfolio insurance on the current CSI sleeve engine does not meet
the target and is weaker than the best monthly blend-TIPP frontier.

- 1308 daily TIPP/CPPI rules were tested, all at 0 / 48 strict passes.
- The best daily result inside the -10% drawdown limit,
  `dtipp_guard60us10y_f90_m10_x100`, reached only about 352w with about -10.0%
  worst drawdown.
- The highest-capital daily CPPI result,
  `dcppi_lever120us10y_f84_m12_x200`, reached about 2030w but drew down about
  -83.0%.
- This rejects the hypothesis that simply moving TIPP from monthly to daily
  solves the gap-risk problem. Daily ratcheting protects drawdown by cutting
  average exposure too aggressively, while CPPI keeps the return engine but
  accepts unacceptable drawdowns.

## Feature Guard Experiment

`scripts/audit_scorecard_csi_crash_features.py` expands the strongest phase
ensemble candidate across the 12 month phases and 4 execution lags, labels large
loss months, and snapshots pre-month features from CSI 300 price action, index
valuation/turnover, and margin balance data.

The audit found several features with some crash association, especially very low
margin balance and margin/turnover spikes, but the single-feature thresholds either
flagged too many months at low precision or captured too little of the tail loss.

`scripts/backtest_scorecard_csi_feature_guard.py` then tested those feature
thresholds as pre-month caps on the phase-diversified sleeves.

Result: feature guards improved some return floors but still did not meet the
strict target.

- all tested candidates were 0 / 48 strict phase/lag passes.
- the best worst-case return candidate, `margin_low_cap0`, reached worst final
  capital around 1453w and median final capital around 2095w, but worst drawdown
  remained around -39.6%.
- broader combined feature guards such as `feature_any_cap40` also improved the
  worst final capital versus the unguarded phase ensemble, but still bottomed near
  1251w with the same roughly -39.6% worst drawdown.
- the lower-risk `mean_us10y_feature_any_cap60` reduced worst drawdown to about
  -33.2%, but worst final capital dropped to around 810w.

This rejects another narrow hypothesis: currently available pre-month price,
valuation, turnover, and margin features can help rank risk months, but they are
not strong enough as standalone guards to satisfy both 4000w final capital and
10% max drawdown across randomized rebalance months.

## Cross-Asset Momentum Probe

A temporary cached probe tested whether long-sample external assets could solve the
target through monthly trend selection and volatility budgeting. The candidate
universe used only assets with near-full 2006-2025 coverage in the local database:
CSI 300, CSI 500, SSE 50, S&P 500, Nasdaq, and the synthetic US 10Y duration proxy.

Result: simple cross-asset momentum improves the return side, but the drawdown
tradeoff is still far outside the target.

- high-risk variants can clear the 4000w final-capital floor in every month/lag
  case; the best return probe reached about 6062w worst final capital and 8749w
  median final capital.
- those same high-return variants had unacceptable worst drawdowns, roughly -47%
  to -56%.
- the lowest-drawdown representative variants still had worst drawdowns around
  -19.7%, and their worst final capital was only around 390w-532w.

This rejects a broad but still mechanical hypothesis: adding US equity indices and
a US 10Y proxy to a monthly momentum/volatility-budget allocator does not meet the
4000w/10% all-scenario target. The return target requires aggressive risk, while
the current observable risk controls cannot keep that risk inside the drawdown
constraint.

## External Asset Rotation Experiment

`sql/external_asset_daily_schema.sql` and `scripts/import_external_asset_daily.py`
add a cached external ETF/index price layer using Yahoo chart data. The first
default universe is SPY, QQQ, TLT, IEF, SHY, GLD, UUP, DBC, and VIX. This gives
near-full coverage for SPY/QQQ/TLT/IEF/SHY/VIX from 2004 onward, GLD from
2004-11, DBC from 2006-02, and UUP from 2007-03.

`scripts/backtest_scorecard_csi_external_rotation.py` tests whether these assets
can form a high-return, low-drawdown hedge sleeve before being wired into the
scorecard+CSI production portfolio. The experiment uses the same 12 month phases
and 4 execution lags as the strict drift matrix, with VIX and SPY 3M momentum as
risk-off guards.

Result: this broader external asset layer still does not meet the target.

- all tested candidates were 0 / 48 strict phase/lag passes.
- the highest-return tested rule, `rot_aggressive_vix30`, reached worst final
  capital around 3054w and median final capital around 3994w, but worst drawdown
  was about -52.2%.
- the lower-risk rules reduced drawdown only to around -17% to -21%, while worst
  final capital fell to about 222w-563w.
- intermediate rules such as `rot_growth_vix30` reached around 2166w worst final
  capital with about -43.3% worst drawdown.

This confirms the same binding constraint with a stronger external data layer:
available long-sample ETFs and VIX guards improve the research surface, but simple
monthly rotation still cannot deliver 20%+ annualized compounding with sub-10%
drawdown across all rebalance-month drift cases.

## External Daily Risk Experiment

`scripts/backtest_scorecard_csi_external_daily_risk.py` tests the next stricter
risk-control hypothesis: daily VIX, QQQ trend, realized-volatility sizing, and
daily defensive rotation across the cached external asset layer. The script keeps
the same 12 month-phase starts and 4 execution lags, but marks and reallocates the
portfolio daily using only prior-day observable data.

Result: daily controls reduce some drawdowns versus aggressive monthly rotation,
but still do not meet the target.

- all tested candidates were 0 / 48 strict phase/lag passes.
- higher-return daily rules still had unacceptable drawdown: `daily_q_vix30_tv30`
  reached only about 955w worst final capital with about -46.6% worst drawdown.
- the best lower-drawdown rules stayed above the 10% drawdown target: `daily_lowdd_vix25`
  had about 567w worst final capital and about -14.9% worst drawdown; `daily_lowdd_vix20`
  had about 356w worst final capital and about -16.1% worst drawdown.
- stop-based versions reduced risk by exiting for long stretches, but destroyed
  compounding: `daily_stop8_tv25` bottomed near 118w worst final capital with
  about -15.2% worst drawdown.

This rejects a stronger mechanical protection hypothesis: simple daily VIX/trend
switches and volatility sizing on liquid ETF proxies are not enough to deliver
4000w final capital with sub-10% max drawdown. The next candidate needs either a
different source of alpha, a true convex/option-like hedge with priced costs, or a
materially better early-warning signal than price/VIX trend states alone.

## CBOE Option Protection Experiment

`scripts/import_cboe_option_indices.py` imports CBOE official option-strategy and
volatility index histories into `external_asset_daily`. The first cached set
includes PPUT, PUT, BXM, BXMD, CLLZ, VXTH, VPD, VVIX, and VIX3M. These are useful
because their histories include option roll costs; they are better evidence than a
hand-made zero-cost hedge proxy.

A quick standalone audit showed that these option-strategy indices are not
standalone answers:

- PPUT had about 4.4x-4.7x 2006-2025 buy-and-hold growth across start months, with
  about -42% worst drawdown.
- VXTH had about 6.2x-6.7x growth, with about -43% worst drawdown.
- BXMD and CLLZ also stayed in low-single-digit multiples with drawdowns around
  -47%.

`scripts/backtest_scorecard_csi_option_protection.py` then tested QQQ/SPY growth
sleeves with CBOE option-strategy protection sleeves, using the same 12 month
phases and 4 execution lags.

Result: priced option-strategy protection improved the return side but still did
not meet the strict drawdown target.

- all tested candidates were 0 / 48 strict phase/lag passes.
- the strongest tested return candidate, `qqq_vxth_30_vix30`, reached about 3691w
  worst final capital and about 4067w median final capital, but worst drawdown was
  still about -45.7%.
- lighter VXTH protection variants such as `qqq_vxth_15_vix25` and
  `qqq_vxth_25_vix25` kept worst final capital around 1973w-2054w with drawdowns
  around -38% to -42%.
- stop-based variants reduced return sharply and still left drawdowns around
  -36% to -40%.

This rejects the first priced-convexity proxy as currently configured. It is the
closest return-side experiment so far, but it still does not solve the binding
sub-10% drawdown requirement. Future work needs either true option-chain based
defined-loss protection, a different crisis-alpha sleeve, or an earlier risk
signal that reduces exposure before the large drawdown rather than after it.

## Synthetic Defined-Loss Option Experiment

`scripts/backtest_scorecard_csi_synthetic_option_hedge.py` tests monthly reset,
daily-marked option packages on QQQ/SPY using Black-Scholes prices with VIX/VIX3M
as implied-volatility proxies. This is not executable option-chain evidence: it
does not include listed strike availability, bid/ask, skew, open interest, or
broker execution terms. It is useful as a defined-loss feasibility screen before
spending effort on real option-chain ingestion.

Result: modelled option structures got closer to the target, but still did not
pass.

- all tested candidates were 0 / 48 strict phase/lag passes.
- weekly and biweekly resets did not improve the drawdown frontier; they raised
  option turnover cost and, in the tested structures, worsened the worst drawdown.
  For example `qqq_w_put98_call108_lev125` had about 2743w worst final capital and
  about -48.7% worst drawdown, while `qqq_bi_put98_call108_lev125` had about 2485w
  worst final capital and about -40.4% worst drawdown.
- high-return collars such as `qqq_put95_call108_lev150` reached about 8051w worst
  final capital, but still had about -35.9% worst drawdown.
- the best risk/return compromise in this pass was `qqq_put98_call108_lev125`:
  about 3344w worst final capital, about 4255w median final capital, and about
  -21.5% worst drawdown.
- low-leverage ATM put structures lowered drawdown versus aggressive leverage but
  did not meet either side of the target: `qqq_put100_lev100` had about 823w worst
  final capital and about -25.1% worst drawdown.
- put-spread structures recovered return but reopened downside too much; for
  example `qqq_put95_85spread_lev150` reached about 3949w worst final capital but
  had about -58.4% worst drawdown.

This is the strongest evidence so far that true defined-loss construction may be
directionally useful, but the current monthly-reset model still cannot satisfy the
strict target. The remaining gap is large: protection needs to be more tightly
specified with real option-chain terms or a different payoff shape, or the return
engine must improve so the strategy does not need as much leverage to reach 4000w.

## Blended CSI + Option Protection Experiment

`scripts/backtest_scorecard_csi_blended_protection.py` tests a portfolio-level
blend of:

- phase-diversified CSI scorecard sleeves from
  `scripts/backtest_scorecard_csi_phase_ensemble.py`
- synthetic QQQ option-protected sleeves from
  `scripts/backtest_scorecard_csi_synthetic_option_hedge.py`
- residual cash / short-duration financing

The experiment uses the same 12 month-phase starts and 4 execution lags. It also
adds drawdown, VIX, CS300 trend, QQQ trend, and hard stop-to-cash variants. The
implementation precomputes monthly CSI and option paths first, then searches
blend weights, so it can be rerun through `scripts/run_csi_research_pipeline.py`
with `--run-blended-protection`.

Result: blending did not solve the strict target. The latest run tested 360
blend/guard combinations, all at 0 / 48 strict passes.

- High-return blends easily exceeded the 4000w capital target but retained large
  drawdowns. The best capital floor was the 100% `qqq_put95_call115_lev250`
  synthetic sleeve, at about 16945w worst final capital, but with about -69.1%
  worst drawdown.
- The best blend that kept worst final capital above 4000w while minimizing
  drawdown was `blend_phase12_lever120_us10y_qqq_put98_call112_lev220_c35_o65`:
  about 5385w worst final capital, about 9594w median final capital, and about
  -30.6% worst drawdown.
- The best near-low-drawdown macro guard still missed both targets:
  `macroguard_phase12_lever120_us10y_qqq_put98_call108_lev125_c20_o80` had
  about 1370w worst final capital and about -11.8% worst drawdown.
- The only tested rules below a 10% drawdown were hard stop-to-cash rules, but
  they destroyed compounding. `hardstop5_phase12_*_qqq_put100_lev125_c00_o100`
  had about 134w worst final capital and about -9.8% worst drawdown.

This confirms that simple portfolio blending and trailing portfolio stops are not
enough. The remaining bottleneck is structural: the target needs a return engine
that reaches roughly 20%+ annualized without relying on drawdown-heavy leverage,
or an option/hedge engine with materially better convexity than the VIX-proxy
monthly packages tested here.

## Monthly CSI Selector Experiment

`scripts/backtest_scorecard_csi_monthly_selector.py` tests whether the CSI
selection scorecard can be made less calendar-fragile by moving from annual
holdings to monthly ex-ante ranking. Each month it ranks available CSI indices
using price features known at the snapshot:

- 12-month momentum
- blended 6/12-month momentum
- momentum/quality
- lower-volatility quality
- breakout-style momentum

The portfolio still uses the scorecard risk budget, optional trend/drawdown caps,
and the same strict 12 month-phase starts times 4 execution lags. Before enough
CSI cross-section history exists, it falls back to CSI300, so the reported
`median_fallback_months` is also a data-depth diagnostic. It can be run from the
pipeline with `--run-monthly-csi-selector`.

Result: monthly price-feature CSI selection does not improve the target frontier.
The latest run tested 135 selector/risk-budget combinations, all at 0 / 48 strict
passes.

- Best capital-floor result:
  `lowvol_quality_top5_lev150_trend_guard` reached only about 476w worst final
  capital and about 834w median final capital, with about -71.9% worst drawdown.
- Best drawdown result:
  `lowvol_quality_top20_lev100_risk_guard_us10y` still had about -40.2% worst
  drawdown, with only about 282w worst final capital.
- No monthly selector variant came close to the 4000w / -10% target. The median
  fallback period was about 55.5 months, which confirms that CSI index data depth
  still constrains the 20-year objective.

This rejects a simple "rebalance CSI more often with price momentum" fix. The
next return-engine work needs richer pre-trade features, stronger data backfill,
or a fundamentally different alpha source; monthly price-only CSI rotation is
not sufficient.

## CPPI / TIPP Portfolio Insurance Experiment

`scripts/backtest_scorecard_csi_cppi_protection.py` tests daily portfolio
insurance on cached external assets. This is a different risk structure from
monthly stops or option collars:

- `TIPP` uses a trailing peak floor, so it is the relevant variant for controlling
  realized maximum drawdown.
- `CPPI` uses an initial capital floor, so it is mainly a return-side comparison;
  it does not protect the portfolio from peak-to-trough drawdown.

The script uses daily `external_asset_daily` prices for QQQ, SPY, GLD, TLT, IEF,
SHY, and VIX. It runs the same 12 start-month phases and 4 execution lags, and is
available from the pipeline with `--run-cppi-protection`.

Result: portfolio insurance cleanly shows the return/drawdown tradeoff, but still
does not meet the strict target. The latest run tested 188 rules, all at 0 / 48
strict passes.

- Best capital-floor result:
  `cppi_qqq_top1_f90_m8_l20` reached about 1897w worst final capital and about
  2002w median final capital, but maximum drawdown was about -63.9%.
- Best sub-10% drawdown result:
  `tipp_all_mom_top2_f92_m8_l20_vix30` reached only about 265w worst final
  capital and about 273w median final capital, with about -9.9% worst drawdown.
- No CPPI/TIPP rule reached the 4000w capital floor. TIPP can enforce the
  drawdown side, but the required exposure budget is too small to compound to the
  return target; CPPI restores exposure but loses drawdown control.

This rejects "daily portfolio insurance on liquid proxies" as a complete answer.
It is useful as a risk-control primitive, but not a standalone return engine for
the requested objective.

## TIPP / CPPI Option Overlay Experiment

`scripts/backtest_scorecard_csi_tipp_option_overlay.py` tests the direct
combination of the two strongest but incomplete ideas:

- synthetic option-protected sleeves can reach or exceed the 4000w capital floor,
  but with unacceptable drawdowns;
- TIPP/CPPI portfolio insurance can constrain drawdown, but on ordinary liquid
  proxy returns it cannot compound enough.

This experiment uses the same modelled Black-Scholes/VIX option proxy as
`scripts/backtest_scorecard_csi_synthetic_option_hedge.py`, then allocates to
that sleeve with a trailing floor. Residual capital stays in SHY. It is available
from the pipeline with `--run-tipp-option-overlay`.

Result: the combined structure also does not meet the strict target. The latest
run tested 362 rules, all at 0 / 48 strict passes. The expanded run explicitly
added the lower-drawdown standalone option sleeve `qqq_put98_call108_lev125`,
which had previously been missing from the TIPP/CPPI overlay search.

- Best capital-floor result:
  `cppiopt_qqq_put98_call112_lev220_f90_m6_x150` reached about 23026w worst final
  capital, but maximum drawdown was about -56.0%.
- Best drawdown result:
  `tippopt_qqq_put100_lev125_f95_m2_x50` held drawdown to about -6.0%, but reached
  only about 174w worst final capital.
- Best at or inside the -10% drawdown target:
  `tippopt_qqq_put98_call108_lev125_f92_m8_x150` reached about 516w worst final
  capital with about -9.9% drawdown.
- Best with worst final capital above 4000w and the lowest drawdown:
  `cppiopt_qqq_put98_call112_lev220_f90_m3_x75` reached about 4315w but still
  drew down about -31.8%.

This rejects "use trailing-floor sizing on the current synthetic option sleeve"
as a complete solution. It improves the diagnostic frontier but still preserves
the same structural break: drawdown-controlled variants do not compound, and
compounding variants still require a drawdown budget far above 10%.

## Walk-Forward Crash Feature Guard Experiment

`scripts/backtest_scorecard_csi_walkforward_crash_guard.py` turns the crash-feature
audit into an ex-ante annual walk-forward risk model. For each year, it trains only
on prior-year monthly snapshots from `scorecard_csi_crash_feature_rows.csv`, builds
a simple standardized bad-vs-ok feature score, and caps the phase-ensemble equity
exposure when the current month sits in the high-risk tail of the prior training
distribution. It is available from the pipeline with `--run-walkforward-crash-guard`.

Result: the processed feature model does not meet the target. The latest run tested
18 feature/cap/cooldown rules, all at 0 / 48 strict passes.

- Best capital-floor result:
  `wf_turnover_q80_cap40` reached about 1132w worst final capital and about 1627w
  median final capital, but maximum drawdown was about -41.6%.
- Best drawdown result:
  `wf_margin_q85_cap60` still had about -39.2% worst drawdown, with only about
  790w worst final capital.
- No walk-forward feature model came close to the -10% drawdown limit.

This rejects the first simple processed-feature classifier as a complete answer.
The current price, valuation, turnover, and margin features can change exposure
timing, but they do not identify the drawdown-driving months early enough to meet
the strict all-phase target.

## FRED Macro Risk Guard Experiment

`scripts/import_fred_macro_series.py` imports selected FRED macro and credit-risk
series into `external_asset_daily` as `FRED:<series_id>` symbols. The first cached
set covers financial conditions, rates, dollar strength, and credit spreads:

- `NFCI`, `ANFCI`: 2004-01-02 to 2026-07-03
- `DFF`: 2004-01-01 to 2026-07-13
- `DGS10`, `DGS2`: 2004-01-02 to 2026-07-13
- `DTWEXBGS`: 2006-01-02 to 2026-07-10
- `BAMLH0A0HYM2`, `BAMLC0A0CM`: only 2023-07-17 onward from the current FRED CSV
  response, so these credit-spread fields cannot explain the full 20-year window

`scripts/backtest_scorecard_csi_macro_risk_guard.py` then tests pre-month macro
guards on the phase-ensemble strategy. It is available from the pipeline with
`--sync-fred-macro` and `--run-macro-risk-guard`.

Result: the first FRED macro guard set also does not meet the strict target. The
latest run tested 13 rules, all at 0 / 48 strict passes.

- Best capital-floor result:
  `macro_credit_stress_cap40` and related credit-stress rules effectively behaved
  like the ungated phase ensemble in most of the sample because full-window credit
  spread coverage is missing; worst final capital stayed around 964w with about
  -39.6% drawdown.
- Best drawdown result:
  `macro_mean_any_cap60` reduced worst drawdown to about -33.2%, but worst final
  capital was only about 700w.
- No FRED macro guard came close to the -10% drawdown limit.

This rejects the first simple macro-state guard as a complete answer. FRED
financial-conditions and rates features are useful context, but in this form they
do not fire early or specifically enough to protect the scorecard+CSI sleeve.

## Oracle Upper-Bound Diagnostic

`scripts/diagnose_scorecard_csi_oracle_upper_bound.py` is a non-investable
lookahead diagnostic. It uses future monthly returns from
`scorecard_csi_crash_feature_rows.csv` to answer a feasibility question: if a
perfect risk model knew the next month's loss in advance, could the current
scorecard+CSI return engine meet the all-phase target?

This output is deliberately not named `scorecard_csi_*_search.csv`, so
`scripts/summarize_scorecard_csi_frontier.py` does not treat it as a valid
candidate strategy. It is available from the pipeline with
`--run-oracle-upper-bound`.

Result: the current return engine is theoretically powerful enough, but only with
an unrealistically high-recall risk oracle.

- Perfectly avoiding every negative-return month:
  `oracle_avoid_negative_cap0` passed 48 / 48 strict cases, with about 75586w
  minimum final capital and about -9.4% worst drawdown.
- Avoiding only months below -2%:
  `oracle_avoid_loss2_cap0` passed 36 / 48 cases, with about 52151w minimum final
  capital but about -11.5% worst drawdown.
- Avoiding only months below -4%:
  `oracle_avoid_loss4_cap0` passed 24 / 48 cases, with about 28528w minimum final
  capital and about -11.5% worst drawdown.
- Avoiding only months below -8% left drawdown near -20.9%, even though the
  minimum final capital stayed above 4000w.

This changes the research direction. The current phase-ensemble return engine can
clear the target if risk control has very high recall on ordinary negative months,
not only crash months. The failed feature/macro guards were too sparse or too late.
Next work should focus on dense, high-recall one-month loss prediction or a
replacement alpha source that does not require catching roughly every negative
month.

## Boosted Walk-Forward Loss Guard Experiment

`scripts/backtest_scorecard_csi_boosted_loss_guard.py` tests a stronger
walk-forward ordinary-loss classifier. It trains a small AdaBoost-style ensemble
of one-feature threshold stumps using only prior-year snapshots, then applies the
trained score to the same 12 month phases and 4 execution lags. The feature sets
include local scorecard/CSI features, cached external market and macro features,
combined features, and a risk-market subset. The pipeline can run it with
`--run-boosted-loss-guard`; failures are allowed so the search evidence remains
available for frontier consolidation.

Result: the boosted model improves the search breadth but still does not meet the
target.

- 544 boosted guard rules were tested, all at 0 / 48 strict passes.
- The best boosted result,
  `boost_loss1p0_external_e24_q55_cap0`, reached about 1698w worst final capital
  with about -38.1% worst drawdown.
- No boosted rule reached 4000w minimum final capital, and no boosted rule had
  worst drawdown inside -10%.
- This rejects the hypothesis that a denser nonlinear classifier over the
  currently cached local/external month-start features can approximate the oracle
  ordinary-loss guard closely enough.

## Calendar / Phase Loss Guard Experiment

`scripts/backtest_scorecard_csi_calendar_loss_guard.py` tests whether the drift
failure has a stable, ex-ante timing signature. It trains only on prior-year
snapshots, then caps exposure using historical loss statistics by calendar month,
month plus rebalance phase, and month plus execution lag. The pipeline can run it
with `--run-calendar-loss-guard`.

Result: calendar and phase priors are too weak to approximate the oracle ordinary
loss guard. The focused run tested 378 rules, all at 0 / 48 strict passes.

- Best capital candidate:
  `cal_loss2_month_n8_r45_cap60` reached about 953w worst final capital, but
  worst drawdown remained about -39.6%.
- Best drawdown candidate:
  `cal_neg_month_n32_r45_cap20` reduced worst drawdown only to about -33.9%, with
  about 492w worst final capital.
- Highest median ordinary-loss recall:
  `cal_neg_month_lag_n8_r45_cap60` reached only about 24.0% recall, with about
  564w worst final capital and about -36.4% worst drawdown.

This rejects the hypothesis that the randomized month-drift failures can be fixed
with simple seasonal or rebalance-phase priors. The ordinary loss months are not
clustered tightly enough in these known timing buckets.

## External Data Refresh

`scripts/import_us_treasury.py` now supports targeted refreshes with:

- `--apis`
- `--start-year`
- `--end-year`
- `--sleep`
- `--timeout`
- `--skip-rebuild-snapshots`

The current DB state after targeted refresh:

- `us_tycr_daily`: 5127 rows, 2006-01-03 to 2026-06-25
- `us_trycr_daily`: 4111 rows, 2010-01-04 to 2026-06-12
- `us_tbr_daily`, `us_tltr_daily`, `us_trltr_daily`: no returned rows from the
  current Tushare route
- `macro_annual_snapshot`: rebuilt with US nominal/real 10Y and
  `global_rate_stance` for the scorecard window
- `external_asset_daily`: added via `scripts/import_external_asset_daily.py`, with
  cached SPY, QQQ, TLT, IEF, SHY, GLD, UUP, DBC, and VIX daily prices through the
  latest Yahoo chart response
- CBOE option-strategy and volatility indices are also cached into
  `external_asset_daily` via `scripts/import_cboe_option_indices.py`: PPUT, PUT,
  BXM, BXMD, CLLZ, VXTH, VPD, VVIX, and VIX3M
- FRED macro/credit-risk series are cached via
  `scripts/import_fred_macro_series.py`; financial conditions and rates have
  full-window coverage, while the current FRED CSV response for the selected ICE
  credit-spread series starts only in 2023
- BTC-USD and ETH-USD were cached through the same Yahoo chart route for a
  feasibility test of high-growth external alpha. BTC coverage starts on
  2014-09-17 and ETH coverage starts on 2017-11-09, so neither is a full-window
  CSI replacement source.
- `scripts/diagnose_scorecard_csi_oracle_upper_bound.py` is wired as a
  non-investable pipeline diagnostic via `--run-oracle-upper-bound`

## Frontier Summary Automation

`scripts/summarize_scorecard_csi_frontier.py` consolidates all
`data/backtests/scorecard_csi_*_search.csv` experiment outputs into a normalized
frontier report:

- `data/backtests/scorecard_csi_frontier_summary.json`
- `data/backtests/scorecard_csi_frontier_summary.csv`
- `docs/design/scorecard_csi_frontier_summary.md`

The pipeline can run this before final strict QA with `--summarize-csi-frontier`,
so failed research runs still leave a machine-readable diagnosis instead of only a
raised exception.

Latest generated summary:

- 6995 candidates were consolidated across the strict-search experiments.
- 0 candidates satisfied the all-scenario 4000w / -10% target.
- Best capital floor:
  `crypto_tipp_overlay/cryptocppi_crypto_top2_f90_m60_x100` reached about
  29763w, but with about -86.1% worst drawdown.
- Best drawdown candidate:
  `blend_tipp_overlay/btipp_l120_us10y_qqq_put98_call108_lev125_c20_o80_f95_m15_x35`
  held worst drawdown to about -0.6%, but reached only about 179w worst final
  capital.
- Best candidate with final capital above 4000w and lowest drawdown:
  `blend_tipp_expanded/xbcppi_l120_us10y_qqq_put98_call108_lev125_c20_o80_f86_m080_x125`
  reached about 4788w, but still had about -21.3% worst drawdown.
- Best candidate at or inside the -10% drawdown limit:
  `blend_tipp_expanded/xbtipp_l120_us10y_qqq_put98_call108_lev125_c20_o80_f88_m080_x100`
  reached only about 1003w.

Expanded monthly TIPP/CPPI pressure grid:

- `scripts/backtest_scorecard_csi_blend_tipp_expanded.py` formalizes the wider
  monthly drift grid over existing blended CSI + synthetic-option sleeves.
- 738 expanded candidates were tested across all 12 month phases and 4 execution
  lags.
- 0 candidates satisfied the all-scenario target.
- Best candidate inside the -10% drawdown limit improved to about 1003w, with
  about -9.7% worst drawdown.
- Best candidate inside a looser -12% drawdown band reached about 1370w, with
  about -11.8% worst drawdown.
- Best capital candidate reached about 24467w, but its worst drawdown was about
  -62.3%.

Extended external asset cache:

- `scripts/import_external_asset_daily.py` now includes additional long-sample
  Yahoo symbols in its default sync list: EFA, EEM, IWM, XLK, XLE, XLU, HYG, LQD,
  AGG, and TIP.
- The July 15, 2026 sync upserted 55838 rows for those symbols.
- EFA, EEM, IWM, XLK, XLE, XLU, LQD, AGG, and TIP covered 2004-01-02 through
  2026-07-13; HYG covered 2007-04-11 through 2026-07-13.
- SH, PSQ, and RWM were added as inverse-ETF hedge candidates. The July 15, 2026
  sync upserted 14986 rows; SH and PSQ covered 2006-06-21 through 2026-07-13,
  while RWM covered 2007-01-25 through 2026-07-13.

Cross-asset ETF TIPP experiment:

- `scripts/backtest_scorecard_csi_cross_asset_tipp.py` tests the new external
  ETF cache as a separate cross-asset momentum/defense sleeve blended with
  scorecard+CSI sleeves.
- 1200 candidates were tested across all 12 month phases and 4 execution lags,
  including the inverse-ETF defensive sleeve.
- 0 candidates satisfied the all-scenario target.
- Best candidate inside the -10% drawdown limit reached only about 364w, with
  about -10.0% worst drawdown.
- Best candidate inside a looser -12% drawdown band reached about 411w, with
  about -11.9% worst drawdown.
- Best capital candidate reached about 1160w, but its worst drawdown was about
  -43.1%.
- This confirms that the newly cached liquid ETF rotation and inverse-ETF hedge
  set improves data breadth, but does not provide enough independent alpha or
  convexity to close the current 4000w / -10% target gap.

Cross-asset trend-following TIPP experiment:

- `scripts/backtest_scorecard_csi_trend_follow_tipp.py` tests cross-asset
  long-only and synthetic long/short trend-following sleeves across SPY, QQQ,
  IWM, EFA, EEM, sector ETFs, treasury/bond ETFs, GLD, DBC, UUP, TIP, and VIX.
- 2688 candidates were tested across all 12 month phases and 4 execution lags.
- 0 candidates satisfied the all-scenario target.
- Best capital candidate
  `tf_cppi_l120_us10y_macro_long_relative_c50_t50_f86_m080_x150` reached about
  2432w worst final capital and 3963w median final capital, but its worst
  drawdown was about -36.7%.
- Best candidate inside the -10% drawdown limit reached only about 380w, with
  about -9.8% worst drawdown.
- Best candidate inside a looser -12% drawdown band reached only about 436w,
  with about -11.8% worst drawdown.
- This rejects the first crisis-alpha sleeve hypothesis in the cached ETF layer:
  long/short time-series momentum adds another independent research surface, but
  it still preserves the same tradeoff between return and drawdown.

Crypto satellite mix experiment:

- `scripts/backtest_scorecard_csi_crypto_satellite_mix.py` tests whether a small
  BTC/crypto CPPI satellite can lift the strongest low-drawdown CSI + synthetic
  option core without breaking the same 12 month-phase and 4 execution-lag
  validation matrix.
- 1560 candidates were tested, all at 0 / 48 strict passes.
- The best candidate inside the -10% drawdown limit improved the frontier to
  about 1486w worst final capital, with about -9.8% worst drawdown:
  `satmix_tipp_core_xbcppi_sub12_sat_btc_cppi_c90_s10_f88_m080_x125_dd100s100`.
- The best candidate inside a looser -12% drawdown band reached about 2162w,
  with about -11.7% worst drawdown.
- The lowest-drawdown candidate that cleared 4000w was the plain 95% core / 8%
  crypto mix, around 4003w worst final capital but still about -17.4% worst
  drawdown.
- This is a real frontier improvement versus the previous sub-10% result around
  1003w, but it still rejects the hypothesis that a small high-growth satellite
  can close the full 4000w / -10% gap.

Modeled defined-loss overlay experiment:

- `scripts/backtest_scorecard_csi_defined_loss_overlay.py` tests a monthly
  defined-loss overlay on the best CSI + crypto satellite mix. The overlay is
  modeled as a direct monthly loss floor after explicit monthly premium and
  upside haircut.
- 1120 cost-boundary candidates were tested across all 12 month phases and 4
  execution lags.
- 146 candidates satisfied the strict modeled target.
- The highest-return zero-cost boundary case,
  `defloss_mix95_8_floor010_prem000_up0`, reached about 21931w worst final
  capital with about -4.6% worst drawdown.
- A stricter cost-bearing representative,
  `defloss_mix95_8_floor010_prem075_up10`, assumes a -1.0% monthly loss floor,
  0.75% monthly premium, and 10% upside haircut; it still passed all 48 cases
  with about 4202w worst final capital and about -6.1% worst drawdown.
- This identifies the first modeled path that clears the user's numerical
  target. It does not complete the production objective by itself: the monthly
  loss floor still needs to be mapped to executable option/structured-product
  terms and verified against actual strike availability, skew, bid/ask, margin,
  taxes, and intramonth mark-to-market.

Expanded external ETF feature guard:

- `scripts/backtest_scorecard_csi_external_feature_guard_expanded.py` tests a
  focused full-window loss-month guard over the expanded ETF feature set.
- 64 focused rules were tested across the external and risk-market feature
  groups, 1%-2% negative-month labels, 50%-80% score quantiles, and 0%-60% caps.
- 0 candidates satisfied the all-scenario target.
- Best capital candidate `xext_loss2p0_external_q70_cap0` reached about 2301w,
  with about -38.1% worst drawdown.
- The newly added ETF features improved the return frontier versus the earlier
  external full-window guard, but still did not capture ordinary loss months
  densely enough to reduce the drawdown plateau.

This makes the current binding constraint explicit: the tested protection layers
can lower drawdown or preserve compounding, but not both at the target level.
Further progress likely needs a new alpha/hedge data source with full-window
coverage, not another small cap/stop variant on the same scorecard feature set.

## Implication

The current long-only scorecard plus CSI selector is not yet generalized enough for
the requested target. The next useful work is not minor threshold tuning. It needs a
broader risk/return design, likely including:

- a cached, faster drift-validation engine suitable for daily automation
- date-aware CSI selection that does not depend on fixed Jan 1 annual recommendations
- stronger regime/risk gates for summer-start portfolios
- additional defensive or hedge features beyond cash-only de-risking
- phase-diversified or rolling recommendation construction, so production holdings
  are not dominated by a single calendar cut point; current evidence says this
  helps the return floor but does not solve drawdown by itself
- higher-frequency risk controls or hedge proxies that can cut single-month loss
  events before they compound into 30%-40% portfolio drawdowns; simple daily stop
  and CS300 MA/trailing-drawdown guards, including naive inverse-CS300 guard
  proxies, have now been tested and rejected
- more than daily TIPP/CPPI wrappers on phase-diversified CSI sleeves; the best
  sub-10% drawdown result reached only about 352w, while the highest-capital
  daily CPPI result reached about 2030w with about -83.0% drawdown
- stronger predictive or tradable hedge inputs than the currently available
  pre-month price, valuation, turnover, and margin snapshots
- more than simple long-only cross-asset momentum; the first long-sample
  US/CN/macro proxy test reached the return target only with 40%-50% drawdowns
- more than monthly external ETF/index rotation with VIX guards; the first cached
  SPY/QQQ/TLT/IEF/SHY/GLD/DBC/UUP/VIX experiment still had 0 / 48 strict passes
- more than daily VIX/trend/volatility controls on those same liquid proxies; the
  first formal daily experiment also had 0 / 48 strict passes
- more than currently tested CBOE option-strategy protection sleeves; they improve
  return-side evidence but still leave roughly 36%-46% drawdowns
- more than a first-pass synthetic monthly option model; its best compromise still
  leaves about -21.5% drawdown and misses the 4000w worst-case capital floor
- more than simple blending of CSI phase sleeves with synthetic option-protected
  QQQ sleeves; the best >4000w blend still had about -30.6% drawdown, while the
  only sub-10% hard-stop rules compounded to only about 134w worst final capital
- more than portfolio-level TIPP/CPPI sizing over the strongest blended sleeves;
  the best sub-10% result improved to about 767w with about -8.2% worst drawdown,
  while the best >4000w result still drew down about -25.3%
- more than the expanded monthly TIPP/CPPI pressure grid over blended CSI plus
  synthetic-option sleeves; its best sub-10% result improved to about 1003w with
  about -9.7% worst drawdown, while the highest-capital variant still had about
  -62.3% worst drawdown
- more than cross-asset ETF momentum/defense sleeves using the extended
  SPY/QQQ/global/sector/bond/gold/commodity/inverse-ETF cache; its best sub-10%
  result reached only about 364w, the best sub-12% result reached about 411w,
  and the highest-capital variant reached about 1160w with about -43.1% worst
  drawdown
- more than cross-asset long/short trend-following sleeves on the extended ETF
  cache; the best capital variant reached about 2432w but still drew down about
  -36.7%, while the best sub-10% drawdown variant reached only about 380w
- more than a small BTC/crypto CPPI satellite on the strongest low-drawdown
  CSI + synthetic-option core; it improved the sub-10% frontier to about 1486w,
  but the best 4000w candidate still drew down about -17.4%
- more than a purely modeled monthly defined-loss overlay; the first modeled
  pass requires translating a -1.0% monthly loss floor, explicit premium, and
  upside haircut into executable protection terms before it can be used as a
  real production holding process
- more than expanded external ETF feature loss-month guards; the best focused
  rule reached about 2301w but still drew down about -38.1%, with 0 / 48 strict
  passes
- more than daily TIPP/CPPI sizing over cached BTC/ETH external alpha; the best
  sub-10% crypto result reached only about 311w with about -9.6% worst drawdown,
  while the highest-capital crypto CPPI result reached about 29763w but drew down
  about -86.1%
- more than monthly CSI price-feature rotation; the first monthly selector search
  had 0 / 48 strict passes, a best capital floor around 476w with -71.9% drawdown,
  and a best drawdown still near -40.2%
- more than daily CPPI/TIPP portfolio insurance on liquid external proxies; CPPI
  reached only about 1897w while drawing down -63.9%, and the best sub-10% TIPP
  variant reached only about 265w
- more than TIPP/CPPI sizing over the current synthetic option-protected sleeves,
  including the lower-drawdown `qqq_put98_call108_lev125` sleeve; the best
  sub-10% result reached only about 516w, while the best >4000w result still
  drew down about -31.8%
- more than the first walk-forward crash-risk feature model; the best result reached
  only about 1132w while still drawing down about -41.6%
- more than the first FRED macro-risk guard; the best drawdown result still drew
  down about -33.2%, and credit-spread coverage from the current CSV route is too
  short for the full 20-year target
- the oracle upper-bound says the current return engine can meet the target only
  if the risk model catches nearly all negative months; catching only larger loss
  months is not enough to hold drawdown below 10%
- more than the first walk-forward ordinary-negative-month feature model; the
  best result reached only about 1099w, still with about -38.1% worst drawdown,
  and the strongest tested recall profile remained far below the oracle-like
  near-complete negative-month capture required by the target
- more than a nonlinear threshold-stump negative-month model on the same
  observable month-start features; its best result reached about 1356w, still
  with about -38.1% worst drawdown, so simple feature interactions do not solve
  the loss-month detection problem
- more than the first external full-window feature negative-month model using
  cached SPY, QQQ, TLT, IEF, SHY, GLD, CBOE option-strategy indices, VIX, and
  FRED macro/rate features; its best result,
  `ext_loss2p0_combined_q70_cap0`, reached about 1830w with about -38.1% worst
  drawdown and 0 / 48 strict passes, so the currently cached external features
  also do not yet provide the required dense loss-month guard
- more than a boosted walk-forward ordinary-loss classifier over local, external,
  combined, and risk-market month-start features; the best result reached about
  1698w with about -38.1% worst drawdown and 0 / 48 strict passes
- more than simple walk-forward calendar/month/phase loss priors; the best capital
  result reached about 953w with about -39.6% drawdown, the best drawdown result
  still drew down about -33.9%, and median ordinary-loss recall topped out around
  24.0%
- automated frontier summarization after experiments, because the current
  executable-feature frontier had 0 / 11243 candidates passing before the
  modeled defined-loss overlay, and the full modeled frontier must distinguish
  passable cost-boundary assumptions from implementable holdings
- explicit validation against the strict all-scenario target before production adoption
