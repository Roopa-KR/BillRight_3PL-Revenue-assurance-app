"""Import behaviour (5.3) and the unresolved-customer queue (5.2)."""
from decimal import Decimal as Dec

from sqlalchemy import func, select

from billright import models as m
from billright.db import make_session
from billright.ingest.importer import import_file
from billright.services.run import run_period
from tests.conftest import actor


def test_identical_file_is_noop_and_validation_report(world):
    s = make_session(world["a"]["tenant"])
    site = s.get(m.Site, world["a"]["site"])
    adm = actor(world, "a", "admin")
    data = open("fixtures/fixture_a/01_customer_master_M140.csv", "rb").read()
    n = s.execute(select(func.count()).select_from(m.ImportBatch)).scalar()
    b = import_file(s, adm, site, "gini_customer_master", "01_customer_master_M140.csv", data)
    assert s.execute(select(func.count()).select_from(m.ImportBatch)).scalar() == n and b.id == 1
    bad = ("INVOICE_NUMBER,LINE_NUMBER,CUSTOMER_CODE,CHARGE_CODE,PERIOD,QUANTITY,UOM,UNIT_PRICE,AMOUNT\n"
           "X1,1,CUST-99,LINE_RX,2026-W39,1,line,1,1\n"            # unknown customer
           "X1,2,CUST-01,NOPE,2026-W39,1,line,1,1\n"               # unknown charge code
           "X1,3,CUST-01,LINE_RX,2030-W01,1,line,1,1\n"            # unknown period
           "X1,4,CUST-01,LINE_RX,2026-W39,abc,line,1,1\n"          # type
           "X1,4,CUST-01,LINE_RX,2026-W39,1,line,1,1\n"            # duplicate key (after the bad row 4? row 4 rejected, so this is accepted)
           "X1,5,CUST-01,LINE_RX,2026-W39,1,line,1,\n")            # required amount missing
    b = import_file(s, actor(world, "a", "analyst"), site, "invoice_lines", "bad.csv", bad.encode())
    s.commit()
    reasons = " | ".join(r.reason for r in s.execute(select(m.ImportRejectedRow).where(m.ImportRejectedRow.import_batch_id == b.id)).scalars())
    assert b.rejected_count == 5 and b.accepted_count == 1
    for frag in ("unknown customer CUST-99", "unknown charge code NOPE", "unknown period 2030-W01", "'abc' is not a number", "AMOUNT is required"):
        assert frag in reasons
    assert "1 row(s) accepted, 5 row(s) rejected." == b.report["summary"]
    missing = import_file(s, actor(world, "a", "analyst"), site, "invoice_lines", "missing.csv", b"INVOICE_NUMBER,AMOUNT\nA,1\n")
    assert missing.status == "rejected" and "missing" in missing.report["summary"]
    neg = import_file(s, adm, site, "gini_order_line", "neg.csv", b"ORDER_LINE_ID,ORDER_ID,ORDERED_QTY\nL1,O1,-5\n")
    assert neg.status == "rejected"
    s.rollback()
    s.close()


def test_unresolved_customer_is_excluded_and_queued(world, client):
    s = make_session(world["a"]["tenant"])
    site = s.get(m.Site, world["a"]["site"])
    w39 = s.execute(select(m.BillingPeriod).where(m.BillingPeriod.label == "2026-W39")).scalar_one()
    csv_ = ("TI102ID,TI102X3ID,CLIENTID,WHID,WORKTYPE,MATERIALID,SRCLOC,RESOURCENAME,WORKER_ID,PROCESSEDQTY,UOM,CREATEDATETIME,COMPLETED_AT\n"
            "99999901,ORPHAN-1,SYN,100,1,427452,J1,x,W0001,1,each,2026-09-22T08:00:00-07:00,2026-09-22T09:00:00-07:00\n")
    b = import_file(s, actor(world, "a", "admin"), site, "gini_ti102c", "orphan_task.csv", csv_.encode())
    assert b.status == "accepted" and b.report["events_created"] == 1
    s.commit()
    run = run_period(s, actor(world, "a", "controller"), w39.id)
    s.commit()
    un = s.execute(select(m.CannotRateItem).where(m.CannotRateItem.run_id == run.id, m.CannotRateItem.reason == "no_customer")).scalars().all()
    assert [u.source_ref for u in un] == ["TI102C:99999901"] and un[0].details["resolution"] == "no_match"
    assert s.execute(select(func.count()).select_from(m.RatedEvent).where(m.RatedEvent.run_id == run.id, m.RatedEvent.activity_event_id.is_not(None))).scalar() == 0
    html = client("analyst@gini-synthetic.example").get(f"/data?tab=unresolved&period={w39.id}").data.decode()
    assert "TI102C:99999901" in html
    s.close()
