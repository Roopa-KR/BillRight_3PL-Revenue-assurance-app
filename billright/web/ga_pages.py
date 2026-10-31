"""GA feature pages (G1-G8). Registered on the main pages blueprint."""
from __future__ import annotations

import datetime as dt

from flask import abort, g, redirect, request, url_for

from .. import models as m
from ..ga import features as ga
from .auth import login_required
from .pages import _commit_and_flash, _ctx, bp, render


@bp.route("/ga")
@login_required("view.billing|view.costs|view.admin")
def ga_home():
    c = _ctx()
    data = {}
    if c["period_id"]:
        if g.cur.can("view.billing"):
            data["minimums"] = ga.minimums_tiers(g.s, g.cur.scope, c["site_id"], c["period_id"])
            data["drift"] = ga.contract_drift(g.s, g.cur.scope, c["site_id"], c["period_id"])
            data["sla"] = ga.sla_exposure(g.s, g.cur.scope, c["site_id"], c["period_id"])
        data["storage"] = ga.storage_preview(g.s, g.cur.scope, c["site_id"], c["period_id"])
        data["surcharges"] = g.s.query(m.SurchargeRule).filter(m.SurchargeRule.site_id == c["site_id"]).all()
        data["exports"] = g.s.query(m.AccountingExport).filter(m.AccountingExport.site_id == c["site_id"]).order_by(m.AccountingExport.id.desc()).limit(10).all()
    return render("pages/ga.html", "home", c=c, data=data)


@bp.route("/ga/alerts/run", methods=["POST"])
@login_required("run.start")
def ga_daily():
    c = _ctx()
    n = ga.daily_run(g.s, g.cur.actor, c["site_id"])
    _commit_and_flash(f"Daily rating and rule run done: {n} new item alert(s).")
    return redirect(url_for("pages.alerts"))


@bp.route("/ga/drift/run", methods=["POST"])
@login_required("view.billing")
def ga_drift():
    c = _ctx()
    n = ga.drift_alerts(g.s, g.cur.actor, c["site_id"], c["period_id"])
    _commit_and_flash(f"{n} drift warning(s) raised.")
    return redirect(url_for("pages.alerts"))


@bp.route("/ga/storage/generate", methods=["POST"])
@login_required("data.import")
def ga_storage():
    c = _ctx()
    b = ga.generate_storage_events(g.s, g.cur.actor, c["site_id"], c["period_id"])
    _commit_and_flash(f"Storage quantities created from daily snapshots (import #{b.id}). Run the period to price them.")
    return redirect(url_for("pages.ga_home"))


@bp.route("/ga/surcharge", methods=["POST"])
@login_required("admin.config|rate.approve")
def ga_surcharge():
    c = _ctx()
    if request.form.get("action") == "approve":
        ga.approve_surcharge_rule(g.s, g.cur.actor, request.form.get("rule_id", type=int))
    else:
        import json
        ga.create_surcharge_rule(g.s, g.cur.actor, c["site_id"], request.form["name"], request.form["kind"], request.form["event_type"],
                                 request.form.get("charge", type=int), json.loads(request.form.get("parameters") or "{}"))
    _commit_and_flash("Saved.")
    return redirect(url_for("pages.ga_home"))


@bp.route("/ga/accounting", methods=["POST"])
@login_required("accounting.export")
def ga_accounting():
    c = _ctx()
    exp = ga.push_adjustments(g.s, g.cur, c["site_id"], c["period_id"])
    _commit_and_flash(f"Accounting export #{exp.id}: {len(exp.item_ids)} adjustment(s) in {exp.file_name}.")
    return redirect(url_for("pages.ga_home"))


@bp.route("/ga/accounting/<int:export_id>")
@login_required("accounting.export")
def ga_accounting_file(export_id):
    import io
    from flask import send_file
    e = g.s.get(m.AccountingExport, export_id)
    if e is None or not g.cur.scope.site_allowed(e.site_id):
        abort(404)
    return send_file(io.BytesIO(e.content), mimetype="text/csv", as_attachment=True, download_name=e.file_name)
