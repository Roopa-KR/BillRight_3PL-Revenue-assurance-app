"""UI smoke: every page renders for the roles that may see it; headers carry site, period, data-as-of, run and data label."""
import pytest
from sqlalchemy import select

from billright import models as m
from billright.db import make_session

ANALYST = ["/", "/leakage", "/leakage?rule=L1", "/reconciliation", "/customers", "/contracts", "/work", "/data", "/data?tab=unresolved",
           "/data?tab=cannot", "/data?tab=freshness", "/ga", "/alerts"]
CONTROLLER = ["/", "/leakage", "/reconciliation", "/customers", "/costs", "/costs/labor", "/contracts", "/close", "/data", "/admin/drivers", "/ga",
              "/costs/cell?category=supervisors"]
ADMIN = ["/admin", "/admin?tab=settings", "/admin?tab=charges", "/admin?tab=periods", "/admin?tab=drivers", "/admin?tab=audit", "/data", "/contracts"]


@pytest.mark.parametrize("email,pages", [("analyst@gini-synthetic.example", ANALYST), ("controller@gini-synthetic.example", CONTROLLER),
                                         ("controller@bsp-illustrative.example", CONTROLLER), ("admin@bsp-illustrative.example", ADMIN),
                                         ("manager@bsp-illustrative.example", ["/", "/customers", "/contracts"]),
                                         ("supervisor@gini-synthetic.example", ["/work"])])
def test_pages_render(world, client, email, pages):
    c = client(email)
    for p in pages:
        r = c.get(p, follow_redirects=True)
        assert r.status_code == 200, (email, p)
        html = r.data.decode()
        assert "Data as of" in html and ("Synthetic data" in html or "Illustrative data" in html), p
        assert "Traceback" not in html


def test_detail_pages(world, client):
    c = client("analyst@gini-synthetic.example")
    s = make_session(world["a"]["tenant"])
    for i in s.execute(select(m.LeakageItem.id)).scalars():
        html = c.get(f"/leakage/{i}").data.decode()
        assert "What happened" in html and "Why we think so" in html and "What to do" in html
    v = s.execute(select(m.RateCardVersion.id)).scalars().first()
    assert c.get(f"/contracts/version/{v}").status_code == 200
    b = s.execute(select(m.ImportBatch.id)).scalars().first()
    assert c.get(f"/data/batch/{b}").status_code == 200
    cust = s.execute(select(m.Customer.id)).scalars().first()
    assert c.get(f"/customers/{cust}").status_code == 200
    s.close()


def test_mobile_work_capture_markup(world, client):
    html = client("supervisor@gini-synthetic.example").get("/work").data.decode()
    assert 'name="viewport"' in html and 'capture="environment"' in html and 'class="btn big"' in html
    assert '<label for="q">' in html


def test_empty_state_says_what_to_do(world, client):
    c = client("admin@gini-synthetic.example")
    from billright.services import admin as admin_svc
    from tests.conftest import actor
    s = make_session(world["a"]["tenant"])
    import datetime as dt
    p = s.execute(select(m.BillingPeriod).where(m.BillingPeriod.label == "2026-W39")).scalar_one_or_none()
    if p is None:
        p = admin_svc.create_period(s, actor(world, "a", "admin"), world["a"]["site"], "2026-W39", dt.date(2026, 9, 21), dt.date(2026, 9, 27), "week")
        s.commit()
    pid = p.id
    s.close()
    a = client("analyst@gini-synthetic.example")
    html = a.get(f"/reconciliation?period={pid}").data.decode()
    assert "No run for this period yet" in html and "Import data" in html
