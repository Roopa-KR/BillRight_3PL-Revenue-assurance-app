# User guide

GiNi BillRight prices every billable warehouse activity against each customer's contract, compares it with what was
invoiced, puts every gap in front of a person with its evidence, and shows what each customer really earns.
Nothing is invoiced, credited or written off automatically.

Every page header shows the site, the period selector, **Data as of** (latest import; hover for each source), the
**run** the figures come from, and a **Synthetic / Illustrative data** badge when the data is not real. A closed
period shows **Closed · frozen**. Hover the **i** next to any tile or column for its definition.

## Billing analyst

**Home — what needs action today.** Four tiles: open leakage ($ and count), “Bill next cycle” items carried from
earlier periods, cannot-rate items, and manual entries waiting for review. “Your next items” lists the ten largest
open items; *Open* goes to the item.

**Leakage review.** Filter by customer, rule, status, direction (billed too little / too much) and amount. The chart
shows which kind of leakage costs most right now; click a bar to filter. Select items and *Assign* them to a
colleague. *Export leakage register* gives the Excel register with decisions and history.

**An item** reads top to bottom:
1. *What happened* — one sentence, e.g. “CUST-07 was not billed the cold-chain surcharge for 2026-W38: 345 lines × $0.40 = $138.00.”
2. *Why we think so* — the contract rate (contract reference, rate-card version), the billable calculation, the
   invoice line or “No invoice line found”, and the events (source record, order, task, worker, timestamps, labor
   minutes of the warehouse order).
3. *What to do* — choose exactly one decision:
   * under-billing: **Bill now**, **Bill next cycle**, **Waive** (choose a reason: commercial goodwill, contract
     interpretation, below materiality, other with text), **Not billable** (reason required);
   * over-billing: **Issue credit**, **Not owed** (reason), **Waive** (reason).
   Above the approval threshold a finance controller who did not decide the item must approve it. “Bill now” is
   refused when the period ended more than the back-billing window ago. Add evidence notes at any time.

**Billing reconciliation.** Billable vs billed for every customer × charge code of the period, with customer
subtotals, a site total and totals per charge code. Click a charge to see the rated events and invoice lines behind
it. Status: Reconciled · Gap (a leakage item exists) · Rounding only · Cannot rate.

**Work capture review.** Accept or reject (with a reason) the off-system work supervisors recorded; accepted
entries are priced in the next run.

**Data.** Cannot-rate queue with the fix (“Add a rate for LINE_RX valid from 2026-09-07”); unresolved customers.

**Running a period.** *Run period* (header) recalculates the open period from the current data. Decisions are kept;
items that no longer appear are marked “Resolved by data”.

## Finance controller

**Home.** Leakage confirmed and invoiced this period, site margin, approvals waiting; chart H1 (are we recovering
what we confirm?); the approvals list (rate-card versions, leakage decisions above the threshold, allocation
drivers) and the close checklist.

**Approvals.** Rate-card versions: open the version, read the side-by-side diff and the number of open-period
events the change re-prices, then *Approve* or *Reject* with a reason. You cannot approve a version you authored,
and an effective date inside a closed period is refused. Allocation drivers: *Admin → Allocation drivers*.

**Costs and allocation.** Total operating cost, allocated, unassigned, pools with exceptions; chart K1; *Who gets
paid* with the reconciliation to the expense ledger; the allocation matrix (✓ when pool = allocated + unassigned).
Click a category for its lines, driver quantities and shares. *Labor attribution* (controller only) shows each
worker's attributed and unassigned hours at the loaded rate; training, idle and shared time stays unassigned.

**Customers.** Revenue, cost, site profit and margin; chart C1; the table reconciles to the tiles with the
*Unassigned cost* line. A customer page shows the revenue-to-profit bridge (C2), the margin trend after three closed
periods (C3), cost by category with its method, and — when the margin is below the site's or its target — the
*Why is margin low?* panel listing only drivers the data supports. Enter unallocated central overhead to see profit
before financing and tax.

**Period close.** Seven checks run live (payroll and agency reconciliation, vendor invoices captured, every
transaction mapped to a customer, every pool allocated and reconciled, rating and review complete, P&L review,
receivables/payables report). Checks 3 and 6 can be acknowledged with a reason. *Close* freezes the period.
*Verify* re-runs a closed period read-only and confirms the results are identical. *Reopen* needs a reason and starts
a new close version.

**Exports.** Invoice adjustment file (CSV/Excel: approved Bill now and Issue credit only), leakage register,
reconciliation, customer P&L (Excel and PDF), expense and allocation statement, close report (PDF), receivables and
payables, pilot results. Every export carries site, period, run, data-as-of and the data label.

## Account manager

Read-only. Home lists customers by margin (lowest first) with revenue, site margin and customers below their target
(“Not set” when no targets are entered). Customer pages show the P&L bridge and the *Why is margin low?* panel. No
worker-level detail or individual pay is shown.

## Operations supervisor

**Work capture** (phone friendly): scan or type the work order, choose the customer (only yours) and the service,
quantity and unit, date and start/end time, a note and an optional photo, then *Send for review*. Your entries show
their status (submitted, accepted, rejected with the reason). On phones whose browser supports it, *Scan with camera*
reads a barcode or QR code.

## GA tools

Storage from daily inventory snapshots (G1), the daily check that alerts analysts of new items before invoicing (G3),
surcharge rules (G4), the minimums and tiers checker (G5), contract drift warnings (G6), SLA penalty exposure vs
penalties booked (G7) and the accounting export (G8).
