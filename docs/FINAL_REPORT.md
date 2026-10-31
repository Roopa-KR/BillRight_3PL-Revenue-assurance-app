# Final report — GiNi BillRight, Release 1 and GA

## What was built

A working Flask + PostgreSQL application that prices every billable warehouse activity against each customer's
versioned contract, compares billable with billed per customer, charge code and period, raises leakage items under
rules L1–L7 for a recorded human decision, records the 3PL's operating expenses, allocates them to customers with
approved drivers, and shows each customer's revenue, cost, site profit and margin.

* **Release 1, F1–F13, all built.** Customer and contract register; rate-card editor with draft → submitted →
  approved/rejected and segregation of duties; billable event capture from GiNi extracts (TI102C, TI202, orders,
  storage) plus a manual entry form; rating engine with per-unit, hourly, fixed-fee, cost-plus and pass-through
  methods, tiers (method chosen per rate) and minimums; invoice import and reconciliation; rules L1–L7 with
  precedence and per-rule tolerances; review queue with decisions, approvals, notes and history; evidence drill-down to
  events, orders, tasks, timestamps, labor minutes and invoice lines; expense ledger with payment-recipient
  reconciliation; cost allocation (direct first, approved drivers, largest remainder, unassigned kept separate); labor
  attribution; customer P&L; seven-step period close with freeze, verify and reopen; seven exports; OIDC sign-in,
  five roles, site/customer scopes, PostgreSQL row-level tenant isolation, append-only audit log and immutable
  snapshots.
* **GA, G1–G8, all built**, each with its own test and without changing any Release 1 result: storage billing from
  daily inventory snapshots (grace days, split months, minimums), mobile capture by work-order barcode/QR with photo,
  daily run with alerts to billing analysts, surcharge automation, minimums and tiers checker, contract drift monitor,
  SLA penalty exposure vs penalties booked, and an accounting export connector (documented file format plus an adapter
  interface for QuickBooks, NetSuite, SAP and Dynamics).
* **Pages** (section 9.3): role homes, Leakage review and item detail, Billing reconciliation, Customers and customer
  detail with the "Why is margin low?" panel, Costs and allocation, Contracts and rates (with version diff and
  re-pricing impact), Work capture (phone-first), Period close, Data, Admin; plus a GA tools page.
  **Charts** H1, L1, C1, C2, C3 and K1 only, on locally bundled Plotly.js.
* **Fixtures.** Fixture A converts the GiNi Labor Economics synthetic dataset into GiNi-style import files (customer
  master, TI102C, order header/lines, task links, labor intervals, rate card, invoices). Fixture B builds the BSP worked
  example as import files with illustrative drivers that reproduce the document's split. Both load through the normal
  import path and are labelled "Synthetic" and "Illustrative" on every page and export.

## Test results

83 automated tests, all passing on a fresh database (183 s). Every acceptance criterion in section 12.2 passes; the
measured values are in `docs/TEST_REPORT.md`. Performance: 3 months of pilot history (13 weekly periods, 994,631 events) imported and calculated in 10.2 minutes, against the proposed 15-minute target.

## Requirements not met, or met differently, and why

1. **Fixture A billable total is $227,111.61, not $227,111.59.** The reference sheet rounded two lines with binary
   floating point ($363.285 → $363.28 and $294.975 → $294.97). The prompt's own rule P2 (Decimal, ROUND_HALF_UP, once
   per invoice line) gives $363.29 and $294.98. BillRight follows P2. Quantities and prices on those two lines match
   the contract, so no rule applies and no leakage item is raised; reconciliation shows them as "Rounding only". The
   three required leakage items and the $357.64 total are exact.
2. **OIDC was not tested against a live identity provider** (none is reachable from the build environment). The flow
   is implemented to the standard (authorization code + PKCE, JWKS-verified ID token, state, nonce); the development
   login is tested and is off outside `APP_ENV=development`.
3. **Python 3.13 instead of 3.12** in the build environment. No 3.13-only features are used; the Dockerfile uses 3.12.
4. **Navigation has an 11th item, "GA tools"**, for the GA features that have no natural home in the ten Release 1
   pages. The Release 1 pages are unchanged.
5. **G1 storage policy is one policy per site** (setting `storage.policy`), not per rate.
6. **G5 has no fixture with a minimum or tier billed on an invoice**, so its finding path is proven by engine unit
   tests only.
7. **The reconciliation matrix** is shown as customer × charge rows with customer subtotals and charge totals (a
   literal wide grid of 12 customers × 8 charges with five values per cell does not fit a screen); the Excel export
   carries the true customer × charge grid.
8. **Live GiNi MySQL feed**: ingestion reads GiNi table extracts with a saved column mapping per source; a direct
   scheduled MySQL connection is an adapter to add once the deployed schema and the customer join path are verified
   (open decision 10).

## Open decisions (section 14) — left to you, each a setting with no invented default

| # | Decision | Where it is set | Effect while empty |
|---|---|---|---|
| 1 | Hosting | Deployment: container image (`Dockerfile`, `docker-compose.yml`) or plain Flask/waitress | — |
| 2 | Labor cost basis | `labor.cost_basis` (+ `labor.standard_rates` if standard) | A run for a period with labor data is blocked and names the setting |
| 3 | Rule tolerances | `tolerance.L1…L7.amount` and `.pct` per site | Calculation runs are blocked |
| 4 | Approval threshold for leakage decisions | `leakage.approval_threshold` | Decisions are blocked and name the setting |
| 5 | Back-billing window | `leakage.back_billing_window_days` | "Bill now" decisions are blocked |
| 6 | Tier method | Per rate (`tier_method` = all_units or incremental), required whenever tiers exist | The rate cannot be saved |
| 7 | Invoice source system and format for the pilot | `invoice.source_system`; CSV/Excel import with a saved column mapping works now | — |
| 8 | Daily inventory snapshots per customer in GiNi | `storage.daily_snapshots_available`; `storage.policy` for G1 | G1 storage billing is off |
| 9 | Customer margin targets | `margin_target.<customer>` per customer | Shown as "Not set"; excluded from "below target" counts |
| 10 | Customer-resolution join path per tenant | `customer_resolution.join_path` (verify in the deployed GiNi schema before go-live) | Calculation runs are blocked |

Fixture settings (all tolerances $0.00 and 0%, for example) are test settings loaded with the fixtures, not product
defaults.
