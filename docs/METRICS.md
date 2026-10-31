# Metrics and formulas, as implemented

Every number on a page, in the API and in the exports comes from `billright/metrics/service.py` (P8), which reads
the results the calculation engine (`billright/engine/`) wrote for the run of the period (the close run for a closed
period). Money is `decimal.Decimal` end to end; amounts are rounded to cents once, at the billable line.

## Billing

| Metric | Formula | Code |
|---|---|---|
| Rated amount (event) | quantity in rate unit × unit price of the rate valid on the **service date**; cost-plus: cost × (1 + markup/100); pass-through: cost; fixed fee: fee once per period | `engine/rating.py` |
| Billable amount | max(Σ rated amounts (or tier re-pricing), minimum_charge), rounded once ROUND_HALF_UP per customer × charge × period | `engine/billable.py` |
| Billed amount | Σ invoice line amounts of the key (lines with ADJUSTMENT_REF excluded) | `engine/leakage.py` |
| Leakage (gap) | Billable − Billed | `engine/leakage.py` |
| Leakage rate | Σ leakage ÷ Σ billable | `engine/leakage.py` docstring; matrix totals |
| Open leakage (tile) | Σ positive gap of items New or Under review in the period (+ count) | `analyst_kpis` |
| Bill next cycle carried | Σ gap of earlier periods' items decided “Bill next cycle” and not yet invoiced | `analyst_kpis` |
| Cannot-rate items | count of cannot-rate rows of the run (all reasons incl. unresolved customer) | `analyst_kpis` |

## 7.9 operational metrics

| Metric | Definition (as coded in `period_leakage_metrics` / `pilot_metrics`) |
|---|---|
| Leakage flagged | Σ positive gap of items raised for the period |
| Leakage confirmed | Σ positive gap of items with a final decision “Bill now” or “Bill next cycle” |
| Leakage invoiced (recovered) | Σ min(invoiced amount, gap) of confirmed items whose adjustment appears on a later imported invoice line (ADJUSTMENT_REF) |
| Recovery rate | invoiced ÷ confirmed (“Not available” when nothing is confirmed) |
| Leakage prevented | Σ positive gap of items flagged by a daily alert (G3) that later disappeared because the original invoice billed them correctly (“Resolved by data”) |
| Precision | items decided Bill now, Bill next cycle, Waive (or Issue credit) ÷ all decided items; “Not billable” / “Not owed” count as false alerts |
| Over-billing found | Σ |gap| of L7 items |
| Time to close | days from period end to the close date |
| Labor attribution coverage | attributed labor cost ÷ total labor cost (`engine/labor.py::coverage`) |
| Cannot-rate value | count of cannot-rate items (and their estimated quantities); no value is invented |
| Return on subscription | (recovered + prevented) ÷ subscription cost — pilot results export only; subscription cost from setting `pilot.subscription_cost` |

## Costs and P&L (7.5–7.8)

| Metric | Formula |
|---|---|
| Expense ledger reconciliation | Σ expense lines = Σ by payment recipient (direct payroll + staffing agency + carriers + facility/equipment/IT/other); any difference blocks close |
| Direct cost_c | expense lines carrying a customer (100%) |
| Allocated shared cost_c | pool × driver_quantity_c ÷ Σ driver quantity (largest-remainder cents); no approved driver → whole pool unassigned (never an equal split) |
| Pool check | pool = Σ allocated to customers + unassigned |
| Total cost_c | direct cost_c + allocated shared cost_c |
| Revenue_c | billed revenue: Σ invoice lines of the period (adjustment lines included in the period they are invoiced) |
| Site profit_c | revenue_c − total cost_c |
| Margin_c | site profit_c ÷ revenue_c × 100, displayed to 1 decimal; “Not available” if revenue = 0 |
| Site profit | Σ revenue − Σ allocated cost − unassigned cost |
| Site margin | site profit ÷ site revenue × 100 |
| Cost per order_c | total cost_c ÷ completed orders_c (resolved order_completed events); “Not available” if 0 orders |
| Profit before financing and tax | site profit − unallocated central overhead (only when entered) |
| Confirmed, not yet invoiced | Σ (gap − invoiced) of the period's confirmed under-billing items; never added to revenue |
| Loaded rate | loaded_cost ÷ paid_hours per worker; assigned cost = attributed hours × loaded rate; unattributed paid hours stay unassigned |

When a period has no expense data, cost, site profit and margin show “Not available” (P5) instead of a 100% margin.

## Charts (9.4) — all built in `billright/web/charts.py` from the rows above

H1 confirmed vs invoiced by period (≥ 2 periods) · L1 open gap by rule · C1 cost + profit = revenue per customer,
lowest margin accented · C2 revenue → cost categories (max 6) → site profit · C3 margin by closed period vs site
margin (≥ 3 closed periods) · K1 cost by category segmented by expense line.
