"""JSON API (/api/v1/...) for every table and chart; same permissions and scope as the pages; same metrics service."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from flask import Blueprint, abort, g, jsonify, request

from .. import models as m
from ..metrics import service as ms
from ..web import charts
from ..web.auth import login_required

bp = Blueprint("api", __name__, url_prefix="/api/v1")


def js(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): js(x) for k, x in v.items() if not hasattr(x, "__table__")}
    if isinstance(v, (list, tuple, set)):
        return [js(x) for x in v]
    if hasattr(v, "__table__"):
        return {c.key: js(getattr(v, c.key)) for c in v.__table__.columns}
    return v


def _sp():
    site = request.args.get("site", type=int)
    period = request.args.get("period", type=int)
    if site is None:
        abort(400, "site is required")
    if not g.cur.scope.site_allowed(site):
        abort(403)
    if period is None:
        ps = ms.periods(g.s, g.cur.scope, site)
        with_run = [p for p in ps if ms.period_run(g.s, p) is not None]
        period = (with_run or ps)[-1].id if (with_run or ps) else None
    if period is not None:
        p = g.s.get(m.BillingPeriod, period)
        if p is None or p.site_id != site:
            abort(404)
    return site, period


@bp.route("/me")
@login_required()
def me():
    return jsonify(user=g.cur.user.email, roles=g.cur.roles, sites=sorted(g.cur.scope.site_ids))


@bp.route("/periods")
@login_required()
def periods():
    site, _ = _sp()
    return jsonify([js(p) for p in ms.periods(g.s, g.cur.scope, site)])


@bp.route("/leakage")
@login_required("view.leakage")
def leakage():
    site, period = _sp()
    rows = ms.leakage_rows(g.s, g.cur.scope, site, period, customer_id=request.args.get("customer"), rule=request.args.get("rule"),
                           status=request.args.get("status"), direction=request.args.get("direction"), amount_above=request.args.get("amount_above"))
    return jsonify(rows=[js({k: v for k, v in r.items() if k not in ("item", "customer")}) for r in rows],
                   totals=js(dict(billable=sum((r["billable"] for r in rows), Decimal(0)), billed=sum((r["billed"] for r in rows), Decimal(0)),
                                  gap=sum((r["gap"] for r in rows), Decimal(0)), count=len(rows))))


@bp.route("/leakage/<int:item_id>/evidence")
@login_required("view.evidence|view.leakage")
def evidence(item_id):
    ev = ms.evidence(g.s, g.cur.scope, item_id)
    return jsonify(item=js(ev["item"]), events=js(ev["events"]), total_events=ev["total_events"], invoices=js(ev["invoices"]),
                   rate=js(ev["rate"]) if ev["rate"] else None, contract_reference=ev["contract"].reference if ev["contract"] else None)


@bp.route("/kpis/<page>")
@login_required()
def kpis(page):
    site, period = _sp()
    sc = g.cur.scope
    if page == "analyst" and g.cur.can("view.leakage"):
        k = ms.analyst_kpis(g.s, sc, site, period)
        return jsonify(js({x: k[x] for x in ("open_amount", "open_count", "carried_amount", "cannot_rate", "manual_to_review")}))
    if page == "controller" and g.cur.can("leakage.approve"):
        k = ms.controller_kpis(g.s, sc, site, period)
        return jsonify(js({x: k[x] for x in ("confirmed", "invoiced", "site_margin", "approvals_waiting")}))
    if page == "customers" and g.cur.can("view.customers"):
        p = ms.customers_pnl(g.s, sc, site, period)
        return jsonify(js(p["site"] or {}))
    if page == "costs" and g.cur.can("view.costs"):
        d = ms.costs(g.s, sc, site, period)
        return jsonify(js(dict(total=d["total"], allocated=d["allocated"], unassigned=d["unassigned"], exceptions=len(d["exceptions"]))))
    abort(403)


@bp.route("/reconciliation")
@login_required("view.billing")
def reconciliation():
    site, period = _sp()
    d = ms.reconciliation(g.s, g.cur.scope, site, period)
    return jsonify(rows=js(d["rows"]), totals=js(d["totals"]), by_customer=js(d.get("by_customer")), by_charge=js(d.get("by_charge")),
                   run_id=d["run"].id if d["run"] else None)


@bp.route("/customers")
@login_required("view.customers")
def customers():
    site, period = _sp()
    p = ms.customers_pnl(g.s, g.cur.scope, site, period)
    return jsonify(available=p["available"], rows=js(p["rows"]), site=js(p["site"]), run_id=p["run"].id if p.get("run") else None)


@bp.route("/customers/<int:customer_id>")
@login_required("view.customers")
def customer(customer_id):
    site, period = _sp()
    d = ms.customer_detail(g.s, g.cur.scope, site, period, customer_id)
    return jsonify(row=js(d["row"]), categories=js(d["categories"]), trend=js(d["trend"]), insight=js(d["insight"]))


@bp.route("/costs")
@login_required("view.costs")
def costs():
    site, period = _sp()
    d = ms.costs(g.s, g.cur.scope, site, period)
    return jsonify(total=js(d["total"]), allocated=js(d["allocated"]), unassigned=js(d["unassigned"]), who=js(d["who"]),
                   ledger_reconciles=d["ledger_reconciles"], exceptions=js(d["exceptions"]), k1=js(d["k1"]),
                   matrix=[js(dict(category=r["category"], cells={str(k): v for k, v in r["cells"].items()}, unassigned=r["unassigned"], pool=r["pool"],
                                   check=r["check"], method=r["method"])) for r in d["matrix"]])


@bp.route("/labor")
@login_required("view.labor_detail")
def labor():
    site, period = _sp()
    return jsonify(js(ms.labor_attribution(g.s, g.cur.scope, site, period)))


@bp.route("/data/health")
@login_required("view.data")
def data_health():
    site, period = _sp()
    d = ms.data_health(g.s, g.cur.scope, site, period)
    return jsonify(freshness=[js(dict(source=f["source"], label=f["label"], rows=f["rows"], status=f["status"],
                                      last=f["last"].created_at if f["last"] else None)) for f in d["freshness"]],
                   unresolved=d["unresolved_count"], cannot_rate=d["cannot_count"])


@bp.route("/pilot")
@login_required("export.finance")
def pilot():
    site, _ = _sp()
    return jsonify(js(ms.pilot_metrics(g.s, g.cur.scope, site)))


@bp.route("/charts/<cid>")
@login_required()
def chart(cid):
    site, period = _sp()
    sc = g.cur.scope
    cid = cid.upper()
    if cid == "H1" and g.cur.can("leakage.approve"):
        ch = charts.h1(ms.recovery_by_period(g.s, sc, site, period))
    elif cid == "L1" and g.cur.can("view.leakage"):
        ch = charts.l1(ms.leakage_by_rule(ms.leakage_rows(g.s, sc, site, period)))
    elif cid == "C1" and g.cur.can("view.customers"):
        ch = charts.c1(ms.customers_pnl(g.s, sc, site, period)["rows"])
    elif cid in ("C2", "C3") and g.cur.can("view.customers"):
        d = ms.customer_detail(g.s, sc, site, period, request.args.get("customer", type=int))
        ch = charts.c2(d["row"], d["categories"]) if cid == "C2" else charts.c3(d["trend"])
    elif cid == "K1" and g.cur.can("view.costs"):
        d = ms.costs(g.s, sc, site, period)
        ch = charts.k1(d["k1"], d["total"])
    else:
        abort(403)
    return jsonify(ch)
