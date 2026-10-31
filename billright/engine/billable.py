"""Billable amount per customer x charge code x period (7.3).

Inputs: rated rows (``engine.rating.Rated``) for one run.
Method: sum quantity and unrounded amount per (customer, charge code, period); if the governing rate has
tiers, re-price the period quantity by its explicit ``tier_method`` (``all_units``: every unit at the
price of the tier the total quantity reaches; ``incremental``: each band at its own price); then
Billable = max(sum, minimum_charge); then round ONCE to cents with ROUND_HALF_UP.
The governing rate for tiers and minimum is the rate applied to the latest service date in the period.
Assumption: tiers are only defined for per_unit / hourly rates (validated in the rate-card editor).
Output: ``BillableLineResult`` per key.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from .money import ZERO, D, round_cents


@dataclass
class BillableLineResult:
    customer_id: int
    charge_code_id: int
    period_id: int
    quantity: Decimal
    unrounded: Decimal
    amount: Decimal
    minimum_applied: bool
    tier_applied: str | None
    unit_prices: list
    rate_ids: list
    governing_rate: object
    rated: list = field(default_factory=list)


def price_tiers(quantity: Decimal, tiers, method: str) -> tuple[Decimal, str]:
    bands = sorted(tiers, key=lambda t: D(t["from"]))
    if method == "all_units":
        chosen = None
        for t in bands:
            lo, hi = D(t["from"]), (D(t["to"]) if t.get("to") not in (None, "") else None)
            if quantity >= lo and (hi is None or quantity < hi):
                chosen = t
        if chosen is None:
            chosen = bands[0]
        return quantity * D(chosen["unit_price"]), f"all units at {chosen['unit_price']} (tier from {chosen['from']})"
    if method == "incremental":
        total = ZERO
        for t in bands:
            lo = D(t["from"])
            hi = D(t["to"]) if t.get("to") not in (None, "") else None
            if quantity <= lo:
                continue
            upper = quantity if hi is None else min(quantity, hi)
            total += (upper - lo) * D(t["unit_price"])
        return total, "incremental bands"
    raise ValueError(f"tier_method must be explicit when tiers exist, got {method!r}")


def build_billable_lines(rated) -> dict:
    groups = defaultdict(list)
    for r in rated:
        groups[(r.rate.customer_id, r.rate.charge_code_id, r.period_id)].append(r)
    out = {}
    for key in sorted(groups):
        rows = groups[key]
        qty = sum((r.quantity for r in rows), ZERO)
        unrounded = sum((r.amount for r in rows), ZERO)
        governing = max(rows, key=lambda r: (r.service_date, r.rate.id)).rate
        tier_note = None
        if governing.tiers and governing.tier_method != "none":
            unrounded, tier_note = price_tiers(qty, governing.tiers, governing.tier_method)
        minimum_applied = False
        if governing.minimum_charge is not None and D(governing.minimum_charge) > unrounded:
            unrounded = D(governing.minimum_charge)
            minimum_applied = True
        prices = sorted({str(r.unit_price) for r in rows if r.unit_price is not None})
        out[key] = BillableLineResult(key[0], key[1], key[2], qty, unrounded, round_cents(unrounded), minimum_applied,
                                      tier_note, prices, sorted({r.rate.id for r in rows}), governing, rows)
    return out
