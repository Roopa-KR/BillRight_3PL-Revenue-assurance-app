"""Expense ledger reconciliation (7.5) and customer P&L (7.8).

Inputs: expense lines, allocation pools (``engine.allocation``), billed revenue per customer (all invoice lines
of the period, including adjustment lines invoiced in it), completed orders per customer, confirmed-not-yet-
invoiced leakage per customer, unallocated central overhead (optional).
Method (7.8):
  Total cost_c  = direct cost_c + allocated shared cost_c
  Site profit_c = revenue billed_c - total cost_c
  Margin_c (%)  = site profit_c / revenue billed_c x 100 ("Not available" when revenue = 0)
  Site profit   = sum revenue - sum allocated cost - unassigned cost
  Cost per order_c = total cost_c / orders completed_c ("Not available" when 0 orders)
  Profit before financing and tax = site profit - unallocated central overhead (if entered)
Revenue is billed revenue; confirmed-but-not-invoiced leakage is reported separately and never added.
Unassigned cost is never pushed onto customers.
Output: customer rows and a site summary.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from .allocation import PAYMENT_RECIPIENTS
from .money import ZERO, D, pct


def ledger_by_recipient(expenses) -> dict:
    """7.5: totals by payment recipient; 'unclassified' collects lines without a valid recipient type."""
    out = {k: ZERO for k in PAYMENT_RECIPIENTS}
    for e in expenses:
        key = e.payment_recipient_type if e.payment_recipient_type in PAYMENT_RECIPIENTS else "unclassified"
        out[key] = out.get(key, ZERO) + D(e.amount)
    total = sum((D(e.amount) for e in expenses), ZERO)
    by_recipient = sum((v for k, v in out.items() if k != "unclassified"), ZERO)
    return {"by_recipient": out, "ledger_total": total, "recipient_total": by_recipient,
            "difference": total - by_recipient, "reconciles": total == by_recipient}


@dataclass
class CustomerPnl:
    customer_id: int
    revenue_billed: Decimal
    direct_cost: Decimal
    allocated_shared_cost: Decimal
    total_cost: Decimal
    site_profit: Decimal
    margin_pct: Decimal | None
    orders_completed: int | None
    cost_per_order: Decimal | None
    confirmed_not_invoiced: Decimal


@dataclass
class SitePnl:
    revenue: Decimal
    total_cost: Decimal
    allocated_cost: Decimal
    unassigned_cost: Decimal
    site_profit: Decimal
    margin_pct: Decimal | None
    central_overhead_unallocated: Decimal | None
    profit_before_financing_tax: Decimal | None


def customer_pnl(customer_ids, revenue: dict, pools: dict, orders: dict, confirmed: dict, central_overhead=None):
    direct = defaultdict(lambda: ZERO)
    shared = defaultdict(lambda: ZERO)
    unassigned = ZERO
    total_cost = ZERO
    for pool in pools.values():
        total_cost += pool.pool_amount
        unassigned += pool.unassigned_amount
        for r in pool.rows:
            if r.customer_id is None:
                continue
            if r.method == "direct":
                direct[r.customer_id] += r.amount
            else:
                shared[r.customer_id] += r.amount
    ids = sorted(set(customer_ids) | set(revenue) | set(direct) | set(shared))
    rows = []
    for c in ids:
        rev = D(revenue.get(c, ZERO))
        tc = direct[c] + shared[c]
        profit = rev - tc
        n = orders.get(c)
        rows.append(CustomerPnl(c, rev, direct[c], shared[c], tc, profit, pct(profit, rev), n,
                                (tc / n) if n else None, D(confirmed.get(c, ZERO))))
    rev_total = sum((r.revenue_billed for r in rows), ZERO)
    allocated = sum((r.total_cost for r in rows), ZERO)
    profit = rev_total - allocated - unassigned
    co = D(central_overhead) if central_overhead is not None else None
    site = SitePnl(rev_total, total_cost, allocated, unassigned, profit, pct(profit, rev_total), co,
                   (profit - co) if co is not None else None)
    return rows, site


def display_margin(m: Decimal | None) -> str:
    from .money import D as _D
    if m is None:
        return "Not available"
    from decimal import ROUND_HALF_UP
    return f"{_D(m).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"
