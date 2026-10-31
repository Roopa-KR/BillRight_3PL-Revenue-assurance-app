# Requirements traceability

Status: **Met** = implemented and proven by an automated test; **Met (manual)** = implemented and checked by
rendering / inspection, no automated assertion; **Partial** = implemented with a stated limitation; **Deviation** =
implemented differently from the letter of the prompt, for the reason given. Test paths are relative to `tests/`.
(UT = `unit/test_engine.py`; A = `acceptance/`.)

## Principles

| ID | Code | Proof | Status |
|---|---|---|---|
| P1 Deterministic calculation | `engine/*` (pure Python, no AI); `services/run.py::result_hash` | A/test_z_lifecycle `test_close_freeze_verify_and_reopen` (identical hash on re-run) | Met |
| P2 Exact money | `engine/money.py` (`D` rejects float, `round_cents` HALF_UP once, `largest_remainder`); NUMERIC(18,6)/(18,2) columns | UT `test_float_rejected`, `test_round_once_half_up`, `test_largest_remainder_adds_up`, `test_billable_rounds_once_and_minimum` | Met — see Deviation D1 (reference sheet rounding) |
| P3 Traceability | `metrics.evidence`, `recon_cell`, `allocation_cell`; leakage_evidence table | A/test_s_workflow `test_every_item_opens_to_events_invoices_and_labor`; UI smoke detail pages | Met |
| P4 Human decision | `services/leakage.py`, `services/contracts.py::approve`, `services/admin.py::approve_driver` | A/test_s_workflow `test_decision_rules`; A/test_z_lifecycle `test_rate_approval_rules_open_periods_only` | Met |
| P5 Unknown is not zero | cannot_rate_item, unresolved queue, unassigned cost, `margin_pct=None`, "Not available" when no expense data | UT `test_cannot_rate_reasons`, `test_no_driver_stays_unassigned_never_equal_split`; A/test_u_imports `test_unresolved_customer_is_excluded_and_queued` | Met |
| P6 Effective dating | `RateSpec.covers` (rate window ∩ version window); `load_rates` | UT `test_rate_selected_by_service_date_not_today` | Met |
| P7 Immutable history | import_batch + source_snapshot (append-only triggers), supersede not overwrite, close freeze, reopen versions | A/test_n_security `test_audit_log_and_snapshots_append_only`; A/test_u_imports; A/test_z_lifecycle | Met |
| P8 One source per metric | `metrics/service.py` used by pages, API (`api/v1.py`) and exports (`services/exports.py`) | A/test_m_consistency (4 tests: tiles = table = API = Excel) | Met |
| P9 Over-billing shown | L7, credit decisions | A/test_s_workflow `test_over_billing_flagged_as_credit`; UT `test_L2_undercount_and_L7_overbilling` | Met |
| P10 Honest labelling | data_label on site/batches/drivers → header badge, exports; "Proxy: completed-line share" tags | A/test_p_ui_smoke (badge on every page); A/test_m_consistency (export label) | Met |
| P11 Tenant and customer isolation | PostgreSQL RLS (migration 0002) + `security.Scope` in every metrics query | A/test_n_security (pages, API, exports, DB-level RLS, cross-tenant) | Met |

## Features

| ID | Code | Proof | Status |
|---|---|---|---|
| F1 Customer and contract register | `ingest` customer master handler, `services/contracts.py` (create/update contract with versions), Contracts page | A/test_fixture_a (12 customers), A/test_zz_ga `test_g6_contract_drift` (contract update) | Met |
| F2 Rate-card editor with approval | `services/contracts.py` (validate, save, submit, approve/reject, diff, repricing_impact), `version.html` | A/test_z_lifecycle `test_rate_approval_rules_open_periods_only` | Met |
| F3 Billable event capture | importer handlers (TI102C, TI202, orders, storage), `services/work.py` | A/test_fixture_a; A/test_s_workflow `test_manual_entry_workflow` | Met |
| F4 Rating engine (5 methods) | `engine/rating.py`, `engine/billable.py` | UT rating tests incl. `test_cost_plus_and_pass_through`, `test_fixed_fee_once_per_period`, `test_tiers_explicit_methods` | Met |
| F5 Invoice import and reconciliation | `invoice_lines` source, `engine/leakage.reconcile`, Reconciliation page | A/test_fixture_a line-by-line | Met |
| F6 Rules L1–L7 | `engine/leakage.py` | UT L1–L7 tests, precedence, tolerance | Met |
| F7 Review queue and workflow | `services/leakage.py`, Leakage pages | A/test_s_workflow | Met |
| F8 Evidence drill-down | `metrics.evidence` (events, orders, tasks, timestamps, labor minutes, invoice lines) | A/test_s_workflow `test_every_item_opens…` | Met |
| F9 Cost-to-serve and P&L | `engine/allocation.py`, `engine/pnl.py`, Customers pages | A/test_fixture_b | Met |
| F10 Period close and reports | `services/close.py`, `services/exports.py` | A/test_z_lifecycle; A/test_m_consistency (exports) | Met |
| F11 Security and audit | OIDC (`web/auth.py`), roles (`security.py`), RLS, CSRF, append-only audit | A/test_n_security | Met — OIDC flow not exercised against a live IdP (L2) |
| F12 Expense ledger and payment recipients | vendor/ledger sources, `engine/pnl.ledger_by_recipient`, Costs page | A/test_fixture_b `test_expense_ledger_and_payment_recipients` | Met |
| F13 Cost allocation ledger | `engine/allocation.py`, cost_allocation table, allocation matrix | A/test_fixture_b `test_allocation`; UT allocation tests | Met |
| G1 Storage billing engine | `engine/storage.py`, `ga/features.generate_storage_events` | UT `test_storage_quantities_grace_and_split`; A/test_zz_ga `test_g1_…` | Met (policy per site; see L4) |
| G2 Mobile capture | Work capture page (mobile-first, barcode/QR via scanner or camera, photo) | A/test_p_ui_smoke `test_mobile_work_capture_markup`; screenshot at 390 px | Met (manual for the camera path) |
| G3 Daily leakage alerts | `ga/features.daily_run`, `cli nightly/scheduler`, Alerts page | A/test_zz_ga `test_g3_…` | Met |
| G4 Surcharge automation | `ga/features` surcharge rules applied in the run; L4 detection | A/test_zz_ga `test_g4_…` | Met |
| G5 Minimums and tier checker | `ga/features.minimums_tiers` | A/test_zz_ga `test_g5_…` (empty for Fixture A); UT tiers/minimum | Partial — no fixture has minimums/tiers billed on invoices |
| G6 Contract drift monitor | `ga/features.contract_drift`, `drift_alerts` | A/test_zz_ga `test_g6_…` | Met |
| G7 SLA penalty tracker | `ga/features.sla_exposure` | A/test_zz_ga `test_g7_…` | Met |
| G8 Accounting export connector | `ga/features` adapter interface + `FileAdapter` | A/test_zz_ga `test_g8_…` | Met (file adapter; others are extension points) |

## Calculations, workflows, pages, charts, exports

| ID | Code | Proof | Status |
|---|---|---|---|
| 7.1 Customer resolution | `engine/resolution.py`, setting `customer_resolution.join_path` | UT `test_join_path_and_ambiguity_clientid_never_used`; A/test_fixture_a `test_clientid_is_not_used_for_customers` | Met |
| 7.2 Rate selection | `engine/rating.select_rate` | UT rate-selection tests | Met |
| 7.3 Billable amount | `engine/billable.py` | UT, A/test_fixture_b `test_retailco_invoice_rated_from_rate_card` | Met |
| 7.4 Leakage | `engine/leakage.py` | UT, A/test_fixture_a | Met |
| 7.5 Expense ledger reconciliation | `engine/pnl.ledger_by_recipient`; close check 4 | UT `test_ledger_reconciliation_and_pnl`; A/test_fixture_b | Met |
| 7.6 Cost allocation | `engine/allocation.py` | UT, A/test_fixture_b | Met |
| 7.7 Labor attribution | `engine/labor.py`, `metrics.labor_attribution` | UT `test_labor_op001`; A/test_fixture_b `test_labor_op001`; A/test_z_lifecycle `test_training_hours_stay_unassigned` | Met |
| 7.8 Customer P&L | `engine/pnl.customer_pnl` | A/test_fixture_b `test_customer_pnl`, `test_central_overhead…` | Met |
| 7.9 Operational metrics | `metrics.period_leakage_metrics`, `pilot_metrics` | A/test_s_workflow `test_carry_and_adjustment_export_and_recovery` (confirmed, invoiced, recovery, precision) | Met |
| 8.1 Rate card change | `services/contracts.py` | A/test_z_lifecycle | Met |
| 8.2 Calculation run | `services/run.py` (advisory lock, keep decisions, resolved by data) | A/test_s_workflow `test_over_billing…` (decisions survive re-run) | Met |
| 8.3 Leakage review | `services/leakage.py` | A/test_s_workflow `test_decision_rules` | Met |
| 8.4 Manual billable entry | `services/work.py` | A/test_s_workflow `test_manual_entry_workflow` | Met |
| 8.5 Allocation | drivers + quantities + exceptions | UT; A/test_fixture_b | Met |
| 8.6 Period close | `services/close.py` | A/test_z_lifecycle | Met |
| 9.3 Pages (Home ×4, Leakage, item, Reconciliation, Customers, Customer, Costs, Contracts, Work capture, Close, Data, Admin) | `web/pages.py`, templates | A/test_p_ui_smoke; screenshots | Met |
| Charts H1, L1, C1, C2, C3, K1 | `web/charts.py` | A/test_m_consistency `test_charts_rules_and_empty_states` | Met |
| Exports 1–7 | `services/exports.py` | A/test_m_consistency, A/test_s_workflow (adjustment CSV), A/test_n_security (permissions) | Met |
| 9.5 Insight panels | `metrics.margin_insight`, pool-exception panel, item sentence | A/test_fixture_b `test_foodco_lowest_margin_with_data_supported_insight` | Met |

## Acceptance tests (12.2)

| Criterion | Test | Result |
|---|---|---|
| Fixture A: exactly 3 items, $357.64 | A/test_fixture_a `test_exactly_three_items_total_357_64` | Pass |
| Fixture A: billable $227,111.59 / billed $226,753.95 line by line | A/test_fixture_a `test_billable_and_billed_totals_and_line_by_line` | Pass with Deviation D1 (billable $227,111.61) |
| Fixture B: ledger $256,000 (12 categories) | A/test_fixture_b | Pass |
| Fixture B: payment recipients | A/test_fixture_b | Pass |
| Supervisor 40/35/25 | A/test_fixture_b `test_allocation`, UT | Pass |
| Allocated cost per customer | A/test_fixture_b | Pass |
| RetailCo invoice $145,000 | A/test_fixture_b | Pass |
| Customer P&L and site 16.6% | A/test_fixture_b | Pass |
| Central overhead → $39,000 | A/test_z_lifecycle | Pass |
| OP001 labor and training hours | A/test_fixture_b, A/test_z_lifecycle, UT | Pass |
| FoodCo lowest, data-supported insight | A/test_fixture_b | Pass |
| Re-run of a closed period identical | A/test_z_lifecycle | Pass |
| Every item opens to evidence | A/test_s_workflow | Pass |
| Scope isolation per endpoint | A/test_n_security | Pass |
| Rate SoD; open periods only | A/test_z_lifecycle | Pass |
| Unresolved customer excluded and queued | A/test_u_imports | Pass |
| KPI = table = export | A/test_m_consistency | Pass |
| Chart rules | A/test_m_consistency | Pass |
| 3 months of pilot history < 15 min | `scripts/perf_pilot.py` → `docs/perf_result.json` | Pass (10.2 min) |

## Deviations and limitations

* **D1 Rounding of two Fixture A lines.** The reference sheet rounded 150 × $2.4219 = $363.285 to $363.28 and
  125 × $2.3598 = $294.975 to $294.97 (binary floating point). P2 requires Decimal with ROUND_HALF_UP, which gives
  $363.29 and $294.98. BillRight therefore reports billable **$227,111.61** (reference $227,111.59). Both lines are
  shown as "Rounding only" with no leakage item, so the leakage result (3 items, $357.64) is exactly as required.
* **D2 Navigation** has an 11th entry, *GA tools*, for G1/G3–G8 (added after Release 1; Release 1 pages unchanged).
* **L1 Environment**: developed and tested on Python 3.13 / PostgreSQL 16 (prompt: Python 3.12); the code uses no 3.13-only features.
* **L2 OIDC** is implemented (authorization code + PKCE, JWKS-verified ID token, nonce/state) but was not exercised against a live identity provider in this environment.
* **L3 Reconciliation matrix** is rendered as customer × charge rows with customer subtotals and per-charge totals; the Excel export also has a true customer × charge gap matrix.
* **L4 G1 storage policy** is one policy per site (setting `storage.policy`); per-rate storage policies are not modelled.
