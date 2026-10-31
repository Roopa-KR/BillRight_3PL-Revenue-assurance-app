"""Rate selection (7.2), unit conversion and per-event pricing (7.3).

Pure functions: no database or Flask imports. Inputs are plain dataclasses built by
``billright.services.run`` from the database snapshot of one calculation run.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from .money import ZERO, D

PRICING_METHODS = ("per_unit", "hourly", "fixed_fee", "cost_plus", "pass_through")
CANNOT_RATE_REASONS = ("no_customer", "no_rate", "no_uom_conversion", "ambiguous_rate", "missing_quantity")


@dataclass(frozen=True)
class RateSpec:
    id: int
    rate_card_version_id: int
    version_no: int
    version_status: str  # approved | superseded
    version_from: dt.date | None  # effective_from of the version
    version_to: dt.date | None  # superseded_from of the version (exclusive)
    contract_reference: str
    customer_id: int
    charge_code_id: int
    charge_code: str
    category: str
    event_type: str | None
    activity_code: str | None
    pricing_method: str
    unit_price: Decimal | None
    uom: str | None
    markup_pct: Decimal | None
    eligible_cost_categories: tuple = ()
    minimum_charge: Decimal | None = None
    billing_period: str = "month"
    tier_method: str = "none"
    tiers: tuple = ()
    condition: tuple = ()  # sorted (key, value) pairs
    valid_from: dt.date = dt.date.min
    valid_to: dt.date | None = None  # inclusive

    def covers(self, day: dt.date) -> bool:
        if day < self.valid_from or (self.valid_to is not None and day > self.valid_to):
            return False
        if self.version_from is not None and day < self.version_from:
            return False
        if self.version_to is not None and day >= self.version_to:
            return False
        return True

    def condition_matches(self, attributes: dict) -> bool:
        attrs = attributes or {}
        for key, expected in self.condition:
            got = attrs.get(key)
            if isinstance(expected, tuple):
                if got not in expected:
                    return False
            elif got != expected:
                return False
        return True


@dataclass(frozen=True)
class EventSpec:
    id: int | None
    source_ref: str
    event_type: str
    service_date: dt.date
    quantity: Decimal | None
    uom: str | None
    material_id: str | None
    attributes: dict = field(default_factory=dict, hash=False, compare=False)
    customer_id: int | None = None
    manual_entry_id: int | None = None
    charge_code_id: int | None = None  # set when the source names the charge (manual entries)


@dataclass(frozen=True)
class Conversion:
    material_id: str | None
    from_uom: str
    to_uom: str
    multiplier: Decimal
    valid_from: dt.date
    valid_to: dt.date | None


@dataclass
class Rated:
    event: EventSpec | None
    rate: RateSpec
    quantity: Decimal
    unit_price: Decimal | None
    amount: Decimal  # unrounded
    service_date: dt.date
    period_id: int
    expense_line_id: int | None = None


@dataclass
class CannotRate:
    event: EventSpec | None
    source_ref: str
    reason: str
    details: dict
    customer_id: int | None = None
    charge_code_id: int | None = None
    estimated_quantity: Decimal | None = None


def normalise_condition(condition: dict | None) -> tuple:
    if not condition:
        return ()
    out = []
    for k in sorted(condition):
        v = condition[k]
        out.append((k, tuple(v) if isinstance(v, list) else v))
    return tuple(out)


class UomConverter:
    """Material-specific conversions first, then generic (material_id NULL). Reverse conversions are derived."""

    def __init__(self, conversions: list[Conversion]):
        self.by_key = defaultdict(list)
        for c in conversions:
            self.by_key[(c.material_id, c.from_uom, c.to_uom)].append((c, False))
            self.by_key[(c.material_id, c.to_uom, c.from_uom)].append((c, True))

    def convert(self, qty: Decimal, from_uom: str | None, to_uom: str | None, material_id: str | None, day: dt.date):
        if from_uom == to_uom or to_uom is None:
            return qty
        for mat in (material_id, None):
            for conv, reverse in self.by_key.get((mat, from_uom, to_uom), []):
                if conv.valid_from <= day and (conv.valid_to is None or day <= conv.valid_to):
                    return qty / conv.multiplier if reverse else qty * conv.multiplier
        return None


def select_rate(candidates: list[RateSpec], day: dt.date, attributes: dict):
    """7.2 Rate selection.

    Inputs: rates of one customer and one charge code that apply to the event's type and activity;
    the event's service date and attributes.
    Method: keep rates whose validity (rate window and rate-card version window) covers the service
    date and whose ``condition`` matches the attributes.
    Output: ('ok', rate) | ('no_rate', None) | ('ambiguous_rate', [rates]).
    """
    hits = [r for r in candidates if r.covers(day) and r.condition_matches(attributes)]
    if not hits:
        return "no_rate", None
    if len(hits) > 1:
        return "ambiguous_rate", hits
    return "ok", hits[0]


def rate_events(events, rates: list[RateSpec], converter: UomConverter, period_of) -> tuple[list[Rated], list[CannotRate]]:
    """Price each event with the rate valid on its service date (P6).

    ``period_of(day)`` returns the billing period id for a service date (or None).
    An event can create several rated rows (e.g. a cooler line is priced as LINE_COOLER and also
    attracts COLD_CHAIN_SURCHARGE). An event that matches no applicable charge goes to Cannot rate.
    """
    by_customer_type = defaultdict(list)
    for r in rates:
        if r.event_type:
            by_customer_type[(r.customer_id, r.event_type)].append(r)
    rated: list[Rated] = []
    cannot: list[CannotRate] = []
    for ev in events:
        if ev.customer_id is None:
            cannot.append(CannotRate(ev, ev.source_ref, "no_customer", {"event_type": ev.event_type}, estimated_quantity=ev.quantity))
            continue
        if ev.quantity is None:
            cannot.append(CannotRate(ev, ev.source_ref, "missing_quantity", {"event_type": ev.event_type}, ev.customer_id))
            continue
        period_id = period_of(ev.service_date)
        activity = (ev.attributes or {}).get("activity")
        pool = by_customer_type.get((ev.customer_id, ev.event_type), [])
        if ev.charge_code_id is not None:
            pool = [r for r in pool if r.charge_code_id == ev.charge_code_id]
        applicable = [r for r in pool if r.activity_code is None or r.activity_code == activity]
        by_charge = defaultdict(list)
        for r in applicable:
            by_charge[r.charge_code_id].append(r)
        produced = False
        problems = []
        for charge_id in sorted(by_charge):
            cands = by_charge[charge_id]
            # a conditional charge (e.g. surcharge for refrigerated items) only applies when its condition can match
            if not any(r.condition_matches(ev.attributes) for r in cands):
                continue
            status, rate = select_rate(cands, ev.service_date, ev.attributes)
            if status == "no_rate":
                problems.append(CannotRate(ev, ev.source_ref, "no_rate", {
                    "charge_code": cands[0].charge_code, "service_date": ev.service_date.isoformat(),
                    "fix": f"Add a rate for {cands[0].charge_code} valid from {ev.service_date.isoformat()}"},
                    ev.customer_id, charge_id, ev.quantity))
                continue
            if status == "ambiguous_rate":
                problems.append(CannotRate(ev, ev.source_ref, "ambiguous_rate", {
                    "charge_code": cands[0].charge_code, "rate_ids": sorted(x.id for x in rate),
                    "fix": "Rates overlap for this charge, date and condition; correct the rate card"}, ev.customer_id, charge_id, ev.quantity))
                continue
            qty = converter.convert(ev.quantity, ev.uom, rate.uom, ev.material_id, ev.service_date)
            if qty is None:
                problems.append(CannotRate(ev, ev.source_ref, "no_uom_conversion", {
                    "charge_code": rate.charge_code, "from_uom": ev.uom, "to_uom": rate.uom, "material_id": ev.material_id,
                    "fix": f"Add a unit conversion {ev.uom} -> {rate.uom}"}, ev.customer_id, charge_id, ev.quantity))
                continue
            if period_id is None:
                problems.append(CannotRate(ev, ev.source_ref, "no_rate", {
                    "charge_code": rate.charge_code, "service_date": ev.service_date.isoformat(),
                    "fix": "No billing period covers this service date"}, ev.customer_id, charge_id, ev.quantity))
                continue
            price = rate.unit_price if rate.pricing_method in ("per_unit", "hourly") else None
            amount = qty * rate.unit_price if price is not None else ZERO
            rated.append(Rated(ev, rate, qty, price, amount, ev.service_date, period_id))
            produced = True
        cannot.extend(problems)
        if not produced and not problems:
            cannot.append(CannotRate(ev, ev.source_ref, "no_rate", {
                "event_type": ev.event_type, "activity": activity, "service_date": ev.service_date.isoformat(),
                "fix": f"Add a rate for {activity or ev.event_type} valid from {ev.service_date.isoformat()}"},
                ev.customer_id, None, ev.quantity))
    return rated, cannot


@dataclass(frozen=True)
class CostSpec:
    """An expense line eligible for cost-based billing (cost_plus / pass_through)."""
    expense_line_id: int
    customer_id: int
    expense_category: str
    amount: Decimal
    period_id: int
    description: str


def rate_costs(costs: list[CostSpec], rates: list[RateSpec], period_start: dict) -> tuple[list[Rated], list[CannotRate]]:
    """Cost-based charges (7.3). cost_plus: amount = cost x (1 + markup %/100); pass_through: amount = cost.

    The rate is the one valid on the first day of the billing period. Eligible costs are the customer's
    direct expense lines whose category is in the rate's ``eligible_cost_categories``.
    """
    rated, cannot = [], []
    cost_rates = [r for r in rates if r.pricing_method in ("cost_plus", "pass_through")]
    for cost in costs:
        day = period_start[cost.period_id]
        cands = [r for r in cost_rates if r.customer_id == cost.customer_id and cost.expense_category in r.eligible_cost_categories]
        by_charge = defaultdict(list)
        for r in cands:
            by_charge[r.charge_code_id].append(r)
        for charge_id in sorted(by_charge):
            status, rate = select_rate(by_charge[charge_id], day, {})
            if status != "ok":
                cannot.append(CannotRate(None, f"expense_line:{cost.expense_line_id}", status, {
                    "charge_code": by_charge[charge_id][0].charge_code}, cost.customer_id, charge_id, cost.amount))
                continue
            factor = (1 + D(rate.markup_pct) / 100) if rate.pricing_method == "cost_plus" else Decimal(1)
            rated.append(Rated(None, rate, cost.amount, None, cost.amount * factor, day, cost.period_id, cost.expense_line_id))
    return rated, cannot


def rate_fixed_fees(rates: list[RateSpec], customers_periods: list[tuple[int, int, dt.date, dt.date, str]]) -> list[Rated]:
    """fixed_fee: the fee once per billing period for every approved fixed-fee rate valid on the period's first day
    (or, if none, its last day). ``customers_periods`` = (customer_id, period_id, start, end, period_type)."""
    out = []
    fees = [r for r in rates if r.pricing_method == "fixed_fee"]
    for customer_id, period_id, start, end, ptype in customers_periods:
        by_charge = defaultdict(list)
        for r in fees:
            if r.customer_id == customer_id and r.billing_period == ptype:
                by_charge[r.charge_code_id].append(r)
        for charge_id in sorted(by_charge):
            status, rate = select_rate(by_charge[charge_id], start, {})
            if status != "ok":
                status, rate = select_rate(by_charge[charge_id], end, {})
            if status == "ok":
                out.append(Rated(None, rate, Decimal(1), rate.unit_price, rate.unit_price, start, period_id))
    return out
