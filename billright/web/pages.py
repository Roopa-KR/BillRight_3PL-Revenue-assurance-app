"""Server-rendered pages (9.2, 9.3). Every number comes from billright.metrics.service."""
from __future__ import annotations

import datetime as dt
import io
import json
import threading
from decimal import Decimal

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, send_file, session,
                   url_for)
from sqlalchemy import func, select

from .. import models as m
from ..engine.leakage import RULE_NAMES
from ..engine.money import D
from ..metrics import service as ms
from ..services import admin as admin_svc
from ..services import close as close_svc
from ..services import contracts as contract_svc
from ..services import leakage as leak_svc
from ..services import work as work_svc
from ..services.common import SETTINGS, BusinessError, get_setting, set_setting
from . import charts
from .auth import login_required

bp = Blueprint("pages", __name__)

NAV = [
    ("home", "Home", None, "/"),
    ("leakage", "Leakage review", "view.leakage", "/leakage"),
    ("reconciliation", "Billing reconciliation", "view.billing", "/reconciliation"),
    ("customers", "Customers", "view.customers", "/customers"),
    ("costs", "Costs and allocation", "view.costs", "/costs"),
    ("contracts", "Contracts and rates", "view.contracts|view.contracts.read", "/contracts"),
    ("work", "Work capture", "view.work_capture", "/work"),
    ("close", "Period close", "view.close", "/close"),
    ("data", "Data", "view.data", "/data"),
    ("admin", "Admin", "view.admin", "/admin"),
    ("ga", "GA tools", "view.billing|view.costs", "/ga"),
]


# ------------------------------------------------------------------ context
def _ctx():
    cur = g.cur
    sites = sorted(cur.scope.site_ids)
    if not sites:
        abort(403)
    site_id = request.args.get("site", type=int) or session.get("site_id")
    if site_id not in sites:
        site_id = sites[0]
    session["site_id"] = site_id
    ps = ms.periods(g.s, cur.scope, site_id)
    pid = request.args.get("period", type=int) or session.get(f"period_{site_id}")
    if pid not in {p.id for p in ps}:
        with_run = [p for p in ps if ms.period_run(g.s, p) is not None]
        pid = (with_run or ps or [None])[-1].id if (with_run or ps) else None
    session[f"period_{site_id}"] = pid
    hdr = ms.header(g.s, cur.scope, site_id, pid)
    hdr["sites"] = [g.s.get(m.Site, x) for x in sites]
    nav = [n for n in NAV if n[2] is None or any(cur.can(p) for p in n[2].split("|"))]
    alerts = 0
    if cur.can("view.leakage") or cur.can("view.alerts"):
        alerts = g.s.execute(select(func.count()).select_from(m.Alert).where(m.Alert.site_id == site_id, m.Alert.read_at.is_(None),
                                                                             (m.Alert.user_id == cur.user.id) | m.Alert.user_id.is_(None))).scalar()
    return dict(cur=cur, hdr=hdr, site_id=site_id, period_id=pid, period=hdr["period"], nav=nav, alerts=alerts)


def render(tpl, active, **kw):
    c = _ctx() if "c" not in kw else kw.pop("c")
    return render_template(tpl, c=c, active=active, rule_names=RULE_NAMES, **kw)


def _commit_and_flash(msg):
    g.s.commit()
    flash(msg, "ok")


def _back(default="/"):
    ref = request.form.get("next") or request.referrer or default
    return redirect(ref if ref.startswith("/") or ref.startswith(request.host_url) else default)


# ------------------------------------------------------------------ home
@bp.route("/")
@login_required()
def home():
    cur = g.cur
    c = _ctx()
    if c["period_id"] is None:
        return render("pages/empty.html", "home", c=c, message="No billing periods are set up for this site.", link=("/admin", "Admin →"))
    if cur.can("leakage.update"):
        k = ms.analyst_kpis(g.s, cur.scope, c["site_id"], c["period_id"])
        prev = _prev_period(c)
        kprev = ms.analyst_kpis(g.s, cur.scope, c["site_id"], prev.id) if prev else None
        return render("pages/home_analyst.html", "home", c=c, k=k, kprev=kprev)
    if cur.can("leakage.approve"):
        k = ms.controller_kpis(g.s, cur.scope, c["site_id"], c["period_id"])
        prev = _prev_period(c)
        kprev = ms.controller_kpis(g.s, cur.scope, c["site_id"], prev.id) if prev else None
        chart = charts.h1(ms.recovery_by_period(g.s, cur.scope, c["site_id"], c["period_id"]))
        checks = close_svc.run_checks(g.s, cur.actor, cur.scope, c["period_id"])
        return render("pages/home_controller.html", "home", c=c, k=k, kprev=kprev, chart=chart, checks=checks)
    if cur.can("view.customers"):
        pnl = ms.customers_pnl(g.s, cur.scope, c["site_id"], c["period_id"])
        prev = _prev_period(c)
        pprev = ms.customers_pnl(g.s, cur.scope, c["site_id"], prev.id) if prev else None
        below = None
        if pnl["available"]:
            targets = [r for r in pnl["rows"] if r["target"] is not None]
            below = sum(1 for r in targets if r["margin_pct"] is not None and r["margin_pct"] < r["target"]) if targets else None
        return render("pages/home_manager.html", "home", c=c, pnl=pnl, pprev=pprev, below=below)
    if cur.can("view.work_capture"):
        return redirect(url_for("pages.work"))
    if cur.can("view.admin"):
        return redirect(url_for("pages.admin"))
    abort(403)


def _prev_period(c):
    ps = c["hdr"]["periods"]
    idx = next((i for i, p in enumerate(ps) if p.id == c["period_id"]), None)
    return ps[idx - 1] if idx else None


# ------------------------------------------------------------------ runs
@bp.route("/run", methods=["POST"])
@login_required("run.start")
def start_run():
    from ..services.run import start_run as _start
    c = _ctx()
    run = _start(g.s, g.cur.actor, request.form.get("period", type=int) or c["period_id"], request.form.get("run_type", "on_demand"))
    g.s.commit()
    _spawn_run(g.cur.tenant_id, g.cur.actor, run.id)
    flash(f"Run #{run.id} started.", "ok")
    return _back()


def _spawn_run(tenant_id, actor, run_id):
    from ..db import make_session
    from ..services.run import execute_run

    def work():
        s = make_session(tenant_id)
        try:
            execute_run(s, actor, run_id)
            s.commit()
        except Exception as exc:  # recorded on the run row
            s.rollback()
            s2 = make_session(tenant_id)
            r = s2.get(m.CalculationRun, run_id)
            r.status, r.message, r.finished_at = "failed", str(exc)[:2000], dt.datetime.now(dt.timezone.utc)
            s2.commit()
            s2.close()
        finally:
            s.close()

    if current_app.config.get("SYNC_RUNS"):
        work()
    else:
        threading.Thread(target=work, daemon=True).start()


@bp.route("/run/<int:run_id>")
@login_required()
def run_status(run_id):
    r = g.s.get(m.CalculationRun, run_id)
    if r is None or not g.cur.scope.site_allowed(r.site_id):
        abort(404)
    return jsonify(id=r.id, status=r.status, progress=r.progress, message=r.message, counts=r.counts)


# ------------------------------------------------------------------ leakage
@bp.route("/leakage")
@login_required("view.leakage")
def leakage():
    c = _ctx()
    f = {k: request.args.get(k) or None for k in ("customer", "rule", "status", "direction", "amount_above")}
    rows = ms.leakage_rows(g.s, g.cur.scope, c["site_id"], c["period_id"], customer_id=f["customer"], rule=f["rule"], status=f["status"],
                           direction=f["direction"], amount_above=f["amount_above"]) if c["period_id"] else []
    chart_rows = ms.leakage_rows(g.s, g.cur.scope, c["site_id"], c["period_id"], customer_id=f["customer"], status=f["status"],
                                 direction=f["direction"], amount_above=f["amount_above"]) if c["period_id"] else []
    chart = charts.l1(ms.leakage_by_rule(chart_rows))
    users = [u for u in g.s.execute(select(m.AppUser).where(m.AppUser.active)).scalars()]
    customers = ms.customer_names(g.s, c["site_id"])
    customers = {k: v for k, v in customers.items() if g.cur.scope.customer_allowed(c["site_id"], k)}
    return render("pages/leakage.html", "leakage", c=c, rows=rows, chart=chart, f=f, users=users, customers=customers,
                  statuses=leak_svc.STATUS_LABELS)


@bp.route("/leakage/assign", methods=["POST"])
@login_required("leakage.update")
def leakage_assign():
    ids = [int(x) for x in request.form.getlist("item")]
    for i in ids:
        it = g.s.get(m.LeakageItem, i)
        if it is None or not g.cur.scope.customer_allowed(it.site_id, it.customer_id):
            abort(403)
    leak_svc.assign(g.s, g.cur.actor, ids, request.form.get("owner", type=int))
    _commit_and_flash(f"{len(ids)} item(s) assigned.")
    return _back("/leakage")


@bp.route("/leakage/<int:item_id>", methods=["GET", "POST"])
@login_required("view.leakage")
def leakage_item(item_id):
    c = _ctx()
    it = g.s.get(m.LeakageItem, item_id)
    if it is None or not g.cur.scope.customer_allowed(it.site_id, it.customer_id):
        abort(404)
    if request.method == "POST":
        a = request.form.get("action")
        actor = g.cur.actor
        if a == "review" and g.cur.can("leakage.update"):
            leak_svc.start_review(g.s, actor, item_id)
        elif a == "decide" and g.cur.can("leakage.decide"):
            leak_svc.decide(g.s, actor, item_id, request.form.get("decision"), request.form.get("reason_category"), request.form.get("reason"))
        elif a == "approve" and g.cur.can("leakage.approve"):
            leak_svc.approve(g.s, actor, item_id)
        elif a == "send_back" and g.cur.can("leakage.approve"):
            leak_svc.send_back(g.s, actor, item_id, request.form.get("reason"))
        elif a == "note" and g.cur.can("leakage.note"):
            leak_svc.add_note(g.s, actor, item_id, request.form.get("text"))
        else:
            abort(403)
        _commit_and_flash("Saved.")
        return redirect(url_for("pages.leakage_item", item_id=item_id))
    row = ms.leakage_rows(g.s, g.cur.scope, it.site_id, None, item_ids=[item_id])[0]
    ev = ms.evidence(g.s, g.cur.scope, item_id)
    notes = g.s.execute(select(m.LeakageNote).where(m.LeakageNote.leakage_item_id == item_id).order_by(m.LeakageNote.id)).scalars().all()
    history = g.s.execute(select(m.AuditLog).where(m.AuditLog.entity == "leakage_item", m.AuditLog.entity_id == str(item_id)).order_by(m.AuditLog.id)).scalars().all()
    threshold = get_setting(g.s, g.cur.tenant_id, "leakage.approval_threshold", it.site_id)
    needs = threshold is not None and abs(D(it.gap_amount)) > threshold
    period = g.s.get(m.BillingPeriod, it.period_id)
    return render("pages/leakage_item.html", "leakage", c=c, it=it, row=row, ev=ev, notes=notes, history=history, needs_approval=needs,
                  threshold=threshold, decisions=leak_svc.allowed_decisions(it), labels=leak_svc.DECISION_LABELS, waive=leak_svc.WAIVE_REASONS,
                  users=ms.user_names(g.s), period=period)


# ------------------------------------------------------------------ reconciliation
@bp.route("/reconciliation")
@login_required("view.billing")
def reconciliation():
    c = _ctx()
    data = ms.reconciliation(g.s, g.cur.scope, c["site_id"], c["period_id"]) if c["period_id"] else None
    return render("pages/reconciliation.html", "reconciliation", c=c, data=data)


@bp.route("/reconciliation/cell")
@login_required("view.billing")
def reconciliation_cell():
    c = _ctx()
    cust, charge = request.args.get("customer", type=int), request.args.get("charge", type=int)
    data = ms.recon_cell(g.s, g.cur.scope, c["site_id"], c["period_id"], cust, charge)
    return render("pages/recon_cell.html", "reconciliation", c=c, data=data, customer=g.s.get(m.Customer, cust), charge=g.s.get(m.ChargeCode, charge))


# ------------------------------------------------------------------ customers
@bp.route("/customers")
@login_required("view.customers")
def customers():
    c = _ctx()
    pnl = ms.customers_pnl(g.s, g.cur.scope, c["site_id"], c["period_id"]) if c["period_id"] else dict(available=False, rows=[], site=None, reason="No period.")
    prev = _prev_period(c)
    pprev = ms.customers_pnl(g.s, g.cur.scope, c["site_id"], prev.id) if prev else None
    chart = charts.c1(pnl["rows"]) if pnl["available"] else charts.hidden("C1", pnl.get("reason", ""))
    return render("pages/customers.html", "customers", c=c, pnl=pnl, pprev=pprev, chart=chart)


@bp.route("/customers/<int:customer_id>")
@login_required("view.customers")
def customer(customer_id):
    c = _ctx()
    d = ms.customer_detail(g.s, g.cur.scope, c["site_id"], c["period_id"], customer_id)
    return render("pages/customer.html", "customers", c=c, d=d, c2=charts.c2(d["row"], d["categories"]), c3=charts.c3(d["trend"]))


# ------------------------------------------------------------------ costs
@bp.route("/costs")
@login_required("view.costs")
def costs():
    c = _ctx()
    d = ms.costs(g.s, g.cur.scope, c["site_id"], c["period_id"])
    prev = _prev_period(c)
    dprev = ms.costs(g.s, g.cur.scope, c["site_id"], prev.id) if prev else None
    return render("pages/costs.html", "costs", c=c, d=d, dprev=dprev, chart=charts.k1(d["k1"], d["total"]))


@bp.route("/costs/cell")
@login_required("view.costs")
def costs_cell():
    c = _ctx()
    cat = request.args.get("category")
    d = ms.allocation_cell(g.s, g.cur.scope, c["site_id"], c["period_id"], cat)
    return render("pages/costs_cell.html", "costs", c=c, d=d, category=cat, label=ms.CATEGORY_LABELS.get(cat, cat),
                  customers=ms.customer_names(g.s, c["site_id"]), drivers_l=ms.DRIVER_LABELS)


@bp.route("/costs/labor")
@login_required("view.labor_detail")
def costs_labor():
    c = _ctx()
    return render("pages/labor.html", "costs", c=c, d=ms.labor_attribution(g.s, g.cur.scope, c["site_id"], c["period_id"]))


@bp.route("/costs/overhead", methods=["POST"])
@login_required("cost.enter")
def costs_overhead():
    c = _ctx()
    admin_svc.enter_central_overhead(g.s, g.cur.actor, c["site_id"], c["period_id"], Decimal(request.form["amount"]), request.form.get("note"))
    _commit_and_flash("Central overhead saved. Run the period again to update the results.")
    return _back("/costs")


# ------------------------------------------------------------------ contracts and rates
@bp.route("/contracts")
@login_required("view.contracts|view.contracts.read")
def contracts():
    c = _ctx()
    custs = [x for x in ms.customer_names(g.s, c["site_id"]).values() if g.cur.scope.customer_allowed(c["site_id"], x.id)]
    rows = []
    for cu in sorted(custs, key=lambda x: x.code):
        for k in g.s.execute(select(m.Contract).where(m.Contract.customer_id == cu.id)).scalars():
            vs = g.s.execute(select(m.RateCardVersion).where(m.RateCardVersion.contract_id == k.id).order_by(m.RateCardVersion.version_no)).scalars().all()
            rows.append(dict(customer=cu, contract=k, versions=vs))
    return render("pages/contracts.html", "contracts", c=c, rows=rows, custs=custs)


@bp.route("/contracts/new", methods=["POST"])
@login_required("rate.propose")
def contract_new():
    k = contract_svc.create_contract(g.s, g.cur.actor, int(request.form["customer"]), request.form["reference"],
                                     dt.date.fromisoformat(request.form["start_date"]),
                                     dt.date.fromisoformat(request.form["end_date"]) if request.form.get("end_date") else None, request.form.get("document_ref"))
    _commit_and_flash("Contract created.")
    return redirect(url_for("pages.contracts"))


@bp.route("/contracts/<int:contract_id>/draft", methods=["POST"])
@login_required("rate.propose")
def contract_draft(contract_id):
    v = contract_svc.new_draft(g.s, g.cur.actor, contract_id, request.form.get("reason"))
    _commit_and_flash(f"Draft version {v.version_no} created from the current rates.")
    return redirect(url_for("pages.version", version_id=v.id))


def _version_access(version_id):
    v = g.s.get(m.RateCardVersion, version_id)
    if v is None:
        abort(404)
    k = g.s.get(m.Contract, v.contract_id)
    cu = g.s.get(m.Customer, k.customer_id)
    if not g.cur.scope.customer_allowed(cu.site_id, cu.id):
        abort(404)
    return v, k, cu


@bp.route("/contracts/version/<int:version_id>", methods=["GET", "POST"])
@login_required("view.contracts|view.contracts.read")
def version(version_id):
    c = _ctx()
    v, k, cu = _version_access(version_id)
    if request.method == "POST":
        a = request.form.get("action")
        actor = g.cur.actor
        if a == "save_rate" and g.cur.can("rate.propose"):
            contract_svc.save_rate(g.s, actor, v.id, _rate_form(), request.form.get("rate_id", type=int))
        elif a == "delete_rate" and g.cur.can("rate.propose"):
            contract_svc.delete_rate(g.s, actor, request.form.get("rate_id", type=int))
        elif a == "submit" and g.cur.can("rate.propose"):
            contract_svc.submit(g.s, actor, v.id, dt.date.fromisoformat(request.form["effective_from"]) if request.form.get("effective_from") else None)
        elif a == "approve" and g.cur.can("rate.approve"):
            contract_svc.approve(g.s, actor, v.id)
        elif a == "reject" and g.cur.can("rate.approve"):
            contract_svc.reject(g.s, actor, v.id, request.form.get("reason"))
        else:
            abort(403)
        _commit_and_flash("Saved.")
        return redirect(url_for("pages.version", version_id=v.id))
    rates = g.s.execute(select(m.Rate).where(m.Rate.rate_card_version_id == v.id).order_by(m.Rate.charge_code_id, m.Rate.valid_from)).scalars().all()
    others = g.s.execute(select(m.RateCardVersion).where(m.RateCardVersion.contract_id == k.id).order_by(m.RateCardVersion.version_no)).scalars().all()
    compare = request.args.get("compare", type=int)
    if compare is None:
        prior = [o for o in others if o.version_no < v.version_no and o.status in ("approved", "superseded")]
        compare = prior[-1].id if prior else None
    diff = contract_svc.diff(g.s, compare, v.id) if compare else None
    impact = contract_svc.repricing_impact(g.s, v.id) if v.status == "pending_approval" else None
    return render("pages/version.html", "contracts", c=c, v=v, k=k, cu=cu, rates=rates, others=others, diff=diff, compare=compare, impact=impact,
                  charges=ms.charge_names(g.s), users=ms.user_names(g.s), edit=request.args.get("edit", type=int))


def _rate_form():
    f = request.form

    def dec(k):
        return Decimal(f[k]) if f.get(k) not in (None, "") else None

    def js(k):
        return json.loads(f[k]) if f.get(k) else None
    cats = f.get("eligible_cost_categories")
    return dict(charge_code_id=f.get("charge_code_id", type=int), activity_code=f.get("activity_code") or None, pricing_method=f.get("pricing_method"),
                unit_price=dec("unit_price"), uom=f.get("uom") or None, markup_pct=dec("markup_pct"),
                eligible_cost_categories=[x.strip() for x in cats.split(",") if x.strip()] if cats else None, minimum_charge=dec("minimum_charge"),
                billing_period=f.get("billing_period"), tier_method=f.get("tier_method") or "none", tiers=js("tiers"), condition=js("condition"),
                valid_from=dt.date.fromisoformat(f["valid_from"]) if f.get("valid_from") else None,
                valid_to=dt.date.fromisoformat(f["valid_to"]) if f.get("valid_to") else None, currency_code=f.get("currency_code") or "USD")


# ------------------------------------------------------------------ work capture
@bp.route("/work", methods=["GET", "POST"])
@login_required("view.work_capture")
def work():
    c = _ctx()
    if request.method == "POST":
        if not g.cur.can("manual.create"):
            abort(403)
        cust = request.form.get("customer", type=int)
        if not g.cur.scope.customer_allowed(c["site_id"], cust):
            abort(403)
        day = dt.date.fromisoformat(request.form["service_date"])
        tz = dt.timezone.utc

        def t(k):
            return dt.datetime.fromisoformat(f"{day.isoformat()}T{request.form[k]}").replace(tzinfo=tz) if request.form.get(k) else None
        photo = work_svc.save_photo(request.files.get("photo"))
        work_svc.submit_entry(g.s, g.cur.actor, c["site_id"], cust, request.form.get("charge", type=int), request.form["quantity"], request.form.get("uom"),
                              day, t("start"), t("end"), request.form.get("note"), photo, request.form.get("work_order_ref") or None)
        _commit_and_flash("Sent for review.")
        return redirect(url_for("pages.work"))
    custs = [x for x in ms.customer_names(g.s, c["site_id"]).values() if g.cur.scope.customer_allowed(c["site_id"], x.id) and x.active]
    charges = g.s.execute(select(m.ChargeCode).where(m.ChargeCode.manual_capture.is_(True)).order_by(m.ChargeCode.name)).scalars().all()
    mine = g.s.execute(select(m.ManualBillableEntry).where(m.ManualBillableEntry.entered_by == g.cur.user.id).order_by(m.ManualBillableEntry.id.desc()).limit(50)).scalars().all()
    review = []
    if g.cur.can("manual.review"):
        review = [e for e in g.s.execute(select(m.ManualBillableEntry).where(m.ManualBillableEntry.site_id == c["site_id"], m.ManualBillableEntry.status == "submitted")
                                         .order_by(m.ManualBillableEntry.id)).scalars() if g.cur.scope.customer_allowed(c["site_id"], e.customer_id)]
    return render("pages/work.html", "work", c=c, custs=custs, charges=charges, mine=mine, review=review, cmap=ms.customer_names(g.s, c["site_id"]),
                  chmap=ms.charge_names(g.s), users=ms.user_names(g.s), wo=request.args.get("wo", ""), today=dt.date.today())


@bp.route("/work/review/<int:entry_id>", methods=["POST"])
@login_required("manual.review")
def work_review(entry_id):
    e = g.s.get(m.ManualBillableEntry, entry_id)
    if e is None or not g.cur.scope.customer_allowed(e.site_id, e.customer_id):
        abort(404)
    work_svc.review_entry(g.s, g.cur.actor, entry_id, request.form.get("decision") == "accept", request.form.get("reason"))
    _commit_and_flash("Entry reviewed. Accepted entries are priced in the next run.")
    return _back("/work")


@bp.route("/work/photo/<int:entry_id>")
@login_required("view.work_capture|manual.review|view.evidence")
def work_photo(entry_id):
    from ..config import Config
    e = g.s.get(m.ManualBillableEntry, entry_id)
    if e is None or not e.photo_ref or not g.cur.scope.customer_allowed(e.site_id, e.customer_id):
        abort(404)
    if not g.cur.can("manual.review") and not g.cur.can("view.evidence") and e.entered_by != g.cur.user.id:
        abort(404)
    return send_file(Config.UPLOAD_DIR / e.photo_ref)


# ------------------------------------------------------------------ period close
@bp.route("/close", methods=["GET", "POST"])
@login_required("view.close")
def close():
    c = _ctx()
    if request.method == "POST":
        a = request.form.get("action")
        actor = g.cur.actor
        if a == "acknowledge" and g.cur.can("close.acknowledge"):
            close_svc.acknowledge(g.s, actor, c["period_id"], request.form.get("check", type=int), request.form.get("reason"))
        elif a == "close" and g.cur.can("period.close"):
            close_svc.close_period(g.s, actor, g.cur.scope, c["period_id"])
        elif a == "reopen" and g.cur.can("period.reopen"):
            close_svc.reopen_period(g.s, actor, c["period_id"], request.form.get("reason"))
        elif a == "verify" and g.cur.can("period.close"):
            from ..services.run import run_period
            r = run_period(g.s, actor, c["period_id"], "verify")
            g.s.commit()
            flash(f"Verification run #{r.id}: {r.message}", "ok" if (r.counts or {}).get("matches_close_run") else "error")
            return redirect(url_for("pages.close"))
        else:
            abort(403)
        _commit_and_flash("Saved.")
        return redirect(url_for("pages.close"))
    checks = close_svc.run_checks(g.s, g.cur.actor, g.cur.scope, c["period_id"]) if c["period_id"] else []
    verifies = g.s.execute(select(m.CalculationRun).where(m.CalculationRun.period_id == c["period_id"], m.CalculationRun.run_type == "verify")
                           .order_by(m.CalculationRun.id.desc()).limit(5)).scalars().all() if c["period_id"] else []
    return render("pages/close.html", "close", c=c, checks=checks, verifies=verifies, users=ms.user_names(g.s))


# ------------------------------------------------------------------ data
@bp.route("/data")
@login_required("view.data")
def data():
    from ..ingest.sources import SOURCE_TYPES
    c = _ctx()
    d = ms.data_health(g.s, g.cur.scope, c["site_id"], c["period_id"])
    tab = request.args.get("tab", "imports")
    return render("pages/data.html", "data", c=c, d=d, sources=SOURCE_TYPES, tab=tab, cmap=ms.customer_names(g.s, c["site_id"]),
                  chmap=ms.charge_names(g.s))


@bp.route("/data/import", methods=["POST"])
@login_required("data.import")
def data_import():
    from ..ingest.importer import import_file
    c = _ctx()
    f = request.files.get("file")
    if f is None or not f.filename:
        raise BusinessError("Choose a file to import.")
    mapping = None
    if request.form.get("mapping"):
        mapping = json.loads(request.form["mapping"])
        st = request.form["source_type"]
        row = g.s.execute(select(m.ColumnMapping).where(m.ColumnMapping.source_type == st)).scalar_one_or_none()
        if row is None:
            g.s.add(m.ColumnMapping(tenant_id=g.cur.tenant_id, source_type=st, mapping=mapping, updated_by=g.cur.user.id))
        else:
            row.mapping = mapping
    site = g.s.get(m.Site, c["site_id"])
    b = import_file(g.s, g.cur.actor, site, request.form["source_type"], f.filename, f.read(), mapping=mapping,
                    scope_key=request.form.get("scope_key") or None)
    g.s.commit()
    flash(f"Import #{b.id}: {b.report.get('summary')}", "ok" if b.status == "accepted" and not b.rejected_count else "error")
    return redirect(url_for("pages.data_batch", batch_id=b.id))


@bp.route("/data/batch/<int:batch_id>")
@login_required("view.data")
def data_batch(batch_id):
    c = _ctx()
    b = g.s.get(m.ImportBatch, batch_id)
    if b is None or not g.cur.scope.site_allowed(b.site_id):
        abort(404)
    rej = g.s.execute(select(m.ImportRejectedRow).where(m.ImportRejectedRow.import_batch_id == batch_id).order_by(m.ImportRejectedRow.row_no).limit(200)).scalars().all()
    if request.args.get("download"):
        import csv
        rows = g.s.execute(select(m.ImportRejectedRow).where(m.ImportRejectedRow.import_batch_id == batch_id).order_by(m.ImportRejectedRow.row_no)).scalars().all()
        buf = io.StringIO()
        keys = sorted({k for r in rows for k in r.payload})
        w = csv.writer(buf)
        w.writerow(["ROW", "REASON"] + keys)
        for r in rows:
            w.writerow([r.row_no, r.reason] + [r.payload.get(k, "") for k in keys])
        return send_file(io.BytesIO(buf.getvalue().encode()), mimetype="text/csv", as_attachment=True, download_name=f"rejected_rows_batch_{batch_id}.csv")
    return render("pages/batch.html", "data", c=c, b=b, rej=rej, users=ms.user_names(g.s))


# ------------------------------------------------------------------ admin
@bp.route("/admin", methods=["GET", "POST"])
@login_required("view.admin")
def admin():
    from ..security import ROLES, ROLE_LABELS
    c = _ctx()
    tab = request.args.get("tab", "users")
    if request.method == "POST":
        if not g.cur.can("admin.config"):
            abort(403)
        a = request.form.get("action")
        actor = g.cur.actor
        if a == "setting":
            key = request.form["key"]
            spec = SETTINGS.get(key, {})
            site = c["site_id"] if (spec.get("scope") == "site" or key.startswith("margin_target.")) else None
            set_setting(g.s, actor, key, request.form.get("value", "").strip(), site)
        elif a == "user":
            scopes = [(c["site_id"], None)] if not request.form.get("customers") else [(c["site_id"], int(x)) for x in request.form.getlist("customers")]
            admin_svc.create_user(g.s, actor, request.form["email"], request.form["name"], request.form.getlist("roles"), scopes)
        elif a == "user_update":
            uid = request.form.get("user_id", type=int)
            scopes = [(c["site_id"], None)] if not request.form.getlist("customers") else [(c["site_id"], int(x)) for x in request.form.getlist("customers")]
            admin_svc.update_user(g.s, actor, uid, roles=request.form.getlist("roles"), scopes=scopes, active=request.form.get("active") == "1")
        elif a == "charge":
            admin_svc.create_charge_code(g.s, actor, request.form["code"].strip().upper(), request.form["name"], request.form["category"],
                                         request.form.get("uom") or None, request.form.get("event_type") or None, request.form.get("description"),
                                         request.form.get("manual") == "1")
        elif a == "period":
            admin_svc.create_period(g.s, actor, c["site_id"], request.form["label"], dt.date.fromisoformat(request.form["start"]),
                                    dt.date.fromisoformat(request.form["end"]), request.form["period_type"])
        elif a == "driver":
            admin_svc.create_driver(g.s, actor, c["site_id"], request.form["category"], request.form["driver_type"], request.form["description"],
                                    dt.date.fromisoformat(request.form["valid_from"]))
        else:
            abort(400)
        _commit_and_flash("Saved.")
        return redirect(url_for("pages.admin", tab=tab))
    users = g.s.execute(select(m.AppUser).order_by(m.AppUser.email)).scalars().all()
    roles = {u.id: [r.role for r in g.s.execute(select(m.UserRole).where(m.UserRole.user_id == u.id)).scalars()] for u in users}
    scopes = {u.id: g.s.execute(select(m.UserScope).where(m.UserScope.user_id == u.id)).scalars().all() for u in users}
    settings = []
    for k, spec in SETTINGS.items():
        settings.append(dict(key=k, spec=spec, value=_raw_setting(k, c["site_id"] if spec["scope"] == "site" else None)))
    custs = ms.customer_names(g.s, c["site_id"])
    for cu in custs.values():
        settings.append(dict(key=f"margin_target.{cu.code}", spec=dict(desc=f"Margin target % for {cu.code} (optional)", required=None, type="decimal", scope="site"),
                             value=_raw_setting(f"margin_target.{cu.code}", c["site_id"])))
    audit_q = select(m.AuditLog).order_by(m.AuditLog.id.desc())
    if request.args.get("entity"):
        audit_q = audit_q.where(m.AuditLog.entity == request.args["entity"])
    if request.args.get("user"):
        audit_q = audit_q.where(m.AuditLog.user_id == request.args.get("user", type=int))
    if request.args.get("action_like"):
        audit_q = audit_q.where(m.AuditLog.action.like(f"%{request.args['action_like']}%"))
    audit = g.s.execute(audit_q.limit(200)).scalars().all() if g.cur.can("view.audit") else []
    from ..engine.allocation import DRIVER_TYPES, EXPENSE_CATEGORIES
    return render("pages/admin.html", "admin", c=c, tab=tab, users=users, roles=roles, scopes=scopes, all_roles=ROLES, role_labels=ROLE_LABELS,
                  settings=settings, charges=g.s.execute(select(m.ChargeCode).order_by(m.ChargeCode.code)).scalars().all(),
                  categories=admin_svc.CHARGE_CATEGORIES, drivers=g.s.execute(select(m.AllocationDriver).where(m.AllocationDriver.site_id == c["site_id"])
                                                                             .order_by(m.AllocationDriver.expense_category, m.AllocationDriver.id)).scalars().all(),
                  driver_types=DRIVER_TYPES, expense_categories=EXPENSE_CATEGORIES, cat_labels=ms.CATEGORY_LABELS, drv_labels=ms.DRIVER_LABELS,
                  audit=audit, unames=ms.user_names(g.s), custs=custs)


def _raw_setting(key, site_id):
    q = select(m.Setting).where(m.Setting.key == key)
    q = q.where(m.Setting.site_id == site_id) if site_id else q.where(m.Setting.site_id.is_(None))
    r = g.s.execute(q).scalar_one_or_none()
    return r.value if r else None


@bp.route("/admin/drivers", methods=["GET", "POST"])
@login_required("driver.approve|driver.define|cost.enter")
def drivers():
    c = _ctx()
    if request.method == "POST":
        if request.form.get("action") == "approve" and g.cur.can("driver.approve"):
            d = g.s.get(m.AllocationDriver, request.form.get("driver_id", type=int))
            if d is None or d.site_id != c["site_id"]:
                abort(404)
            admin_svc.approve_driver(g.s, g.cur.actor, d.id)
        elif request.form.get("action") == "quantity" and (g.cur.can("driver.define") or g.cur.can("cost.enter")):
            admin_svc.enter_driver_quantity(g.s, g.cur.actor, request.form.get("driver_id", type=int), c["period_id"],
                                            request.form.get("customer", type=int) or None, Decimal(request.form["quantity"]), request.form.get("source_ref") or "manual entry")
        else:
            abort(403)
        _commit_and_flash("Saved.")
        return redirect(url_for("pages.drivers"))
    ds = g.s.execute(select(m.AllocationDriver).where(m.AllocationDriver.site_id == c["site_id"]).order_by(m.AllocationDriver.expense_category, m.AllocationDriver.id)).scalars().all()
    qty = {d.id: g.s.execute(select(m.AllocationDriverQuantity).where(m.AllocationDriverQuantity.driver_id == d.id,
                                                                      m.AllocationDriverQuantity.period_id == c["period_id"])).scalars().all() for d in ds}
    return render("pages/drivers.html", "admin" if g.cur.can("view.admin") else "costs", c=c, ds=ds, qty=qty, users=ms.user_names(g.s),
                  cat_labels=ms.CATEGORY_LABELS, drv_labels=ms.DRIVER_LABELS, custs=ms.customer_names(g.s, c["site_id"]))


# ------------------------------------------------------------------ alerts (G3, G6)
@bp.route("/alerts", methods=["GET", "POST"])
@login_required("view.leakage|view.alerts")
def alerts():
    c = _ctx()
    q = select(m.Alert).where(m.Alert.site_id == c["site_id"], (m.Alert.user_id == g.cur.user.id) | m.Alert.user_id.is_(None))
    if request.method == "POST":
        for a in g.s.execute(q.where(m.Alert.read_at.is_(None))).scalars():
            a.read_at = dt.datetime.now(dt.timezone.utc)
        _commit_and_flash("Marked as read.")
        return redirect(url_for("pages.alerts"))
    return render("pages/alerts.html", "home", c=c, rows=g.s.execute(q.order_by(m.Alert.id.desc()).limit(200)).scalars().all())


# ------------------------------------------------------------------ exports
@bp.route("/exports/<name>")
@login_required()
def export(name):
    from ..services import exports
    c = _ctx()
    fmt = request.args.get("format", "xlsx")
    content, mimetype, filename = exports.build(g.s, g.cur, name, c["site_id"], c["period_id"], fmt)
    return send_file(io.BytesIO(content), mimetype=mimetype, as_attachment=True, download_name=filename)


from . import ga_pages  # noqa: E402,F401  (registers GA routes on this blueprint)
