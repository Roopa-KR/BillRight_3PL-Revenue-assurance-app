"""Billable vs billed reconciliation and leakage rules L1-L7 (7.4).

Inputs: billable lines (``engine.billable``), invoice lines, the rate history per customer and charge
code (approved and superseded versions), charge-code categories, cannot-rate keys and per-rule
tolerances (amount and percentage, from settings; both must be exceeded to raise an item).
Method:
  Leakage = Billable - Billed per (customer, charge code, period); positive = under-billing,
  negative = over-billing. Every rule that matches is recorded; the primary rule is the first in the
  precedence L1, L6, L3, L5, L4, L2, L7 and the others are listed as "also matches".
  Invoice lines that carry an ``adjustment_ref`` settle an earlier leakage item: they are excluded from
  their own period's billed totals and reported as "Leakage invoiced" for that item.
  When events of a key could not be rated, billable is understated, so over-billing (L7) is not raised
  for that key and the matrix status is "cannot_rate".
Output: reconciliation rows (one per key) and leakage findings (rows that pass the primary rule's tolerance).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from .money import CENT, ZERO, D

PRECEDENCE = ("L1", "L6", "L3", "L5", "L4", "L2", "L7")
RULE_NAMES = {
    "L1": "Not billed",
    "L2": "Billed too few units",
    "L3": "Wrong price",
    "L4": "Surcharge not billed",
    "L5": "Minimum not applied",
    "L6": "Expired rate used",
    "L7": "Billed too much",
}


@dataclass(frozen=True)
class InvoiceLineSpec:
    id: int
    customer_id: int
    charge_code_id: int | None
    raw_charge_code: str
    period_id: int
    quantity: Decimal | None
    unit_price: Decimal | None
    amount: Decimal
    adjustment_ref: str | None = None


@dataclass
class Finding:
    customer_id: int
    charge_code_id: int
    period_id: int
    rule_code: str
    also_matches: list
    billable_quantity: Decimal
    billed_quantity: Decimal
    billable_amount: Decimal
    billed_amount: Decimal
    gap_amount: Decimal
    direction: str
    contract_unit_price: Decimal | None
    billed_unit_prices: list
    rated: list = field(default_factory=list)
    invoice_line_ids: list = field(default_factory=list)


@dataclass
class ReconRow:
    customer_id: int
    charge_code_id: int
    period_id: int
    billable_quantity: Decimal
    billed_quantity: Decimal
    billable_amount: Decimal
    billed_amount: Decimal
    gap_amount: Decimal
    status: str
    rules_matched: list
    note: str | None
    finding: Finding | None = None


def tolerance_exceeded(gap: Decimal, billable: Decimal, amount_tol: Decimal, pct_tol: Decimal) -> bool:
    g = abs(gap)
    if g <= amount_tol:
        return False
    if billable == 0:
        return True
    return g / abs(billable) * 100 > pct_tol


def match_rules(billable, billed_qty, billed_amount, billed_prices, invoice_count, category, superseded_prices):
    """Return the list of matching rule codes for one key (unordered)."""
    matched = []
    b_qty = billable.quantity if billable else ZERO
    b_amt = billable.amount if billable else ZERO
    contract_prices = {D(p) for p in (billable.unit_prices if billable else [])}
    if b_qty > 0 and invoice_count == 0:
        matched.append("L1")
    if invoice_count:
        wrong = {p for p in billed_prices if p is not None and contract_prices and p not in contract_prices}
        expired = {p for p in wrong if p in superseded_prices}
        if expired:
            matched.append("L6")
        if wrong:
            matched.append("L3")
        if billable is not None:
            minimum = billable.governing_rate.minimum_charge
            if minimum is not None and D(minimum) > 0 and billed_amount < D(minimum) <= b_amt:
                matched.append("L5")
        if billed_qty < b_qty:
            matched.append("L2")
    if category == "surcharge" and billed_qty < b_qty:
        matched.append("L4")
    if billed_amount > b_amt:
        matched.append("L7")
    return matched


def reconcile(billable_lines: dict, invoice_lines: list[InvoiceLineSpec], categories: dict, rate_history: dict,
              cannot_rate_keys: set, tolerances: dict) -> list[ReconRow]:
    """``rate_history[(customer_id, charge_code_id)]`` = list of RateSpec (approved and superseded)."""
    invoices = defaultdict(list)
    for line in invoice_lines:
        if line.adjustment_ref or line.charge_code_id is None:
            continue
        invoices[(line.customer_id, line.charge_code_id, line.period_id)].append(line)
    keys = sorted(set(billable_lines) | set(invoices))
    rows = []
    for key in keys:
        customer_id, charge_id, period_id = key
        bl = billable_lines.get(key)
        lines = invoices.get(key, [])
        billed_qty = sum((D(l.quantity) for l in lines if l.quantity is not None), ZERO)
        billed_amt = sum((D(l.amount) for l in lines), ZERO)
        billed_prices = sorted({D(l.unit_price) for l in lines if l.unit_price is not None})
        b_qty = bl.quantity if bl else ZERO
        b_amt = bl.amount if bl else ZERO
        history = rate_history.get((customer_id, charge_id), [])
        superseded = {D(r.unit_price) for r in history if r.version_status == "superseded" and r.unit_price is not None}
        current = {D(p) for p in (bl.unit_prices if bl else [])}
        superseded -= current
        matched = match_rules(bl, billed_qty, billed_amt, billed_prices, len(lines), categories.get(charge_id), superseded)
        has_cannot = key in cannot_rate_keys
        if has_cannot and "L7" in matched:
            matched.remove("L7")
        gap = b_amt - billed_amt
        ordered = [r for r in PRECEDENCE if r in matched]
        finding = None
        status = "reconciled"
        note = None
        if ordered and gap != 0:
            primary = ordered[0]
            amt_tol, pct_tol = tolerances[primary]
            if tolerance_exceeded(gap, b_amt, amt_tol, pct_tol):
                contract_price = D(bl.governing_rate.unit_price) if bl and bl.governing_rate.unit_price is not None else None
                finding = Finding(customer_id, charge_id, period_id, primary, ordered[1:], b_qty, billed_qty, b_amt, billed_amt,
                                  gap, "under" if gap > 0 else "over", contract_price, [str(p) for p in billed_prices],
                                  bl.rated if bl else [], [l.id for l in lines])
                status = "gap"
            else:
                note = "Within tolerance"
        elif gap != 0:
            if abs(gap) <= CENT * max(len(lines), 1):
                status = "rounding"
                note = (f"Quantity and unit price agree with the contract; the invoice amount differs by "
                        f"{abs(gap)} from rounding.")
            else:
                status = "gap"
                note = "Billed amount differs from quantity x price; no rule in L1-L7 describes it. Review the invoice."
        if has_cannot:
            status = "cannot_rate" if status != "gap" else status
            note = (note + " " if note else "") + "Some events for this charge could not be rated."
        rows.append(ReconRow(customer_id, charge_id, period_id, b_qty, billed_qty, b_amt, billed_amt, gap, status, ordered, note, finding))
    return rows
