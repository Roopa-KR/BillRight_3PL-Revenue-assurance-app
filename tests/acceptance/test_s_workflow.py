"""Leakage review workflow (8.3), manual entries (8.4), adjustments export and recovery tracking, evidence (F8)."""
import datetime as dt
import io
from decimal import Decimal as Dec

import pytest
from sqlalchemy import select

from billright import models as m
from billright.db import make_session
from billright.ingest.importer import import_file
from billright.metrics import service as ms
from billright.services import leakage as L
from billright.services import work
from billright.services.common import BusinessError
from billright.services.run import run_period
from tests.conftest import actor


def _items(world):
    s = make_session(world["a"]["tenant"])
    charges = {c.code: c.id for c in s.execute(select(m.ChargeCode)).scalars()}
    periods = {p.label: p.id for p in s.execute(select(m.BillingPeriod)).scalars()}
    q = lambda code, label: s.execute(select(m.LeakageItem).where(m.LeakageItem.charge_code_id == charges[code], m.LeakageItem.period_id == periods[label])).scalar_one()
    return s, q, periods


def test_every_item_opens_to_events_invoices_and_labor(world, session_for):
    s = session_for(world["a"]["tenant"])
    a = actor(world, "a", "analyst")
    from billright.security import load_scope
    scope = load_scope(s, a.tenant_id, a.user_id)
    for it in s.execute(select(m.LeakageItem)).scalars():
        ev = ms.evidence(s, scope, it.id)
        assert ev["total_events"] == int(it.billable_quantity)
        assert ev["rate"] is not None and ev["contract"].reference.startswith("MSA-")
        assert all(e["source_ref"] and e["service_date"] and e["order_ref"] for e in ev["events"])
        assert ev["labor_source"] and any(e["labor_minutes"] is not None for e in ev["events"])
        if it.rule_code == "L2":
            assert len(ev["invoices"]) == 1 and ev["invoices"][0]["quantity"] == Dec(221)
        elif it.rule_code == "L1":
            assert ev["invoices"] == []
        else:
            assert ev["invoices"]


def test_decision_rules(world):
    s, q, periods = _items(world)
    analyst, ctl, ctl2 = actor(world, "a", "analyst"), actor(world, "a", "controller"), actor(world, "a", "controller2")
    order_fee = q("ORDER_FEE", "2026-W38")       # 109.24 > threshold 100 -> needs approval
    cold37 = q("COLD_CHAIN_SURCHARGE", "2026-W37")  # 110.40
    cold38 = q("COLD_CHAIN_SURCHARGE", "2026-W38")  # 138.00
    with pytest.raises(BusinessError, match="waive reason"):
        L.decide(s, analyst, cold37.id, "waive")
    with pytest.raises(BusinessError, match="reason is required"):
        L.decide(s, analyst, cold37.id, "not_billable")
    with pytest.raises(BusinessError, match="Choose one of"):
        L.decide(s, analyst, cold37.id, "issue_credit")
    # back-billing window: 90 days from 13 Sep 2026
    with pytest.raises(BusinessError, match="back-billing window"):
        L.decide(s, analyst, cold37.id, "bill_now", today=dt.date(2027, 1, 1))
    L.decide(s, analyst, order_fee.id, "bill_now", reason_text="Order count under-reported", today=dt.date(2026, 10, 1))
    assert order_fee.status == "decided" and L.needs_approval(s, analyst, order_fee)
    with pytest.raises(BusinessError, match="Segregation"):
        L.approve(s, analyst, order_fee.id)
    L.approve(s, ctl, order_fee.id)
    assert order_fee.status == "approved" and order_fee.approved_by == ctl.user_id
    L.decide(s, analyst, cold38.id, "bill_next_cycle", today=dt.date(2026, 10, 1))
    L.approve(s, ctl2, cold38.id)
    L.decide(s, analyst, cold37.id, "waive", "below_materiality", today=dt.date(2026, 10, 1))
    L.approve(s, ctl, cold37.id)
    s.commit()
    hist = s.execute(select(m.AuditLog).where(m.AuditLog.entity == "leakage_item", m.AuditLog.entity_id == str(order_fee.id))).scalars().all()
    assert {"leakage.decide", "leakage.approve"} <= {h.action for h in hist}
    s.close()


def test_carry_and_adjustment_export_and_recovery(world, client):
    c = client("controller@gini-synthetic.example")
    s, q, periods = _items(world)
    # 'Bill next cycle' from W38 carries into the W39 queue until invoiced
    rows = ms.leakage_rows(s, _scope(s, world), world["a"]["site"], periods["2026-W39"])
    assert [r["charge"] for r in rows if r["carried"]] == ["COLD_CHAIN_SURCHARGE"]
    # adjustment file: only approved Bill now / Issue credit
    data = c.get(f"/exports/adjustments?format=csv&period={periods['2026-W38']}").data.decode()
    body = [l for l in data.splitlines() if not l.startswith("#")]
    assert body[0].startswith("Customer,Invoice reference") and len(body) == 2
    of = q("ORDER_FEE", "2026-W38")
    assert f"LKG-{of.id}" in body[1] and "INV-02-W38" in body[1] and "109.24" in body[1]
    # a later invoice line carrying ADJUSTMENT_REF marks it invoiced (recovered); it is not counted as W39 billing
    site = s.get(m.Site, world["a"]["site"])
    csv_ = ("INVOICE_NUMBER,LINE_NUMBER,CUSTOMER_CODE,CHARGE_CODE,PERIOD,QUANTITY,UOM,UNIT_PRICE,AMOUNT,POSTED_ON,INVOICE_DATE,ADJUSTMENT_REF\n"
            f"INV-02-W39-ADJ,1,CUST-02,ORDER_FEE,2026-W39,25,order,4.3695,109.24,2026-09-29,2026-09-29,LKG-{of.id}\n")
    b = import_file(s, actor(world, "a", "analyst"), site, "invoice_lines", "adjustments_w39.csv", csv_.encode())
    assert b.status == "accepted"
    s.commit()
    run_period(s, actor(world, "a", "controller"), periods["2026-W39"])
    s.commit()
    s.refresh(of)
    assert of.invoiced_amount == Dec("109.24")
    pm = ms.period_leakage_metrics(s, _scope(s, world), world["a"]["site"], periods["2026-W38"])
    assert pm["confirmed"] == Dec("247.24") and pm["invoiced"] == Dec("109.24") and pm["recovery_rate"] == Dec("109.24") / Dec("247.24")
    assert pm["precision"] == 1
    w39 = s.execute(select(m.LeakageItem).where(m.LeakageItem.period_id == periods["2026-W39"])).scalars().all()
    assert w39 == []  # the adjustment line is not treated as over-billing in W39
    s.close()


def _scope(s, world):
    from billright.security import load_scope
    a = actor(world, "a", "controller")
    return load_scope(s, a.tenant_id, a.user_id)


def test_over_billing_flagged_as_credit(world):
    """P9: a re-issued invoice that bills more than the contract raises L7 with credit decisions."""
    s, q, periods = _items(world)
    site = s.get(m.Site, world["a"]["site"])
    src = open("fixtures/fixture_a/13_invoice_lines_2026-W37_W38.csv").read().splitlines()
    out = [src[0]]
    for line in src[1:]:
        if line.startswith("INV-01-W37,1,"):  # LINE_RX 5631 @ 1.196 -> bill 5700
            line = "INV-01-W37,1,CUST-01,LINE_RX,2026-W37,5700,line,1.196,6817.20,2026-09-15,2026-09-15,"
        out.append(line)
    b = import_file(s, actor(world, "a", "analyst"), site, "invoice_lines", "invoices_w37_w38_v2.csv", ("\n".join(out) + "\n").encode())
    assert b.status == "accepted" and b.report.get("supersedes")
    s.commit()
    run_period(s, actor(world, "a", "controller"), periods["2026-W37"])
    s.commit()
    it = s.execute(select(m.LeakageItem).where(m.LeakageItem.direction == "over")).scalar_one()
    assert it.rule_code == "L7" and it.gap_amount == Dec("-82.52") and L.allowed_decisions(it) == L.OVER_DECISIONS
    L.decide(s, actor(world, "a", "analyst"), it.id, "issue_credit", today=dt.date(2026, 10, 1))
    s.commit()
    # earlier W37 decisions survive the re-run
    assert q("COLD_CHAIN_SURCHARGE", "2026-W37").decision == "waive"
    s.close()


def test_manual_entry_workflow(world):
    s = make_session(world["a"]["tenant"])
    sup, analyst = actor(world, "a", "supervisor"), actor(world, "a", "analyst")
    cust = s.execute(select(m.Customer).where(m.Customer.code == "CUST-03")).scalar_one()
    cc = s.execute(select(m.ChargeCode).where(m.ChargeCode.code == "VAS_RELABEL")).scalar_one()
    with pytest.raises(BusinessError, match="note"):
        work.submit_entry(s, sup, world["a"]["site"], cust.id, cc.id, 10, "unit", dt.date(2026, 9, 22), None, None, "")
    e = work.submit_entry(s, sup, world["a"]["site"], cust.id, cc.id, 40, "unit", dt.date(2026, 9, 22), None, None, "Relabelled 40 cartons", work_order_ref="WO-1")
    with pytest.raises(BusinessError, match="own entry"):
        work.review_entry(s, sup, e.id, True)
    with pytest.raises(BusinessError, match="reason"):
        work.review_entry(s, analyst, e.id, False)
    work.review_entry(s, analyst, e.id, True)
    s.commit()
    # no VAS rate yet -> accepted entry is visible as Cannot rate (no_rate), not silently dropped
    pid = s.execute(select(m.BillingPeriod.id).where(m.BillingPeriod.label == "2026-W39")).scalar()
    run = run_period(s, actor(world, "a", "controller"), pid)
    s.commit()
    cr = s.execute(select(m.CannotRateItem).where(m.CannotRateItem.run_id == run.id)).scalars().all()
    assert [(c.source_ref, c.reason) for c in cr] == [(f"MANUAL:{e.id}", "no_rate")]
    s.close()
