"""Fixture B lifecycle: central overhead, unassigned training hours, period close, frozen re-run, rate approval rules, reopen."""
import datetime as dt
from decimal import Decimal as Dec

import pytest
from sqlalchemy import select

from billright import models as m
from billright.db import make_session
from billright.ingest.importer import import_file
from billright.metrics import service as ms
from billright.security import load_scope
from billright.services import admin as admin_svc
from billright.services import close as close_svc
from billright.services import contracts
from billright.services.common import BusinessError
from billright.services.run import run_period
from tests.conftest import actor


def _ctx(world):
    s = make_session(world["b"]["tenant"])
    ctl = actor(world, "b", "controller")
    return s, ctl, load_scope(s, ctl.tenant_id, ctl.user_id), s.execute(select(m.BillingPeriod).where(m.BillingPeriod.label == "2026-08")).scalar_one()


def test_central_overhead_profit_before_financing_and_tax(world):
    s, ctl, scope, p = _ctx(world)
    admin_svc.enter_central_overhead(s, ctl, world["b"]["site"], p.id, Dec("12000"), "Corporate overhead (illustrative)")
    s.commit()
    run_period(s, ctl, p.id)
    s.commit()
    site = ms.customers_pnl(s, scope, world["b"]["site"], p.id)["site"]
    assert site["site_profit"] == Dec(51000) and site["profit_before_financing_tax"] == Dec(39000)
    s.close()


def test_training_hours_stay_unassigned(world):
    s, ctl, scope, p = _ctx(world)
    site = s.get(m.Site, world["b"]["site"])
    data = open("fixtures/fixture_b/11b_labor_time_attribution_with_training.csv", "rb").read()
    b = import_file(s, actor(world, "b", "admin"), site, "labor_time_attribution", "11_labor_time_attribution.csv", data)
    assert b.status == "accepted" and b.report.get("supersedes")
    s.commit()
    lab = ms.labor_attribution(s, scope, world["b"]["site"], p.id)  # no run yet: uses active batches
    run_period(s, ctl, p.id)
    s.commit()
    lab = ms.labor_attribution(s, scope, world["b"]["site"], p.id)
    op = [r for r in lab["rows"] if r["worker"] == "OP001"]
    un = [r for r in op if r["customer"] is None]
    assert len(un) == 1 and un[0]["hours"] == Dec(20) and un[0]["cost"] == Dec(600) and un[0]["reason"] == "training"
    assert sum(r["cost"] for r in op if r["customer"]) == Dec(4200)  # 140 attributed hours x $30; not re-spread by picks
    s.close()


def test_close_freeze_verify_and_reopen(world, client):
    s, ctl, scope, p = _ctx(world)
    checks = close_svc.run_checks(s, ctl, scope, p.id)
    status = {c.no: c.passed for c in checks}
    assert status == {1: True, 2: True, 3: True, 4: True, 5: True, 6: False, 7: True}  # 6: accepted manual entries need acknowledgement
    with pytest.raises(BusinessError, match="blocked"):
        close_svc.close_period(s, ctl, scope, p.id)
    close_svc.acknowledge(s, ctl, p.id, 6, "Special-project hours reviewed with operations")
    close_svc.close_period(s, ctl, scope, p.id)
    s.commit()
    assert p.status == "closed"
    # frozen: no runs, imports, decisions or manual entries on the closed period
    with pytest.raises(BusinessError, match="closed"):
        run_period(s, ctl, p.id)
    site = s.get(m.Site, world["b"]["site"])
    b = import_file(s, actor(world, "b", "admin"), site, "expense_ledger", "late.csv",
                    b"REF,PERIOD,EXPENSE_CATEGORY,DESCRIPTION,AMOUNT,SOURCE_TYPE,PAYMENT_RECIPIENT_TYPE\nLATE,2026-08,utilities,late bill,10,manual,facility_equipment_it_other\n")
    assert b.status == "rejected" and "closed" in b.report["top_reasons"][0]["reason"]
    s.rollback()
    # re-running the closed period gives identical results
    v = run_period(s, ctl, p.id, "verify")
    s.commit()
    assert v.counts["matches_close_run"] is True and v.result_hash == s.get(m.CalculationRun, p.close_run_id).result_hash
    html = client("controller@bsp-illustrative.example").get("/close").data.decode()
    assert "Closed" in html and "Identical to the close run" in html
    s.close()


def test_rate_approval_rules_open_periods_only(world):
    s, ctl, scope, p = _ctx(world)
    adm, ctl2 = actor(world, "b", "admin"), actor(world, "b", "controller2")
    nxt = admin_svc.create_period(s, adm, world["b"]["site"], "2026-09", dt.date(2026, 9, 1), dt.date(2026, 9, 30), "month")
    k = s.execute(select(m.Contract).join(m.Customer).where(m.Customer.code == "RETAILCO")).scalar_one()
    v = contracts.new_draft(s, adm, k.id, "Picking price increase")
    pick = s.execute(select(m.Rate).join(m.ChargeCode).where(m.Rate.rate_card_version_id == v.id, m.ChargeCode.code == "PICKING")).scalar_one()
    data = {f: getattr(pick, f) for f in contracts.RATE_FIELDS}
    data.update(unit_price=Dec("3.25"))
    contracts.save_rate(s, adm, v.id, data, pick.id)
    bad = dict(data, charge_code_id=pick.charge_code_id)
    with pytest.raises(BusinessError, match="overlaps"):
        contracts.save_rate(s, adm, v.id, bad)
    contracts.submit(s, adm, v.id, effective_from=dt.date(2026, 8, 15))
    with pytest.raises(BusinessError, match="closed period"):
        contracts.approve(s, ctl, v.id)
    v.status, v.effective_from = "draft", None
    contracts.submit(s, adm, v.id, effective_from=dt.date(2026, 9, 1))
    # the author cannot approve: make the controller the author of a second draft to check segregation of duties
    v2 = contracts.new_draft(s, ctl, k.id, "Controller-authored draft")
    contracts.submit(s, ctl, v2.id, effective_from=dt.date(2026, 10, 1))
    with pytest.raises(BusinessError, match="Segregation"):
        contracts.approve(s, ctl, v2.id)
    contracts.reject(s, ctl2, v2.id, "Not needed")
    contracts.approve(s, ctl, v.id)
    s.commit()
    # the closed August period is unchanged by the approved change
    vr = run_period(s, ctl, p.id, "verify")
    s.commit()
    assert vr.counts["matches_close_run"] is True
    old = s.execute(select(m.RateCardVersion).where(m.RateCardVersion.contract_id == k.id, m.RateCardVersion.version_no == 1)).scalar_one()
    assert old.status == "superseded" and old.superseded_from == dt.date(2026, 9, 1)
    s.close()


def test_reopen_needs_reason_and_creates_new_version(world):
    s, ctl, scope, p = _ctx(world)
    with pytest.raises(BusinessError, match="reason"):
        close_svc.reopen_period(s, ctl, p.id, "")
    close_svc.reopen_period(s, ctl, p.id, "Late carrier credit note")
    s.commit()
    assert p.status == "open" and p.close_version == 1 and p.close_run_id is None
    log = s.execute(select(m.AuditLog).where(m.AuditLog.action == "period.reopen")).scalar_one()
    assert log.reason == "Late carrier credit note"
    s.close()
