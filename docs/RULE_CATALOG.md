# Leakage rule catalog (L1–L7)

Implemented in `billright/engine/leakage.py`. Rules compare, per **customer × charge code × billing period**, the
*billable* line (what the contract allows for the work performed, rounded once to cents) with the *billed* invoice
lines of the same key.

```
Leakage = Billable − Billed        positive = under-billing, negative = over-billing
Leakage rate = Σ Leakage ÷ Σ Billable
```

**Raising an item.** All rules that match are recorded. The *primary* rule is the first match in the precedence
**L1, L6, L3, L5, L4, L2, L7**; the others appear on the item as "also matches". An item is raised only when the
primary rule's two tolerances (settings `tolerance.<rule>.amount` and `tolerance.<rule>.pct`) are **both**
exceeded: |gap| > amount and |gap| ÷ billable × 100 > pct (when billable is 0 the percentage test passes). An empty
tolerance setting blocks the run and names the setting. A key with a gap but no matching rule is shown in the
reconciliation matrix as "Rounding only" (|gap| ≤ $0.01 per invoice line, quantities and prices agree) or as
"Gap" with a note; it is not silently treated as reconciled.

**Adjustment lines.** An invoice line with `ADJUSTMENT_REF = LKG-<id>` settles that item ("Leakage invoiced"); it is
excluded from its own period's billed totals so it is never mistaken for over-billing.

**Cannot rate.** When some events of a key could not be rated, billable is understated: L7 is not raised for that key
and the matrix status is "Cannot rate". Under-billing rules still apply (billable is at least that much).

**L2 / L4 compare units**, so they apply only to unit-priced charges (per unit, hourly); for cost-plus, pass-through
and fixed-fee charges the comparison is by amount (L1, L3, L5, L6, L7).

| Rule | Plain name | Detects | Logic in code | Decisions |
|---|---|---|---|---|
| L1 | Not billed | Work done, no invoice line | billable quantity > 0 and no invoice line for the key | Bill now · Bill next cycle · Waive · Not billable |
| L2 | Billed too few units | Fewer units billed than performed | billed quantity < billable quantity | same |
| L3 | Wrong price | Standard or wrong price used | an invoice unit price is not one of the contract prices applied to the events (rate valid on each service date) | same (or credit decisions if billed > billable) |
| L4 | Surcharge not billed | Conditional charge not applied | charge category = surcharge and billed quantity < number of events that met the surcharge condition | same |
| L5 | Minimum not applied | Low-volume period billed below the minimum | rate has minimum_charge > 0 and billed amount < minimum ≤ billable | same |
| L6 | Expired rate used | Old rate card still in use | an invoice unit price equals the price of a superseded rate-card version of the same customer + charge (and not a current price) | same |
| L7 | Billed too much | Customer charged more than the contract allows (P9) | billed amount > billable amount | Issue credit · Not owed (reason) · Waive (reason) |

The direction of the gap, not the rule, decides the decision set: any item with billed > billable gets the credit
decisions.

## Examples (all from the automated tests)

| Rule | Example | Result |
|---|---|---|
| L1 | CUST-07, COLD_CHAIN_SURCHARGE, 2026-W38: 345 refrigerated pick lines × $0.40, no invoice line | Item $138.00, also matches L4 |
| L2 | CUST-02, ORDER_FEE, 2026-W38: 246 completed orders, 221 invoiced at $4.3695 | Item $109.24 (“25 × $4.3695 = $109.24 not billed”) |
| L3 | 10 lines at contract $2.00, invoiced at $1.50 | Item $5.00 |
| L4 | Surcharge on 10 qualifying lines, invoiced for 5 | Item, also matches L2 |
| L5 | 1 line at $1 with a $50 minimum, invoiced $1 | Item $49.00 |
| L6 | Invoiced at $1.50, the price of superseded version 1 | Item, also matches L3 |
| L7 | CUST-01 LINE_RX re-invoiced 5,700 lines for 5,631 performed | Item −$82.52 (possible credit) |
| none | CUST-11 LINE_CAGE: 150 × $2.4219 = $363.285 → billable $363.29, invoice $363.28 | “Rounding only”, no item |

## Sentences (“What happened”)

Generated deterministically in `billright/metrics/service.py::what_happened`, e.g.
“CUST-07 was not billed the cold-chain surcharge for 2026-W38: 345 lines × $0.40 = $138.00.”
