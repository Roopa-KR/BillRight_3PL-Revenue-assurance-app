"""P11 / F11: tenant and customer isolation through pages, exports and the API; roles; RLS; append-only audit."""
import psycopg
import pytest
from sqlalchemy import select, text

from billright import models as m
from billright.db import make_session
from billright.services import admin as admin_svc
from tests.conftest import APP, TEST_DB, actor

PAGES = ["/", "/leakage", "/reconciliation", "/customers", "/costs", "/costs/labor", "/contracts", "/work", "/close", "/data", "/admin", "/ga", "/alerts",
         "/admin/drivers"]
EXPORTS = ["adjustments", "leakage_register", "reconciliation", "customer_pnl", "expense_allocation", "close_report", "receivables_payables", "pilot_results"]


@pytest.fixture(scope="module")
def scoped_user(world):
    """A billing analyst of tenant A who may only see CUST-01."""
    s = make_session(world["a"]["tenant"])
    cust = s.execute(select(m.Customer).where(m.Customer.code == "CUST-01")).scalar_one()
    u = s.execute(select(m.AppUser).where(m.AppUser.email == "scoped@gini-synthetic.example")).scalar_one_or_none()
    if u is None:
        u = admin_svc.create_user(s, actor(world, "a", "admin"), "scoped@gini-synthetic.example", "Scoped Analyst", ["billing_analyst"],
                                  [(world["a"]["site"], cust.id)])
        s.commit()
    uid, cid = u.id, cust.id
    s.close()
    return dict(email="scoped@gini-synthetic.example", customer_id=cid, user_id=uid)


def test_customer_scope_on_pages_api_exports(world, client, scoped_user):
    c = client(scoped_user["email"])
    site = world["a"]["site"]
    api = c.get(f"/api/v1/leakage?site={site}").get_json()
    assert api["rows"] == []  # the 3 items belong to CUST-02 and CUST-07
    w38 = s_period(world, "2026-W38")
    rec = c.get(f"/api/v1/reconciliation?site={site}&period={w38}").get_json()
    assert rec["rows"] and {r["customer"] for r in rec["rows"]} == {"CUST-01"}
    for p in ("/leakage", "/reconciliation", "/customers", "/data?tab=cannot"):
        html = c.get(p).data.decode()
        assert "CUST-07" not in html and "CUST-02" not in html, p
    for e in ("leakage_register", "reconciliation", "adjustments"):
        data = c.get(f"/exports/{e}").data
        assert b"CUST-07" not in data and b"CUST-02" not in data
    s = make_session(world["a"]["tenant"])
    items = s.execute(select(m.LeakageItem.id)).scalars().all()
    other = s.execute(select(m.Customer.id).where(m.Customer.code == "CUST-07")).scalar()
    s.close()
    for i in items:
        assert c.get(f"/leakage/{i}").status_code == 404
        assert c.get(f"/api/v1/leakage/{i}/evidence").status_code in (403, 404, 409)
    assert c.get(f"/customers/{other}").status_code in (403, 404, 409)
    assert c.get(f"/api/v1/customers/{other}?site={site}").status_code in (403, 404, 409)
    assert c.get(f"/reconciliation/cell?customer={other}&charge=1").status_code in (403, 404, 409)
    assert c.get("/costs").status_code == 403


def test_cross_tenant_access_is_impossible(world, client):
    c = client("controller@gini-synthetic.example")
    site_b = world["b"]["site"]
    for url in (f"/api/v1/customers?site={site_b}", f"/api/v1/costs?site={site_b}", f"/api/v1/leakage?site={site_b}", f"/api/v1/reconciliation?site={site_b}",
                f"/api/v1/charts/C1?site={site_b}", f"/api/v1/data/health?site={site_b}", f"/api/v1/periods?site={site_b}"):
        assert c.get(url).status_code == 403, url
    sb = make_session(world["b"]["tenant"])
    cust_b = sb.execute(select(m.Customer.id)).scalars().first()
    batch_b = sb.execute(select(m.ImportBatch.id)).scalars().first()
    sb.close()
    assert c.get(f"/customers/{cust_b}").status_code in (403, 404, 409)
    assert c.get(f"/data/batch/{batch_b}").status_code == 404
    assert c.get(f"/customers?site={site_b}").status_code == 200  # falls back to the user's own site
    assert b"BSP" not in c.get(f"/customers?site={site_b}").data


def test_rls_blocks_other_tenant_rows_at_database_level(world):
    url = APP.replace("postgresql+psycopg", "postgresql") + "/" + TEST_DB
    with psycopg.connect(url) as conn:
        conn.execute("select set_config('app.tenant_id', %s, false)", (str(world["a"]["tenant"]),))
        own = conn.execute("select count(*) from customer").fetchone()[0]
        conn.execute("select set_config('app.tenant_id', %s, false)", (str(world["b"]["tenant"]),))
        other = conn.execute("select count(*) from customer").fetchone()[0]
        conn.execute("select set_config('app.tenant_id', '-1', false)")
        none = conn.execute("select count(*) from customer").fetchone()[0]
        assert own == 12 and other == 3 and none == 0
        conn.execute("select set_config('app.tenant_id', %s, false)", (str(world["a"]["tenant"]),))
        with pytest.raises(psycopg.Error):
            conn.execute("insert into customer(tenant_id, site_id, code, name, active) values (%s, %s, 'X', 'X', true)",
                         (world["b"]["tenant"], world["b"]["site"]))


def test_audit_log_and_snapshots_append_only(world):
    s = make_session(world["a"]["tenant"])
    with pytest.raises(Exception):
        s.execute(text("update audit_log set reason='x'"))
        s.flush()
    s.rollback()
    with pytest.raises(Exception):
        s.execute(text("delete from source_snapshot where id in (select id from source_snapshot limit 1)"))
    s.rollback()
    s.close()


ROLE_ACCESS = {
    "analyst": {"/leakage": 200, "/reconciliation": 200, "/costs": 403, "/close": 403, "/admin": 403, "/costs/labor": 403},
    "manager": {"/leakage": 403, "/reconciliation": 403, "/customers": 200, "/costs": 403, "/costs/labor": 403, "/admin": 403, "/work": 403},
    "supervisor": {"/leakage": 403, "/customers": 403, "/work": 200, "/admin": 403, "/costs": 403},
    "admin": {"/admin": 200, "/leakage": 403, "/close": 403, "/data": 200},
    "controller": {"/leakage": 200, "/costs": 200, "/costs/labor": 200, "/close": 200, "/admin": 403},
}


@pytest.mark.parametrize("role", list(ROLE_ACCESS))
def test_role_permissions_per_page(world, client, role):
    c = client(f"{role}@bsp-illustrative.example")
    for page, code in ROLE_ACCESS[role].items():
        assert c.get(page).status_code == code, (role, page)


def test_account_manager_sees_no_worker_level_detail(world, client):
    c = client("manager@bsp-illustrative.example")
    assert c.get(f"/api/v1/labor?site={world['b']['site']}").status_code == 403
    html = c.get("/customers").data.decode() + c.get("/").data.decode()
    assert "OP001" not in html and "W00" not in html
    for e in ("expense_allocation", "pilot_results", "close_report", "receivables_payables"):
        assert c.get(f"/exports/{e}").status_code == 403


def test_csrf_required(world, client):
    c = client("analyst@gini-synthetic.example")
    r = c.c.post("/leakage/assign", data={"item": "1", "owner": "1"})
    assert r.status_code == 400


def test_dev_login_disabled_outside_development(world, monkeypatch):
    from billright.config import Config
    from billright.web import create_app
    monkeypatch.setattr(Config, "APP_ENV", "production")
    monkeypatch.setattr(Config, "SECRET_KEY", "x" * 32)
    app = create_app(testing=False)
    c = app.test_client()
    page = c.get("/login").data
    assert b"Development sign-in" not in page


def s_period(world, label):
    s = make_session(world["a"]["tenant"])
    pid = s.execute(select(m.BillingPeriod.id).where(m.BillingPeriod.label == label)).scalar()
    s.close()
    return pid
