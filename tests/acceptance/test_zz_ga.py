"""GA features G1-G8, each additive to Release 1."""
import datetime as dt
from decimal import Decimal as Dec

import pytest
from sqlalchemy import func, select

from billright import models as m
from billright.db import make_session
from billright.ga import features as ga
from billright.ingest.importer import import_file
from billright.security import load_scope
from billright.services import admin as admin_svc
from billright.services import contracts
from billright.services.common import BusinessError, set_setting
from billright.services.run import run_period
from tests.conftest import actor


def _a(world):
    s = make_session(world["a"]["tenant"])
    return s, {p.label: p.id for p in s.execute(select(m.BillingPeriod)).scalars()}


def test_g1_storage_from_daily_snapshots(world):
    s = make_session(world["b"]["tenant"])
    site = s.get(m.Site, world["b"]["site"])
    adm, ctl = actor(world, "b", "admin"), actor(world, "b", "controller")
    sep = s.execute(select(m.BillingPeriod).where(m.BillingPeriod.label == "2026-09")).scalar_one()
    rows = ["SNAPSHOT_DATE,CUSTOMER_CODE,UNIT_TYPE,QUANTITY,RECEIVED_DATE,LOCATION"]
    for d in range(1, 31):
        rows.append(f"2026-09-{d:02d},RETAILCO,pallet,100,2026-08-01,A")
        if d >= 16:
            rows.append(f"2026-09-{d:02d},FOODCO,pallet,60,2026-09-{d:02d},B")  # received the same day: inside a 1-day grace
    b = import_file(s, adm, site, "inventory_snapshot", "inv_2026_09.csv", ("\n".join(rows) + "\n").encode())
    assert b.status == "accepted"
    scope = load_scope(s, ctl.tenant_id, ctl.user_id)
    assert not ga.storage_preview(s, scope, site.id, sep.id)["available"]  # policy setting not filled: no invented default
    set_setting(s, adm, "storage.policy", {"unit_type": "pallet", "method": "average_daily", "output_uom": "pallet_month", "grace_days": 1,
                                           "split_month": "prorate"}, site.id)
    prev = ga.storage_preview(s, scope, site.id, sep.id)
    assert dict(prev["rows"]) == {"RETAILCO": Dec(100)}
    batch = ga.generate_storage_events(s, adm, site.id, sep.id)
    s.commit()
    run = run_period(s, ctl, sep.id)
    s.commit()
    storage = s.execute(select(m.BillableLine).join(m.ChargeCode).where(m.BillableLine.run_id == run.id, m.ChargeCode.code == "STORAGE")).scalar_one()
    assert storage.billable_quantity == Dec(100) and storage.billable_amount == Dec("1200.00")
    s.close()


def test_g3_daily_run_alerts_analysts(world, client):
    s, p = _a(world)
    n = ga.daily_run(s, actor(world, "a", "controller"), world["a"]["site"], today=dt.date(2026, 9, 25))
    s.commit()
    alerts = s.execute(select(m.Alert).where(m.Alert.kind == "daily_leakage")).scalars().all()
    analysts = s.execute(select(func.count()).select_from(m.UserRole).where(m.UserRole.role == "billing_analyst")).scalar()
    assert n >= 0 and all(a.link.startswith("/leakage/") for a in alerts)
    assert s.execute(select(func.count()).select_from(m.LeakageItem).where(m.LeakageItem.status == "new", m.LeakageItem.alerted_at.is_(None))).scalar() == 0
    html = client("analyst@gini-synthetic.example").get("/alerts").data.decode()
    assert "Alerts" in html
    s.close()


def test_g4_surcharge_rule_flags_and_l4(world):
    s, p = _a(world)
    adm, ctl, ctl2 = actor(world, "a", "admin"), actor(world, "a", "controller"), actor(world, "a", "controller2")
    cc = admin_svc.create_charge_code(s, adm, "WEEKEND_SURCHARGE", "Weekend surcharge", "surcharge", "line", "pick_line")
    with pytest.raises(BusinessError, match="Parameters missing"):
        ga.create_surcharge_rule(s, adm, world["a"]["site"], "Weekend picks", "weekend", "pick_line", cc.id, {})
    rule = ga.create_surcharge_rule(s, adm, world["a"]["site"], "Weekend picks", "weekend", "pick_line", cc.id, {"weekdays": [5, 6]})
    with pytest.raises(BusinessError, match="Segregation"):
        ga.approve_surcharge_rule(s, adm, rule.id)
    ga.approve_surcharge_rule(s, ctl, rule.id)
    k = s.execute(select(m.Contract).join(m.Customer).where(m.Customer.code == "CUST-01")).scalar_one()
    v = contracts.new_draft(s, adm, k.id, "Weekend surcharge added")
    contracts.save_rate(s, adm, v.id, dict(charge_code_id=cc.id, pricing_method="per_unit", unit_price=Dec("0.10"), uom="line", billing_period="week",
                                           condition={"surcharge:WEEKEND_SURCHARGE": True}, valid_from=dt.date(2026, 9, 21), currency_code="USD"))
    contracts.submit(s, adm, v.id, dt.date(2026, 9, 21))
    contracts.approve(s, ctl2, v.id)
    s.commit()
    # a Saturday pick in W39 for CUST-01: the extract resolves through the normal join path (task link added)
    site = s.get(m.Site, world["a"]["site"])
    import_file(s, adm, site, "gini_order_header", "w39_orders.csv",
                b"ORDER_ID,ORDER_CODE,CUSTOMER_CODE,COMPLETED_AT\nSO-W39-1,SO-W39-1,CUST-01,\n")
    import_file(s, adm, site, "gini_order_line", "w39_lines.csv", b"ORDER_LINE_ID,ORDER_ID,LINE_NUMBER\nSO-W39-1-1,SO-W39-1,1\n")
    import_file(s, adm, site, "gini_task_order_line", "w39_links.csv", b"TASK_ID,ORDER_LINE_ID\n99999902,SO-W39-1-1\n")
    import_file(s, adm, site, "gini_ti102c", "w39_sat.csv",
                b"TI102ID,TI102X3ID,CLIENTID,WORKTYPE,MATERIALID,PROCESSEDQTY,UOM,COMPLETED_AT\n99999902,WH-W39,SYN,1,427452,1,each,2026-09-26T10:00:00-07:00\n")
    s.commit()
    run = run_period(s, ctl, p["2026-W39"])
    s.commit()
    rated = s.execute(select(m.RatedEvent).where(m.RatedEvent.run_id == run.id, m.RatedEvent.charge_code_id == cc.id)).scalars().all()
    assert len(rated) == 1 and rated[0].amount_unrounded == Dec("0.10")
    item = s.execute(select(m.LeakageItem).where(m.LeakageItem.charge_code_id == cc.id)).scalar_one()
    assert item.rule_code == "L1" and "L4" in item.also_matches
    s.close()


def test_g5_minimums_and_tiers_checker(world):
    s, p = _a(world)
    ctl = actor(world, "a", "controller")
    scope = load_scope(s, ctl.tenant_id, ctl.user_id)
    assert ga.minimums_tiers(s, scope, world["a"]["site"], p["2026-W38"]) == []  # Fixture A rates have no minimums or tiers
    s.close()


def test_g6_contract_drift(world):
    s, p = _a(world)
    adm, ctl = actor(world, "a", "admin"), actor(world, "a", "controller")
    k = s.execute(select(m.Contract).join(m.Customer).where(m.Customer.code == "CUST-05")).scalar_one()
    contracts.update_contract(s, adm, k.id, "Renewal date recorded", end_date=dt.date(2026, 10, 31))
    scope = load_scope(s, ctl.tenant_id, ctl.user_id)
    drift = ga.contract_drift(s, scope, world["a"]["site"], p["2026-W38"])
    assert any(d["kind"] == "renewal" and d["customer"] == "CUST-05" for d in drift)
    assert ga.drift_alerts(s, ctl, world["a"]["site"], p["2026-W38"]) >= 1
    s.rollback()
    s.close()


def test_g7_sla_exposure_vs_booked(world):
    s, p = _a(world)
    ctl = actor(world, "a", "controller")
    scope = load_scope(s, ctl.tenant_id, ctl.user_id)
    d = ga.sla_exposure(s, scope, world["a"]["site"], p["2026-W38"])
    assert d["available"] and len(d["rows"]) == 12
    for r in d["rows"]:
        assert r["exposure"] <= (r["cap"] or r["exposure"]) and r["booked"] == 0 and r["difference"] == r["exposure"]
    assert d["total_exposure"] == sum(r["exposure"] for r in d["rows"])
    s.close()


def test_g8_accounting_export(world, client):
    c = client("controller@gini-synthetic.example")
    s, p = _a(world)
    r = c.post("/ga/accounting", data={}, query_string={"period": p["2026-W38"]})
    assert r.status_code == 302
    e = s.execute(select(m.AccountingExport).order_by(m.AccountingExport.id.desc())).scalars().first()
    body = e.content.decode()
    assert body.splitlines()[0].startswith("DOCUMENT_TYPE,CUSTOMER_CODE") and "invoice_adjustment" in body and "LKG-" in body
    again = c.post("/ga/accounting", data={}, query_string={"period": p["2026-W38"]})
    assert again.status_code == 409  # already exported: nothing left
    with pytest.raises(BusinessError, match="extension point"):
        ga.ADAPTERS["netsuite"]().push([], {})
    s.close()
