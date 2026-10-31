"""Unit tests for the pure calculation modules (billright/engine)."""
import datetime as dt
from decimal import Decimal as Dec

import pytest

from billright.engine import allocation as A
from billright.engine import labor as L
from billright.engine.billable import build_billable_lines, price_tiers
from billright.engine.leakage import InvoiceLineSpec, reconcile, tolerance_exceeded
from billright.engine.money import D, largest_remainder, round_cents
from billright.engine.pnl import customer_pnl, ledger_by_recipient
from billright.engine.rating import (Conversion, CostSpec, EventSpec, RateSpec, UomConverter, normalise_condition, rate_costs, rate_events,
                                     rate_fixed_fees, select_rate)
from billright.engine.resolution import resolve
from billright.engine.storage import storage_quantities

D0 = dt.date(2026, 1, 1)


def rate(id=1, customer=1, charge=10, code="LINE", price="1.196", uom="line", method="per_unit", event="pick_line", act=None, cond=None,
         vf=D0, vt=None, status="approved", vfrom=None, vto=None, minimum=None, tiers=(), tier_method="none", markup=None, cats=(), category="picking"):
    return RateSpec(id=id, rate_card_version_id=id, version_no=1, version_status=status, version_from=vfrom, version_to=vto, contract_reference="C",
                    customer_id=customer, charge_code_id=charge, charge_code=code, category=category, event_type=event, activity_code=act,
                    pricing_method=method, unit_price=D(price) if price is not None else None, uom=uom, markup_pct=D(markup) if markup else None,
                    eligible_cost_categories=tuple(cats), minimum_charge=D(minimum) if minimum else None, billing_period="week",
                    tier_method=tier_method, tiers=tuple(tiers), condition=normalise_condition(cond), valid_from=vf, valid_to=vt)


def ev(i=1, day=dt.date(2026, 9, 8), qty="1", uom="line", customer=1, attrs=None, event="pick_line"):
    return EventSpec(i, f"E{i}", event, day, D(qty) if qty is not None else None, uom, None, attrs or {}, customer)


# ------------------------------------------------------------------ money (P2)
def test_float_rejected():
    with pytest.raises(TypeError):
        D(1.5)


def test_round_once_half_up():
    assert round_cents(D(5631) * D("1.196")) == D("6734.68")  # 6734.676
    assert round_cents(D("363.285")) == D("363.29")
    assert round_cents(D("-0.005")) == D("-0.01")


def test_largest_remainder_adds_up():
    parts = largest_remainder(D("100.00"), {"a": 1, "b": 1, "c": 1})
    assert sum(parts.values()) == D("100.00") and sorted(parts.values()) == [D("33.33"), D("33.33"), D("33.34")]
    neg = largest_remainder(D("-10.00"), {"a": 1, "b": 2})
    assert sum(neg.values()) == D("-10.00")


# ------------------------------------------------------------------ rate selection and rating (7.2, 7.3, P6)
def test_rate_selected_by_service_date_not_today():
    old = rate(id=1, price="1.00", status="superseded", vfrom=D0, vto=dt.date(2026, 9, 10))
    new = rate(id=2, price="1.20", vfrom=dt.date(2026, 9, 10))
    st, r = select_rate([old, new], dt.date(2026, 9, 9), {})
    assert st == "ok" and r.id == 1
    st, r = select_rate([old, new], dt.date(2026, 9, 10), {})
    assert r.id == 2


def test_ambiguous_and_no_rate():
    a, b = rate(id=1), rate(id=2)
    assert select_rate([a, b], dt.date(2026, 9, 9), {})[0] == "ambiguous_rate"
    assert select_rate([rate(vf=dt.date(2027, 1, 1))], dt.date(2026, 9, 9), {})[0] == "no_rate"


def test_conditional_surcharge_applies_only_when_condition_matches():
    base = rate(id=1, charge=10, act="PICK_COOLER")
    sur = rate(id=2, charge=11, code="COLD", price="0.40", act="PICK_COOLER", category="surcharge", cond={"temperature_class": "refrigerated"})
    conv = UomConverter([])
    rated, cannot = rate_events([ev(1, attrs={"activity": "PICK_COOLER", "temperature_class": "refrigerated"}),
                                 ev(2, attrs={"activity": "PICK_COOLER", "temperature_class": "ambient"})], [base, sur], conv, lambda d: 1)
    assert sorted((r.event.id, r.rate.charge_code_id) for r in rated) == [(1, 10), (1, 11), (2, 10)]
    assert not cannot


def test_cannot_rate_reasons():
    conv = UomConverter([])
    rated, cannot = rate_events([ev(1, customer=None), ev(2, qty=None), ev(3, uom="case", attrs={"activity": "X"}),
                                 ev(4, attrs={"activity": "OTHER"})], [rate(act="X")], conv, lambda d: 1)
    assert {c.reason for c in cannot} == {"no_customer", "missing_quantity", "no_uom_conversion", "no_rate"}
    assert not rated


def test_uom_conversion_material_first_and_reverse():
    conv = UomConverter([Conversion(None, "case", "each", D(12), D0, None), Conversion("M1", "case", "each", D(6), D0, None)])
    assert conv.convert(D(2), "case", "each", "M1", dt.date(2026, 9, 1)) == D(12)
    assert conv.convert(D(2), "case", "each", "M2", dt.date(2026, 9, 1)) == D(24)
    assert conv.convert(D(24), "each", "case", "M2", dt.date(2026, 9, 1)) == D(2)


def test_cost_plus_and_pass_through():
    cp = rate(id=1, charge=20, method="cost_plus", price=None, uom=None, event=None, markup="10", cats=["transport_carriers"])
    rated, _ = rate_costs([CostSpec(1, 1, "transport_carriers", D("30000"), 1, "carrier")], [cp], {1: D0})
    assert rated[0].amount == D("33000.0")
    pt = rate(id=2, charge=21, method="pass_through", price=None, uom=None, event=None, cats=["transport_carriers"])
    rated, _ = rate_costs([CostSpec(1, 1, "transport_carriers", D("30000"), 1, "carrier")], [pt], {1: D0})
    assert rated[0].amount == D("30000")


def test_fixed_fee_once_per_period():
    ff = rate(id=1, charge=30, method="fixed_fee", price="500", uom=None, event=None)
    out = rate_fixed_fees([ff], [(1, 7, dt.date(2026, 9, 7), dt.date(2026, 9, 13), "week")])
    assert len(out) == 1 and out[0].amount == D("500")


def test_billable_rounds_once_and_minimum():
    r = rate(price="1.196")
    conv = UomConverter([])
    rated, _ = rate_events([ev(i, attrs={}) for i in range(5631)], [r], conv, lambda d: 1)
    bl = build_billable_lines(rated)[(1, 10, 1)]
    assert bl.unrounded == D("6734.676") and bl.amount == D("6734.68")
    r2 = rate(price="1", minimum="50")
    rated, _ = rate_events([ev(1)], [r2], conv, lambda d: 1)
    b = build_billable_lines(rated)[(1, 10, 1)]
    assert b.amount == D("50.00") and b.minimum_applied


def test_tiers_explicit_methods():
    tiers = [{"from": 0, "to": 100, "unit_price": "1.00"}, {"from": 100, "to": None, "unit_price": "0.50"}]
    assert price_tiers(D(150), tiers, "all_units")[0] == D("75.00")
    assert price_tiers(D(150), tiers, "incremental")[0] == D("125.00")
    with pytest.raises(ValueError):
        price_tiers(D(150), tiers, "none")


# ------------------------------------------------------------------ leakage rules (7.4)
TOL0 = {r: (D(0), D(0)) for r in ("L1", "L2", "L3", "L4", "L5", "L6", "L7")}


def _bl(qty, price, minimum=None, category="picking", history=()):
    r = rate(price=price, minimum=minimum, category=category)
    rated, _ = rate_events([ev(i) for i in range(qty)], [r], UomConverter([]), lambda d: 1)
    return build_billable_lines(rated), r


def _inv(qty, price, amount, i=1):
    return InvoiceLineSpec(i, 1, 10, "LINE", 1, D(qty), D(price), D(amount))


def test_L1_unbilled():
    bl, r = _bl(10, "2")
    row = reconcile(bl, [], {10: "picking"}, {}, set(), TOL0)[0]
    assert row.finding.rule_code == "L1" and row.gap_amount == D("20.00")


def test_L2_undercount_and_L7_overbilling():
    bl, r = _bl(10, "2")
    assert reconcile(bl, [_inv(8, 2, "16.00")], {10: "picking"}, {}, set(), TOL0)[0].finding.rule_code == "L2"
    row = reconcile(bl, [_inv(12, 2, "24.00")], {10: "picking"}, {}, set(), TOL0)[0]
    assert row.finding.rule_code == "L7" and row.finding.direction == "over" and row.gap_amount == D("-4.00")


def test_L3_wrong_rate_and_L6_expired_rate():
    bl, r = _bl(10, "2")
    assert reconcile(bl, [_inv(10, "1.5", "15.00")], {10: "picking"}, {}, set(), TOL0)[0].finding.rule_code == "L3"
    old = rate(id=99, price="1.5", status="superseded")
    row = reconcile(bl, [_inv(10, "1.5", "15.00")], {10: "picking"}, {(1, 10): [old, r]}, set(), TOL0)[0]
    assert row.finding.rule_code == "L6" and "L3" in row.finding.also_matches


def test_L4_surcharge_and_L5_minimum():
    bl, r = _bl(10, "0.4", category="surcharge")
    row = reconcile(bl, [_inv(5, "0.4", "2.00")], {10: "surcharge"}, {}, set(), TOL0)[0]
    assert row.finding.rule_code == "L4" and "L2" in row.finding.also_matches
    bl, r = _bl(1, "1", minimum="50")
    row = reconcile(bl, [_inv(1, "1", "1.00")], {10: "picking"}, {}, set(), TOL0)[0]
    assert row.finding.rule_code == "L5"


def test_tolerance_both_must_be_exceeded():
    assert not tolerance_exceeded(D("5"), D("1000"), D("10"), D("0"))
    assert not tolerance_exceeded(D("50"), D("1000"), D("10"), D("10"))  # 5% not above 10%
    assert tolerance_exceeded(D("150"), D("1000"), D("10"), D("10"))
    bl, r = _bl(10, "2")
    tol = dict(TOL0, L2=(D("10"), D("0")))
    row = reconcile(bl, [_inv(9, 2, "18.00")], {10: "picking"}, {}, set(), tol)[0]
    assert row.finding is None and row.note == "Within tolerance"


def test_rounding_difference_is_not_leakage():
    r = rate(price="2.4219")
    rated, _ = rate_events([ev(i) for i in range(150)], [r], UomConverter([]), lambda d: 1)
    bl = build_billable_lines(rated)
    row = reconcile(bl, [_inv(150, "2.4219", "363.28")], {10: "picking"}, {}, set(), TOL0)[0]
    assert row.billable_amount == D("363.29") and row.finding is None and row.status == "rounding"


# ------------------------------------------------------------------ allocation (7.6) and labor (7.7)
def test_supervisor_pool_documented_time():
    pools = A.allocate([A.ExpenseSpec(1, "supervisors", D("16000"), None, "direct_payroll")], {"supervisors": [A.DriverSpec(5, "supervisors", "documented_time_share")]},
                       {5: {1: D(40), 2: D(35), 3: D(25)}})
    p = pools["supervisors"]
    assert {r.customer_id: r.amount for r in p.rows} == {1: D("6400.00"), 2: D("5600.00"), 3: D("4000.00")} and p.reconciles


def test_no_driver_stays_unassigned_never_equal_split():
    pools = A.allocate([A.ExpenseSpec(1, "utilities", D("6000"), None, "facility_equipment_it_other")], {}, {})
    p = pools["utilities"]
    assert p.unassigned_amount == D("6000") and p.exception == "no_approved_driver" and p.allocated_amount == 0 and p.reconciles


def test_direct_first_and_unassigned_activity():
    pools = A.allocate([A.ExpenseSpec(1, "transport_carriers", D("30000"), 1, "carrier"), A.ExpenseSpec(2, "warehouse_operators", D("100.00"), None, "direct_payroll")],
                       {"warehouse_operators": [A.DriverSpec(7, "warehouse_operators", "paid_labor_hours_by_customer")]}, {7: {1: D(2), 2: D(1), None: D(1)}})
    assert pools["transport_carriers"].rows[0].method == "direct"
    op = pools["warehouse_operators"]
    assert op.unassigned_amount == D("25.00") and op.allocated_amount == D("75.00") and op.reconciles


def test_labor_op001():
    w = L.WorkerCost("OP001", "employee", "operator", D(160), D(4800))
    rows, err = L.attribute([w], [L.Attribution("OP001", 1, D(90), "task_time"), L.Attribution("OP001", 2, D(50), "task_time"), L.Attribution("OP001", 3, D(20), "task_time")])
    assert {r.customer_id: r.cost for r in rows} == {1: D("2700.00"), 2: D("1500.00"), 3: D("600.00")} and not err
    rows, err = L.attribute([w], [L.Attribution("OP001", 1, D(80), "task_time"), L.Attribution("OP001", 2, D(40), "task_time"),
                                  L.Attribution("OP001", 3, D(20), "task_time"), L.Attribution("OP001", None, D(20), "documented_time", "training")])
    un = [r for r in rows if r.customer_id is None]
    assert len(un) == 1 and un[0].hours == D(20) and un[0].cost == D("600.00") and un[0].unassigned_reason == "training"
    assert L.coverage(rows) == D("4200") / D("4800")


def test_labor_over_attribution_is_an_error():
    rows, err = L.attribute([L.WorkerCost("X", "employee", "operator", D(10), D(100))], [L.Attribution("X", 1, D(11), "task_time")])
    assert err and not rows


def test_proxy_completed_line_share():
    out = L.completed_line_share_proxy({"PICK": D("100.00")}, {(1, "PICK"): 2, (2, "PICK"): 1})
    assert out[(1, "PICK")] + out[(2, "PICK")] == D("100.00")


# ------------------------------------------------------------------ P&L (7.5, 7.8)
def test_ledger_reconciliation_and_pnl():
    exps = [A.ExpenseSpec(1, "supervisors", D(100), None, "direct_payroll"), A.ExpenseSpec(2, "utilities", D(50), None, "nope")]
    rec = ledger_by_recipient(exps)
    assert not rec["reconciles"] and rec["difference"] == D(50)
    pools = A.allocate([A.ExpenseSpec(1, "supervisors", D(100), None, "direct_payroll")], {}, {})
    rows, site = customer_pnl([1], {1: D(200)}, pools, {1: 0}, {}, D(10))
    assert rows[0].margin_pct == D(100) and rows[0].cost_per_order is None
    assert site.site_profit == D(100) and site.unassigned_cost == D(100) and site.profit_before_financing_tax == D(90)
    rows, site = customer_pnl([1], {}, pools, {}, {})
    assert rows[0].margin_pct is None  # revenue 0 -> Not available


# ------------------------------------------------------------------ resolution (7.1, 5.2)
class _E:
    def __init__(self, task, day=dt.date(2026, 9, 8), event_type="pick_line", attrs=None):
        self.task_ref, self.order_ref, self.service_date, self.event_type, self.attributes = task, None, day, event_type, attrs or {"clientid_tenant": "BSP"}


def test_join_path_and_ambiguity_clientid_never_used():
    path = {"pick_line": [{"from": "task_ref"}, {"table": "T", "match": "TASK", "take": "ORDER"}, {"table": "O", "match": "ORDER", "take": "CUST"},
                          {"identity_map": "customer_code"}]}
    idx = {("T", "TASK", "ORDER"): {"t1": {"o1"}, "t2": {"o1", "o2"}}, ("O", "ORDER", "CUST"): {"o1": {"C1"}, "o2": {"C2"}}}
    ident = {("customer_code", "C1"): [(1, D0, None)], ("customer_code", "C2"): [(2, D0, None)], ("customer_code", "BSP"): [(9, D0, None)]}
    resolved, unresolved = resolve([_E("t1"), _E("t2"), _E("t3")], path, idx, ident)
    assert resolved == {0: 1}
    assert {u.index: u.reason for u in unresolved} == {1: "more_than_one_customer", 2: "no_match"}


# ------------------------------------------------------------------ G1 storage engine
def test_storage_quantities_grace_and_split():
    snaps = [dict(customer="C", date=dt.date(2026, 8, d), unit_type="pallet", quantity=D(10), received_date=dt.date(2026, 8, 1)) for d in range(1, 16)]
    q = storage_quantities(snaps, dt.date(2026, 8, 1), dt.date(2026, 8, 30), dict(unit_type="pallet", method="average_daily", grace_days=0, split_month="prorate"))
    assert q["C"] == D(150) / D(30)
    q = storage_quantities(snaps, dt.date(2026, 8, 1), dt.date(2026, 8, 30), dict(unit_type="pallet", method="unit_days", grace_days=5))
    assert q["C"] == D(100)
