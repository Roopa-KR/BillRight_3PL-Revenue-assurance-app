"""P8 consistency: each KPI tile equals the total of the table it opens, the API and the Excel export; chart rules (9.4)."""
import io
import re
from decimal import Decimal as Dec

import openpyxl
from sqlalchemy import select

from billright import models as m
from billright.metrics import service as ms
from billright.security import load_scope
from billright.web import charts
from tests.conftest import actor


def _xlsx_rows(data, sheet=None):
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    hdr_idx = next(i for i, r in enumerate(rows) if r and r[0] is None) + 1  # header after the blank row
    header = rows[hdr_idx]
    return header, rows[hdr_idx + 1:], rows[:hdr_idx]


def _money(text):
    return Dec(text.replace("$", "").replace(",", "").replace("−", "-"))


def test_customers_kpis_table_api_excel(world, client, session_for):
    c = client("controller@bsp-illustrative.example")
    site = world["b"]["site"]
    s = session_for(world["b"]["tenant"])
    period = s.execute(select(m.BillingPeriod)).scalars().first()
    html = c.get(f"/customers?period={period.id}").data.decode()
    tiles = re.findall(r'<div class="v">([^<]+)</div><div class="l">([^<]+?) <span', html)
    tiles = {l.strip(): v for v, l in tiles}
    api = c.get(f"/api/v1/customers?site={site}&period={period.id}").get_json()
    rev = sum(Dec(r["revenue"]) for r in api["rows"])
    cost = sum(Dec(r["total_cost"]) for r in api["rows"]) + Dec(api["site"]["unassigned_cost"])
    assert _money(tiles["Revenue"]) == rev == Dec(api["site"]["revenue"])
    assert _money(tiles["Total cost"]) == cost == Dec(api["site"]["total_cost"])
    assert _money(tiles["Site profit"]) == rev - cost == Dec(api["site"]["site_profit"])
    assert tiles["Site margin"] == "16.6%"
    hdr, rows, meta = _xlsx_rows(c.get("/exports/customer_pnl").data)
    site_row = next(r for r in rows if r[0] == "Site total")
    assert Dec(str(site_row[2])) == rev and Dec(str(site_row[5])) == cost and Dec(str(site_row[6])) == rev - cost
    assert any(r[0] == "Data label" and "Illustrative" in r[1] for r in meta)


def test_costs_kpis_matrix_api_excel(world, client, session_for):
    c = client("controller@bsp-illustrative.example")
    site = world["b"]["site"]
    api = c.get(f"/api/v1/costs?site={site}").get_json()
    alloc = sum(sum(Dec(v) for v in r["cells"].values()) for r in api["matrix"])
    assert Dec(api["allocated"]) == alloc and Dec(api["total"]) == alloc + Dec(api["unassigned"])
    html = c.get("/costs").data.decode()
    tiles = {l.strip(): v for v, l in re.findall(r'<div class="v">([^<]+)</div><div class="l">([^<]+?) <span', html)}
    assert _money(tiles["Total operating cost"]) == Dec(api["total"]) and _money(tiles["Allocated to customers"]) == alloc
    hdr, rows, _ = _xlsx_rows(c.get("/exports/expense_allocation").data, "Allocation matrix")
    pool_idx = hdr.index("Pool total")
    assert sum(Dec(str(r[pool_idx])) for r in rows) == Dec(api["total"])


def test_leakage_tiles_table_api_excel(world, client):
    c = client("analyst@gini-synthetic.example")
    site = world["a"]["site"]
    w38 = re.search(r'<option value="(\d+)"[^>]*>2026-W38', c.get("/").data.decode()).group(1)
    c.get(f"/?period={w38}")
    api = c.get(f"/api/v1/leakage?site={site}&period={w38}").get_json()
    open_rows = [r for r in api["rows"] if r["status"] in ("new", "under_review") and not r["carried"] and r["direction"] == "under"]
    k = c.get(f"/api/v1/kpis/analyst?site={site}&period={w38}").get_json()
    assert Dec(k["open_amount"]) == sum(Dec(r["gap"]) for r in open_rows)
    html = c.get("/").data.decode()
    assert f"${Dec(k['open_amount']):,.0f}" in html
    hdr, rows, _ = _xlsx_rows(c.get("/exports/leakage_register").data)
    gap_i = hdr.index("Gap")
    assert sum(Dec(str(r[gap_i])) for r in rows) == Dec(api["totals"]["gap"])


def test_reconciliation_page_api_excel(world, client):
    c = client("analyst@gini-synthetic.example")
    site = world["a"]["site"]
    w38 = re.search(r'<option value="(\d+)"[^>]*>2026-W38', c.get("/").data.decode()).group(1)
    c.get(f"/?period={w38}")
    api = c.get(f"/api/v1/reconciliation?site={site}&period={w38}").get_json()
    assert Dec(api["totals"]["billable"]) == sum(Dec(r["billable"]) for r in api["rows"])
    hdr, rows, _ = _xlsx_rows(c.get("/exports/reconciliation").data)
    tot = next(r for r in rows if r[0] == "Site total")
    assert Dec(str(tot[4])) == Dec(api["totals"]["billable"]) and Dec(str(tot[5])) == Dec(api["totals"]["billed"])
    html = c.get("/reconciliation").data.decode()
    assert f"${Dec(api['totals']['billable']):,.2f}" in html


def _check_chart(ch):
    assert ch["title"] and any(ch_ in ch["title"] for ch_ in ("$", "%", "largest", "lowest", "takes", "invoiced", "Margin"))
    lay = ch["figure"]["layout"]
    assert lay["xaxis"]["title"]["text"] is not None and lay["yaxis"]["title"]["text"]
    for tr in ch["figure"]["data"]:
        assert tr.get("hovertemplate") or tr.get("hoverinfo") == "skip"
        assert tr["type"] not in ("pie", "indicator") and "yaxis2" not in lay
    assert ch["table"]["columns"] and ch["table"]["rows"]
    named = [t for t in ch["figure"]["data"] if t.get("name")]
    assert lay.get("showlegend") == (len(named) > 1)


def test_charts_rules_and_empty_states(world, session_for):
    s = session_for(world["b"]["tenant"])
    a = actor(world, "b", "controller")
    scope = load_scope(s, a.tenant_id, a.user_id)
    site = world["b"]["site"]
    p = s.execute(select(m.BillingPeriod)).scalars().first()
    pnl = ms.customers_pnl(s, scope, site, p.id)
    _check_chart(charts.c1(pnl["rows"]))
    d = ms.customer_detail(s, scope, site, p.id, pnl["rows"][0]["customer_id"])
    _check_chart(charts.c2(d["row"], d["categories"]))
    k = ms.costs(s, scope, site, p.id)
    _check_chart(charts.k1(k["k1"], k["total"]))
    # insufficient data -> hidden with a one-line explanation
    assert charts.h1(ms.recovery_by_period(s, scope, site, p.id))["hidden"]  # one period only
    assert charts.c3(d["trend"])["reason"] == "Trend appears after 3 closed periods."
    assert charts.l1([])["hidden"]
    sa = session_for(world["a"]["tenant"])
    aa = actor(world, "a", "controller")
    sc = load_scope(sa, aa.tenant_id, aa.user_id)
    pa = sa.execute(select(m.BillingPeriod).where(m.BillingPeriod.label == "2026-W38")).scalars().first()
    l1 = charts.l1(ms.leakage_by_rule(ms.leakage_rows(sa, sc, world["a"]["site"], pa.id)))
    _check_chart(l1)
    h1 = charts.h1([dict(period="W1", confirmed=Dec(100), invoiced=Dec(50)), dict(period="W2", confirmed=Dec(0), invoiced=Dec(0))])
    _check_chart(h1)
    c3 = charts.c3([dict(period=f"P{i}", margin=Dec(10 + i), site_margin=Dec(12)) for i in range(3)])
    _check_chart(c3)
