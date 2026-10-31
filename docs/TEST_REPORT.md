# Test report

Measured on 30 Oct 2026. Environment: Python 3.13.18, PostgreSQL 16.15, Flask 3.1.3, SQLAlchemy 2.0.44, psycopg 3.2.10,
Linux x86-64, 2 vCPU, 7 GB RAM. (The prompt targets Python 3.12; the code uses no 3.13-only features.)

```
pytest                       # fresh database 'billright_test', migrated, both fixtures loaded through the import path
83 passed, 0 failed, 0 skipped in 183.2 s
```

The suite creates a new database, applies the Alembic migrations, loads Fixture A (12 synthetic customers, 2 weekly
periods, 148,660 completed picking lines, 190 invoice lines) and Fixture B (BSP worked example, illustrative) through
the normal import path, runs every period, and then runs the tests below. All values in this report are the values the
tests assert with exact `Decimal` equality.

## 1. Acceptance criteria (section 12.2)

### Fixture A — GiNi synthetic billing dataset (rule tolerances $0.00 and 0%)

| Criterion | Measured | Result | Test |
|---|---|---|---|
| Exactly 3 leakage items, nothing else above tolerance | 3 items, total gap **$357.64** | Pass | `test_fixture_a::test_exactly_three_items_total_357_64` |
| CUST-02 · ORDER_FEE · 2026-W38 · L2 | billable 246 × $4.3695 = $1,074.90; billed 221 × $4.3695 = $965.66; gap $109.24 | Pass | same |
| CUST-07 · COLD_CHAIN_SURCHARGE · 2026-W38 · L1 | billable 345 × $0.40 = $138.00; billed $0.00 | Pass | same |
| CUST-07 · COLD_CHAIN_SURCHARGE · 2026-W37 · L1 | billable 276 × $0.40 = $110.40; billed $0.00 | Pass | same |
| Billed total $226,753.95, line by line against the reference sheet (192 lines) | $226,753.95; all 192 billed quantities and amounts equal | Pass | `test_billable_and_billed_totals_and_line_by_line` |
| Billable total $227,111.59, line by line | 192/192 billable quantities equal; 190/192 billable amounts equal; total **$227,111.61** | Pass with deviation D1 | same |
| No unrated or unresolved events | 0 cannot-rate items; 12 customers resolved | Pass | `test_nothing_unrated_or_unresolved` |
| Reference sheet never read by application code | `grep` over `billright/` finds no reference | Pass | `test_reference_sheet_never_read_by_application_code` |
| CLIENTID never used to identify the customer | all 148,660 lines carry CLIENTID `SYN`, yet 12 distinct customers are rated (resolved through task → order line → order → customer master) | Pass | `test_clientid_is_not_used_for_customers` |

**Deviation D1.** The reference sheet rounded two lines with binary floating point: 150 × $2.4219 = $363.285 → $363.28
and 125 × $2.3598 = $294.975 → $294.97. Principle P2 requires `Decimal` with ROUND_HALF_UP once per invoice line,
which gives $363.29 and $294.98, so BillRight's billable total is $0.02 higher than the reference. Quantities and unit
prices on those two invoice lines agree with the contract, so no rule L1–L7 applies; the reconciliation matrix shows
both lines as "Rounding only" and no leakage item is raised. The test asserts both facts: the reference sheet total is
$227,111.59 and BillRight's is $227,111.61, and the only differing lines are these two.

### Fixture B — BSP worked example (illustrative)

| Criterion | Measured | Result | Test |
|---|---|---|---|
| Expense ledger $256,000 across 12 categories | operators $80,000; forklift $16,000; supervisors $16,000; managers $10,000; administration $8,000; carriers $60,000; rent $30,000; utilities $6,000; equipment $7,000; packaging $9,000; software $4,000; other $10,000 | Pass | `test_fixture_b::test_expense_ledger_and_payment_recipients` |
| Payment recipients reconcile | direct payroll $106,000 (operator payroll $56,000 + other roles $50,000); staffing agency $24,000; carriers $60,000; facility/equipment/IT/other $66,000; total $256,000; check passes; agency labor stored once | Pass | same |
| Supervisor pool by documented time 40/35/25 | RetailCo $6,400 · FoodCo $5,600 · PartsCo $4,000 | Pass | `test_allocation` |
| Allocated cost per customer | RetailCo $119,650 · FoodCo $76,950 · PartsCo $59,400; every pool reconciles; Σ $256,000; unassigned $0 | Pass | `test_allocation` |
| RetailCo invoice rated from its rate card | receiving 1,000 × $12 = $12,000; storage 3,000 × $12 = $36,000; picking 12,000 × $3 = $36,000; packing 10,000 × $2 = $20,000; transport $30,000 at cost + 10% = $33,000; special projects 200 h × $40 = $8,000; total $145,000 | Pass | `test_retailco_invoice_rated_from_rate_card` |
| Customer P&L | RetailCo $145,000 / $119,650 / $25,350 / 17.5%; FoodCo $90,000 / $76,950 / $13,050 / 14.5%; PartsCo $72,000 / $59,400 / $12,600 / 17.5%; site $307,000 / $256,000 / $51,000 / 16.6% | Pass | `test_customer_pnl` |
| $12,000 unallocated central overhead | profit before financing and tax $39,000 | Pass | `test_z_lifecycle::test_central_overhead_profit_before_financing_and_tax` |
| Operator OP001 | 160 h at $30 → RetailCo 90 h $2,700; FoodCo 50 h $1,500; PartsCo 20 h $600; total $4,800 | Pass | `test_fixture_b::test_labor_op001` |
| OP001 with 20 training hours | 20 h ($600) unassigned with reason "training"; not spread by completed picks | Pass | `test_z_lifecycle::test_training_hours_stay_unassigned` |
| FoodCo lowest margin; insight lists only data-supported drivers | chart title "FoodCo has the lowest margin: 14.5%"; panel compares 14.5% with the site's 16.6%; no measured driver is worse than the site, so "Why" reads "Cause not established from the data."; "What to check" names task mix, labor cost per transaction, freight exposure, storage use and rate-card terms | Pass | `test_foodco_lowest_margin_with_data_supported_insight` |

### General

| Criterion | Measured | Result | Test |
|---|---|---|---|
| Re-running a closed period gives identical results | the verify run's SHA-256 over all result tables equals the close run's; the closed period rejects writes and new imports |  Pass | `test_z_lifecycle::test_close_freeze_verify_and_reopen` |
| Every leakage item opens to its events, invoice lines and labor evidence | all 3 Fixture A items: rated events with order, task, timestamp; invoice lines (or "no invoice line found"); labor minutes from device work intervals | Pass | `test_s_workflow::test_every_item_opens_to_events_invoices_and_labor` |
| Site/customer scope on pages, exports and API (per endpoint) | a user scoped to CUST-01 sees only CUST-01 rows on every page, API endpoint and export, and gets 403/404 on other customers' items, evidence and drill-downs; another tenant's ids return 403/404; with the app role, RLS returns 0 rows of the other tenant and rejects writes into it | Pass | `test_n_security` (4 tests) |
| Rate edit cannot be approved by its author; approved change re-prices open periods only | author's approval refused (segregation of duties); a rate dated into a closed period is refused; after approval the previous version is superseded from the new valid-from date, and a verify run of the closed period still matches its close run | Pass | `test_z_lifecycle::test_rate_approval_rules_open_periods_only` |
| Unresolved customer record excluded and queued | record with an unknown customer key lands in the Unresolved queue, not in billing or P&L | Pass | `test_u_imports::test_unresolved_customer_is_excluded_and_queued` |
| KPI tile = table = export (metrics service, HTML, API, Excel) | Customers, Costs, Leakage review and Billing reconciliation pages | Pass | `test_m_consistency` (4 tests) |
| Chart rules | H1, L1, C1, C2, C3, K1: finding title, axis titles with units, tooltips, "View data" table, hidden with a one-line reason when data is insufficient | Pass | `test_m_consistency::test_charts_rules_and_empty_states` |
| 3 months of pilot history in under 15 minutes (proposed target) | 994,631 events, 10.2 minutes | Pass | `scripts/perf_pilot.py` |

## 2. Other automated tests

| Area | Tests | Result |
|---|---|---|
| Engine unit tests: money, rate selection, effective dating, conditions, UOM, five pricing methods, tiers, minimum, L1–L7, precedence, tolerance, rounding, allocation, labor, ledger, resolution, storage | `unit/test_engine.py` (26) | 26 pass |
| Workflow: decisions and reasons, approval threshold and segregation of duties, back-billing window, carry to next cycle, adjustment export, recovery, over-billing credit, manual entries | `test_s_workflow.py` (5) | 5 pass |
| Security: roles per page (5 roles), account manager sees no worker detail, CSRF, development login off outside development, audit log and snapshots append-only | `test_n_security.py` (12) | 12 pass |
| UI smoke: every page for every role renders, detail pages, mobile work-capture markup, empty states that say what to do | `test_p_ui_smoke.py` (9) | 9 pass |
| Imports: identical file is a no-op, validation report with rejected rows, unresolved customer queue | `test_u_imports.py` (2) | 2 pass |
| Lifecycle: overhead, training hours, close/verify/reopen, rate approval, reopen needs a reason | `test_z_lifecycle.py` (5) | 5 pass |
| GA features G1, G3–G8 (G2 is covered by the mobile UI smoke test) | `test_zz_ga.py` (7) | 7 pass |

## 3. Performance (3 months of pilot history)

`scripts/perf_pilot.py` builds 13 weekly periods (about 3 months) of pilot history by replaying Fixture A with
shifted dates: **994,631 activity events**, loaded through the normal import path into a fresh tenant, then rated,
reconciled and checked by rule for every period. Measured on the environment above (result in `docs/perf_result.json`):

| Step | Seconds |
|---|---|
| Tenant set-up | 1.9 |
| Imports (13 weeks of GiNi extracts and invoices) | 251.6 |
| Calculation runs (13 periods, 25.7–31.1 s each) | 356.4 |
| **Total** | **609.7 s = 10.2 minutes** |

Target (proposed in the prompt): under 15 minutes. **Met.** An earlier measurement before the join-key indexes of
migration 0003 took 14.8 minutes.

## 4. Not covered by automated tests

* The OIDC sign-in flow is implemented (authorization code with PKCE, JWKS-verified ID token, state and nonce) but
  was not exercised against a live identity provider here; the development login is tested, and a test proves it is
  off outside development.
* The camera path of mobile capture (G2) uses the browser's barcode detector where available; it was checked by
  rendering at phone width, not by an automated camera test. Typed or scanner-keyboard entry is tested.
* G5 (minimums and tiers checker) runs and is tested, but neither fixture bills a minimum or a tier on an invoice, so
  its "finding" path is proven only by the engine unit tests.
