"""12.2 Fixture A acceptance (GiNi synthetic billing dataset; test settings: every tolerance $0.00 and 0%)."""
import csv
import subprocess
from collections import defaultdict
from decimal import Decimal as Dec
from pathlib import Path

from sqlalchemy import func, select

from billright import models as m
from billright.services.contracts import latest_run

ROOT = Path(__file__).resolve().parents[2]
TRUTH = ROOT / "tests" / "truth" / "out_billing_leakage.csv"
# The reference sheet rounded two lines with binary floating point (x.xx5 rounded down). P2 mandates Decimal and
# ROUND_HALF_UP, so BillRight's billable is one cent higher on exactly these lines (documented in docs/TEST_REPORT.md).
HALF_UP_DEVIATIONS = {("CUST-11", "2026-W38", "LINE_CAGE"): (Dec("363.28"), Dec("363.29")),
                      ("CUST-08", "2026-W37", "LINE_CAGE"): (Dec("294.97"), Dec("294.98"))}


def _ctx(world, session_for):
    s = session_for(world["a"]["tenant"])
    periods = {p.label: p for p in s.execute(select(m.BillingPeriod)).scalars() if p.label in ("2026-W37", "2026-W38")}
    custs = {c.id: c.code for c in s.execute(select(m.Customer)).scalars()}
    charges = {c.id: c.code for c in s.execute(select(m.ChargeCode)).scalars()}
    runs = {label: latest_run(s, p.id) for label, p in periods.items()}
    return s, periods, custs, charges, runs


def test_exactly_three_items_total_357_64(world, session_for):
    s, periods, custs, charges, runs = _ctx(world, session_for)
    items = s.execute(select(m.LeakageItem).where(m.LeakageItem.status != "resolved_by_data")).scalars().all()
    got = sorted((custs[i.customer_id], charges[i.charge_code_id], next(k for k, p in periods.items() if p.id == i.period_id), i.rule_code,
                  i.billable_quantity, i.billed_quantity, i.billable_amount, i.billed_amount, i.gap_amount) for i in items)
    assert got == sorted([
        ("CUST-02", "ORDER_FEE", "2026-W38", "L2", Dec("246"), Dec("221"), Dec("1074.90"), Dec("965.66"), Dec("109.24")),
        ("CUST-07", "COLD_CHAIN_SURCHARGE", "2026-W38", "L1", Dec("345"), Dec("0"), Dec("138.00"), Dec("0.00"), Dec("138.00")),
        ("CUST-07", "COLD_CHAIN_SURCHARGE", "2026-W37", "L1", Dec("276"), Dec("0"), Dec("110.40"), Dec("0.00"), Dec("110.40")),
    ])
    assert sum(Dec(i.gap_amount) for i in items) == Dec("357.64")
    o = next(i for i in items if charges[i.charge_code_id] == "ORDER_FEE")
    assert o.contract_unit_price == Dec("4.3695")


def test_billable_and_billed_totals_and_line_by_line(world, session_for):
    s, periods, custs, charges, runs = _ctx(world, session_for)
    rows = {}
    for label, run in runs.items():
        for r in s.execute(select(m.ReconLine).where(m.ReconLine.run_id == run.id)).scalars():
            rows[(custs[r.customer_id], label, charges[r.charge_code_id])] = r
    truth = list(csv.DictReader(open(TRUTH)))
    assert len(truth) == 192 and len(rows) == 192
    billable_total = billed_total = Dec(0)
    for t in truth:
        key = (t["customer_id"].replace("cust-", ""), t["period_id"].replace("prd-", ""), t["charge_code"])
        r = rows[key]
        assert Dec(r.billable_quantity) == Dec(t["billable_qty"]), key
        assert Dec(r.billed_quantity) == Dec(t["billed_qty"]), key
        assert Dec(r.billed_amount) == Dec(t["billed_amount"]).quantize(Dec("0.01")), key
        expected = Dec(t["billable_amount"]).quantize(Dec("0.01"))
        if key in HALF_UP_DEVIATIONS:
            ref, ours = HALF_UP_DEVIATIONS[key]
            assert expected == ref and Dec(r.billable_amount) == ours and r.status == "rounding"
        else:
            assert Dec(r.billable_amount) == expected, key
        billable_total += Dec(r.billable_amount)
        billed_total += Dec(r.billed_amount)
    assert billed_total == Dec("226753.95")
    # reference total 227,111.59 + 0.02 from the two half-up lines
    assert billable_total == Dec("227111.61")
    assert sum(Dec(t["billable_amount"]).quantize(Dec("0.01")) for t in truth) == Dec("227111.59")


def test_nothing_unrated_or_unresolved(world, session_for):
    s, periods, custs, charges, runs = _ctx(world, session_for)
    for run in runs.values():
        assert s.execute(select(func.count()).select_from(m.CannotRateItem).where(m.CannotRateItem.run_id == run.id)).scalar() == 0
        assert run.counts["events"] > 70000


def test_reference_sheet_never_read_by_application_code():
    out = subprocess.run(["grep", "-rIl", "-e", "out_billing_leakage", "-e", "tests/truth", str(ROOT / "billright")], capture_output=True, text=True)
    assert out.stdout.strip() == ""


def test_clientid_is_not_used_for_customers(world, session_for):
    s, *_ = _ctx(world, session_for)
    # every TI102C row carries CLIENTID=SYN (the tenant); customers still resolve to 12 distinct 3PL customers
    from sqlalchemy import text
    ev = s.execute(text("select attributes->>'clientid_tenant', count(*) from activity_event where event_type='pick_line' group by 1")).all()
    assert [tuple(r) for r in ev] == [("SYN", 148660)]
    run = latest_run(s, s.execute(select(m.BillingPeriod.id).where(m.BillingPeriod.label == "2026-W38")).scalar())
    assert s.execute(select(func.count(func.distinct(m.RatedEvent.customer_id))).where(m.RatedEvent.run_id == run.id)).scalar() == 12
