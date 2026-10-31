"""12.2 Fixture B acceptance: the BSP worked example (illustrative), reproduced through normal calculations."""
from decimal import Decimal as Dec

from sqlalchemy import select

from billright import models as m
from billright.metrics import service as ms
from billright.security import load_scope
from billright.services.contracts import latest_run
from tests.conftest import actor


def _b(world, session_for, who="controller"):
    s = session_for(world["b"]["tenant"])
    a = actor(world, "b", who)
    scope = load_scope(s, a.tenant_id, a.user_id)
    period = s.execute(select(m.BillingPeriod)).scalars().first()
    return s, a, scope, period


def test_expense_ledger_and_payment_recipients(world, session_for):
    s, a, scope, p = _b(world, session_for)
    d = ms.costs(s, scope, world["b"]["site"], p.id)
    assert d["total"] == Dec("256000")
    by_cat = {r["category"]: r["amount"] for r in d["k1"]}
    assert by_cat == {"warehouse_operators": Dec(80000), "forklift_operators": Dec(16000), "supervisors": Dec(16000), "managers": Dec(10000),
                      "administration": Dec(8000), "transport_carriers": Dec(60000), "warehouse_rent": Dec(30000), "utilities": Dec(6000),
                      "equipment": Dec(7000), "packaging": Dec(9000), "software": Dec(4000), "other_overhead": Dec(10000)}
    assert len(by_cat) == 12
    who = {w["recipient"]: w["amount"] for w in d["who"]}
    assert who == {"direct_payroll": Dec(106000), "staffing_agency": Dec(24000), "carrier": Dec(60000), "facility_equipment_it_other": Dec(66000)}
    assert d["ledger_reconciles"] and d["recipients_total"] == Dec(256000)
    ops = [x for x in d["lines"] if x.expense_category == "warehouse_operators"]
    assert sorted((x.payment_recipient_type, Dec(x.amount)) for x in ops) == [("direct_payroll", Dec(56000)), ("staffing_agency", Dec(24000))]


def test_allocation(world, session_for):
    s, a, scope, p = _b(world, session_for)
    d = ms.costs(s, scope, world["b"]["site"], p.id)
    code = {c["id"]: c["code"] for c in d["customers"]}
    sup = next(r for r in d["matrix"] if r["category"] == "supervisors")
    assert {code[k]: v for k, v in sup["cells"].items()} == {"RETAILCO": Dec(6400), "FOODCO": Dec(5600), "PARTSCO": Dec(4000)}
    totals = {code[c]: sum(r["cells"][c] for r in d["matrix"]) for c in code}
    assert totals == {"RETAILCO": Dec(119650), "FOODCO": Dec(76950), "PARTSCO": Dec(59400)}
    assert all(r["check"] for r in d["matrix"]) and sum(totals.values()) == Dec(256000) and d["unassigned"] == 0


def test_retailco_invoice_rated_from_rate_card(world, session_for):
    s, a, scope, p = _b(world, session_for)
    run = latest_run(s, p.id)
    cust = s.execute(select(m.Customer).where(m.Customer.code == "RETAILCO")).scalar_one()
    charges = {c.id: c.code for c in s.execute(select(m.ChargeCode)).scalars()}
    lines = {charges[b.charge_code_id]: (Dec(b.billable_quantity), Dec(b.billable_amount)) for b in
             s.execute(select(m.BillableLine).where(m.BillableLine.run_id == run.id, m.BillableLine.customer_id == cust.id)).scalars()}
    assert lines == {"RECEIVING": (Dec(1000), Dec(12000)), "STORAGE": (Dec(3000), Dec(36000)), "PICKING": (Dec(12000), Dec(36000)),
                     "PACKING": (Dec(10000), Dec(20000)), "TRANSPORT": (Dec(30000), Dec(33000)), "SPECIAL_PROJECTS": (Dec(200), Dec(8000))}
    assert sum(v[1] for v in lines.values()) == Dec(145000)


def test_customer_pnl(world, session_for):
    s, a, scope, p = _b(world, session_for)
    pnl = ms.customers_pnl(s, scope, world["b"]["site"], p.id)
    got = {r["customer"]: (r["revenue"], r["total_cost"], r["site_profit"], ms.r1(r["margin_pct"])) for r in pnl["rows"]}
    assert got == {"RETAILCO": (Dec(145000), Dec(119650), Dec(25350), Dec("17.5")), "FOODCO": (Dec(90000), Dec(76950), Dec(13050), Dec("14.5")),
                   "PARTSCO": (Dec(72000), Dec(59400), Dec(12600), Dec("17.5"))}
    st = pnl["site"]
    assert (st["revenue"], st["total_cost"], st["site_profit"], ms.r1(st["margin_pct"])) == (Dec(307000), Dec(256000), Dec(51000), Dec("16.6"))


def test_foodco_lowest_margin_with_data_supported_insight(world, session_for, client):
    s, a, scope, p = _b(world, session_for)
    pnl = ms.customers_pnl(s, scope, world["b"]["site"], p.id)
    assert pnl["rows"][0]["customer"] == "FOODCO"
    from billright.web.charts import c1
    assert c1(pnl["rows"])["title"] == "FoodCo has the lowest margin: 14.5%"
    d = ms.customer_detail(s, scope, world["b"]["site"], p.id, pnl["rows"][0]["customer_id"])
    ins = d["insight"]
    assert ins["applies"] and "14.5%" in ins["what"] and "16.6%" in ins["what"]
    # In the illustrative data FoodCo's measured drivers match the site, so no cause is claimed
    assert ins["why"] == ["Cause not established from the data."]
    assert "task mix" in ins["check"]
    html = client("controller@bsp-illustrative.example").get("/customers").data.decode()
    assert "FoodCo has the lowest margin: 14.5%" in html


def test_labor_op001(world, session_for):
    s, a, scope, p = _b(world, session_for)
    lab = ms.labor_attribution(s, scope, world["b"]["site"], p.id)
    op = {r["customer"]: (r["hours"], r["cost"]) for r in lab["rows"] if r["worker"] == "OP001"}
    assert op == {"RETAILCO": (Dec(90), Dec(2700)), "FOODCO": (Dec(50), Dec(1500)), "PARTSCO": (Dec(20), Dec(600))}
    assert sum(v[1] for v in op.values()) == Dec(4800)
