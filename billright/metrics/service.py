"""The single metrics service (P8).

Every number shown on a page, returned by the API, or written to an export comes from a function in this
module. Each function takes the database session, the caller's ``Scope`` and the site/period, applies the
site and customer scope itself, and returns plain dicts with Decimal values. KPI tiles are computed from the
same rows as the tables beneath them.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import and_, func, or_, select

from .. import models as m
from ..engine import labor as labor_engine
from ..engine.allocation import EXPENSE_CATEGORIES, PAYMENT_RECIPIENTS
from ..engine.leakage import RULE_NAMES
from ..engine.money import ZERO, D, pct
from ..engine.pnl import ledger_by_recipient
from ..security import Scope
from ..services.common import BusinessError, get_setting
from ..services.contracts import latest_run
from ..services.leakage import CONFIRMED, DECISION_LABELS, STATUS_LABELS

CATEGORY_LABELS = {
    "warehouse_operators": "Warehouse operators", "forklift_operators": "Forklift operators", "supervisors": "Supervisors",
    "managers": "Managers", "administration": "Administration", "transport_carriers": "Transport carriers",
    "warehouse_rent": "Warehouse rent", "utilities": "Utilities", "equipment": "Equipment", "packaging": "Packaging",
    "software": "Software", "other_overhead": "Other overhead (security, insurance, cleaning, misc.)",
}
RECIPIENT_LABELS = {"direct_payroll": "Direct employee payroll", "staffing_agency": "Staffing agency", "carrier": "Transportation carriers",
                    "facility_equipment_it_other": "Facility, equipment, IT and other vendors"}
RECIPIENT_TREATMENT = {"direct_payroll": "Employee payroll (operators, forklift, supervisors, managers, admin)",
                       "staffing_agency": "External contract labor, included in the operator expense line",
                       "carrier": "Freight and trucking vendor invoices",
                       "facility_equipment_it_other": "Rent, utilities, packaging, maintenance, software and overhead"}
DRIVER_LABELS = {"paid_labor_hours_by_customer": "Paid labor hours", "documented_time_share": "Documented time",
                 "equipment_hours_by_customer": "Equipment hours", "freight_bill_by_customer": "Freight bill", "occupied_space_days": "Occupied space-days",
                 "pallet_days": "Pallet-days", "reserved_space": "Reserved space", "metered_usage": "Metered usage", "materials_consumed": "Materials consumed",
                 "transactions_by_customer": "Transactions", "completed_line_share_proxy": "Proxy: completed-line share", "direct": "Direct"}
EXCEPTION_LABELS = {"no_approved_driver": "No approved driver", "more_than_one_approved_driver": "More than one approved driver",
                    "zero_driver_quantity": "Driver quantities total zero", "direct_only_category_has_unattributed_lines": "Line without a customer in a direct-only category",
                    "unassigned_activity": "Unassigned activity (idle, shared, training)"}


class NotAllowed(BusinessError):
    pass


def _check_site(scope: Scope, site_id: int):
    if not scope.site_allowed(site_id):
        raise NotAllowed("You do not have access to this site.")


def _check_customer(s, scope: Scope, site_id: int, customer_id):
    c = s.get(m.Customer, customer_id) if customer_id is not None else None
    if c is None or c.site_id != site_id or not scope.customer_allowed(site_id, customer_id):
        raise NotAllowed("Customer not found or not in your scope.")
    return c


def _cust_filter(col, scope: Scope, site_id: int):
    allowed = scope.customers(site_id)
    if allowed is None:
        return True
    return col.in_(list(allowed) or [-1])


def r1(x: Decimal | None) -> Decimal | None:
    return None if x is None else D(x).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


# ------------------------------------------------------------------ header context
def periods(s, scope: Scope, site_id: int):
    _check_site(scope, site_id)
    return s.execute(select(m.BillingPeriod).where(m.BillingPeriod.site_id == site_id).order_by(m.BillingPeriod.start_date)).scalars().all()


def period_run(s, period: m.BillingPeriod):
    if period.status == "closed" and period.close_run_id:
        return s.get(m.CalculationRun, period.close_run_id)
    return latest_run(s, period.id)


def data_as_of(s, scope: Scope, site_id: int):
    _check_site(scope, site_id)
    rows = s.execute(select(m.ImportBatch.source_type, func.max(m.ImportBatch.created_at), func.count())
                     .where(m.ImportBatch.site_id == site_id, m.ImportBatch.status == "accepted").group_by(m.ImportBatch.source_type)).all()
    per_source = {r[0]: r[1] for r in rows}
    return (max(per_source.values()) if per_source else None), per_source


def header(s, scope: Scope, site_id: int, period_id: int | None):
    site = s.get(m.Site, site_id)
    _check_site(scope, site_id)
    ps = periods(s, scope, site_id)
    period = next((p for p in ps if p.id == period_id), None)
    run = period_run(s, period) if period else None
    as_of, per_source = data_as_of(s, scope, site_id)
    labels = sorted({b for (b,) in s.execute(select(m.ImportBatch.data_label).where(m.ImportBatch.site_id == site_id,
                                                                                      m.ImportBatch.status == "accepted").distinct()) if b})
    if site.data_label and site.data_label not in labels:
        labels.append(site.data_label)
    return dict(site=site, periods=ps, period=period, run=run, data_as_of=as_of, per_source=per_source, data_labels=labels)


# ------------------------------------------------------------------ names
def customer_names(s, site_id: int):
    return {c.id: c for c in s.execute(select(m.Customer).where(m.Customer.site_id == site_id)).scalars()}


def charge_names(s):
    return {c.id: c for c in s.execute(select(m.ChargeCode)).scalars()}


def user_names(s):
    return {u.id: u.name for u in s.execute(select(m.AppUser)).scalars()}


# ------------------------------------------------------------------ leakage
def fmt_qty(q):
    q = D(q)
    return f"{q.normalize():,f}" if q == q.to_integral_value() else f"{q:,}"


def money_text(x):
    x = D(x)
    sign = "−" if x < 0 else ""
    return f"{sign}${abs(x):,.2f}"


def price_text(p):
    if p is None:
        return "?"
    p = D(p).normalize()
    q = p.quantize(Decimal("0.01")) if p.as_tuple().exponent > -2 else p
    return f"${q:,f}"


def what_happened(item, cust, charge, period_label, uom=None) -> str:
    """One deterministic sentence per rule (9.3)."""
    code = cust.code
    name = charge.name[0].lower() + charge.name[1:] if charge.name else charge.code
    unit = (uom or charge.default_uom or "unit")
    bq, dq = D(item.billable_quantity), D(item.billed_quantity)
    price = item.contract_unit_price
    rule = item.rule_code
    if rule == "L1":
        if price is not None and bq:
            return f"{code} was not billed the {name} for {period_label}: {fmt_qty(bq)} {unit}s × {price_text(price)} = {money_text(item.billable_amount)}."
        return f"{code} was not billed the {name} for {period_label}: {money_text(item.billable_amount)} is billable."
    if rule == "L2":
        diff = bq - dq
        tail = f": {fmt_qty(diff)} × {price_text(price)} = {money_text(item.gap_amount)} not billed." if price is not None else "."
        return f"{code} was billed {fmt_qty(dq)} of {fmt_qty(bq)} {unit}s of {name} for {period_label}{tail}"
    if rule == "L3":
        billed = ", ".join(price_text(p) for p in (item.billed_unit_prices or []))
        return f"{code} was billed {name} at {billed} instead of the contract {price_text(price)} for {period_label} ({money_text(item.gap_amount)} difference)."
    if rule == "L4":
        missing = bq - dq
        return f"{code} was not billed the {name} on {fmt_qty(missing)} qualifying {unit}s in {period_label}: {money_text(item.gap_amount)}."
    if rule == "L5":
        return f"{code}'s {name} for {period_label} was billed {money_text(item.billed_amount)}, below the contract minimum ({money_text(item.billable_amount)})."
    if rule == "L6":
        billed = ", ".join(price_text(p) for p in (item.billed_unit_prices or []))
        return f"{code} was billed {name} at the expired rate {billed} instead of {price_text(price)} for {period_label} ({money_text(item.gap_amount)})."
    if rule == "L7":
        return f"{code} was billed {money_text(item.billed_amount)} for {name} in {period_label}, {money_text(abs(D(item.gap_amount)))} more than the contract allows ({money_text(item.billable_amount)})."
    return f"{code} {name} {period_label}: gap {money_text(item.gap_amount)}."


def leakage_rows(s, scope: Scope, site_id: int, period_id: int | None = None, customer_id=None, rule=None, status=None, direction=None,
                 amount_above=None, include_carried=True, item_ids=None):
    """Items for the review table. With a period, 'Bill next cycle' items of earlier periods that are not yet
    invoiced are carried into the queue (flag ``carried``)."""
    _check_site(scope, site_id)
    q = select(m.LeakageItem).where(m.LeakageItem.site_id == site_id, _cust_filter(m.LeakageItem.customer_id, scope, site_id))
    ps = {p.id: p for p in periods(s, scope, site_id)}
    if period_id:
        cur = ps[period_id]
        earlier = [p.id for p in ps.values() if p.end_date < cur.start_date]
        cond = m.LeakageItem.period_id == period_id
        if include_carried and earlier:
            cond = or_(cond, and_(m.LeakageItem.period_id.in_(earlier), m.LeakageItem.decision == "bill_next_cycle",
                                  m.LeakageItem.invoiced_amount.is_(None), m.LeakageItem.status.in_(("decided", "approved"))))
        q = q.where(cond)
    if item_ids is not None:
        q = q.where(m.LeakageItem.id.in_(list(item_ids) or [-1]))
    items = s.execute(q.order_by(m.LeakageItem.gap_amount.desc(), m.LeakageItem.id)).scalars().all()
    custs, charges, users = customer_names(s, site_id), charge_names(s), user_names(s)
    today = dt.date.today()
    out = []
    for it in items:
        if customer_id and it.customer_id != int(customer_id):
            continue
        if rule and it.rule_code != rule:
            continue
        if status and it.status != status:
            continue
        if direction and it.direction != direction:
            continue
        if amount_above not in (None, "") and abs(D(it.gap_amount)) <= D(amount_above):
            continue
        c, ch, p = custs[it.customer_id], charges[it.charge_code_id], ps[it.period_id]
        out.append(dict(id=it.id, item=it, customer=c, customer_code=c.code, customer_name=c.name, charge=ch.code, charge_name=ch.name,
                        period=p.label, period_id=p.id, rule=it.rule_code, rule_name=RULE_NAMES[it.rule_code], also=it.also_matches or [],
                        what=what_happened(it, c, ch, p.label), billable=D(it.billable_amount), billed=D(it.billed_amount), gap=D(it.gap_amount),
                        direction=it.direction, status=it.status, status_label=STATUS_LABELS.get(it.status, it.status),
                        decision=it.decision, decision_label=DECISION_LABELS.get(it.decision, ""),
                        age_days=(today - it.created_at.date()).days, owner=users.get(it.owner_id), owner_id=it.owner_id,
                        carried=period_id is not None and it.period_id != period_id, invoiced=D(it.invoiced_amount) if it.invoiced_amount is not None else None))
    return out


OPEN_STATUSES = ("new", "under_review")


def analyst_kpis(s, scope: Scope, site_id: int, period_id: int):
    rows = leakage_rows(s, scope, site_id, period_id)
    open_rows = [r for r in rows if r["status"] in OPEN_STATUSES and not r["carried"]]
    carried = [r for r in rows if r["carried"]]
    run = period_run(s, s.get(m.BillingPeriod, period_id))
    cannot = 0
    if run:
        cannot = s.execute(select(func.count()).select_from(m.CannotRateItem).where(
            m.CannotRateItem.run_id == run.id, or_(m.CannotRateItem.customer_id.is_(None), _cust_filter(m.CannotRateItem.customer_id, scope, site_id)))).scalar()
    to_review = s.execute(select(func.count()).select_from(m.ManualBillableEntry).where(
        m.ManualBillableEntry.site_id == site_id, m.ManualBillableEntry.status == "submitted",
        _cust_filter(m.ManualBillableEntry.customer_id, scope, site_id))).scalar()
    return dict(open_amount=sum((r["gap"] for r in open_rows if r["direction"] == "under"), ZERO),
                open_over_amount=sum((abs(r["gap"]) for r in open_rows if r["direction"] == "over"), ZERO),
                open_count=len(open_rows), open_rows=open_rows,
                carried_amount=sum((r["gap"] for r in carried), ZERO), carried_rows=carried, cannot_rate=cannot, manual_to_review=to_review,
                next_items=sorted([r for r in rows if r["status"] in OPEN_STATUSES], key=lambda r: -abs(r["gap"]))[:10])


def leakage_by_rule(rows):
    """Chart L1 data: open gap $ by rule (absolute), sorted descending."""
    agg = defaultdict(lambda: ZERO)
    for r in rows:
        if r["status"] in OPEN_STATUSES:
            agg[r["rule"]] += abs(r["gap"])
    return sorted(({"rule": k, "label": RULE_NAMES[k], "amount": v} for k, v in agg.items()), key=lambda x: -x["amount"])


def _period_items(s, scope, site_id, period_id):
    return s.execute(select(m.LeakageItem).where(m.LeakageItem.site_id == site_id, m.LeakageItem.period_id == period_id,
                                                 _cust_filter(m.LeakageItem.customer_id, scope, site_id))).scalars().all()


def period_leakage_metrics(s, scope, site_id, period_id):
    """7.9 metrics for one period."""
    items = _period_items(s, scope, site_id, period_id)
    flagged = sum((D(i.gap_amount) for i in items if D(i.gap_amount) > 0), ZERO)
    final = [i for i in items if i.status in ("decided", "approved")]
    confirmed_items = [i for i in final if i.decision in CONFIRMED and D(i.gap_amount) > 0]
    confirmed = sum((D(i.gap_amount) for i in confirmed_items), ZERO)
    invoiced = sum((min(D(i.invoiced_amount), D(i.gap_amount)) for i in confirmed_items if i.invoiced_amount is not None), ZERO)
    decided = [i for i in final if i.decision]
    prec_num = sum(1 for i in decided if i.decision in ("bill_now", "bill_next_cycle", "waive", "issue_credit"))
    prevented = sum((D(i.gap_amount) for i in items if i.alerted_at is not None and i.status == "resolved_by_data" and D(i.gap_amount) > 0), ZERO)
    over = sum((abs(D(i.gap_amount)) for i in items if i.rule_code == "L7"), ZERO)
    return dict(flagged=flagged, confirmed=confirmed, invoiced=invoiced, recovery_rate=(invoiced / confirmed) if confirmed else None,
                prevented=prevented, precision=(Decimal(prec_num) / len(decided)) if decided else None, decided_count=len(decided),
                over_billing=over, items=len(items))


def controller_kpis(s, scope, site_id, period_id):
    pm = period_leakage_metrics(s, scope, site_id, period_id)
    pnl = customers_pnl(s, scope, site_id, period_id)
    appr = approvals(s, scope, site_id)
    return dict(confirmed=pm["confirmed"], invoiced=pm["invoiced"], site_margin=pnl["site"]["margin_pct"] if pnl["site"] else None,
                approvals_waiting=len(appr), approvals=appr, pnl_available=pnl["available"])


def recovery_by_period(s, scope, site_id, upto_period_id=None, n=6):
    """Chart H1: confirmed vs invoiced per period (last 6 available)."""
    ps = periods(s, scope, site_id)
    if upto_period_id:
        cur = next(p for p in ps if p.id == upto_period_id)
        ps = [p for p in ps if p.start_date <= cur.start_date]
    out = []
    for p in ps[-n:]:
        if period_run(s, p) is None:
            continue
        pm = period_leakage_metrics(s, scope, site_id, p.id)
        out.append(dict(period=p.label, confirmed=pm["confirmed"], invoiced=pm["invoiced"]))
    return out


def approvals(s, scope, site_id):
    out = []
    for v, c, cu in s.execute(select(m.RateCardVersion, m.Contract, m.Customer).join(m.Contract, m.Contract.id == m.RateCardVersion.contract_id)
                              .join(m.Customer, m.Customer.id == m.Contract.customer_id)
                              .where(m.Customer.site_id == site_id, m.RateCardVersion.status == "pending_approval",
                                     _cust_filter(m.Customer.id, scope, site_id))).all():
        out.append(dict(kind="Rate-card version", label=f"{cu.code} · {c.reference} v{v.version_no}", link=f"/contracts/version/{v.id}", id=v.id))
    threshold = get_setting(s, scope.tenant_id, "leakage.approval_threshold", site_id)
    if threshold is not None:
        for it in s.execute(select(m.LeakageItem).where(m.LeakageItem.site_id == site_id, m.LeakageItem.status == "decided",
                                                        _cust_filter(m.LeakageItem.customer_id, scope, site_id))).scalars():
            if abs(D(it.gap_amount)) > threshold:
                out.append(dict(kind="Leakage decision", label=f"Item {it.id}: {DECISION_LABELS.get(it.decision)} {money_text(it.gap_amount)}",
                                link=f"/leakage/{it.id}", id=it.id))
    for d in s.execute(select(m.AllocationDriver).where(m.AllocationDriver.site_id == site_id, m.AllocationDriver.status == "draft")).scalars():
        out.append(dict(kind="Allocation driver", label=f"{CATEGORY_LABELS.get(d.expense_category)}: {DRIVER_LABELS.get(d.driver_type)}",
                        link="/admin/drivers", id=d.id))
    return out


# ------------------------------------------------------------------ evidence (F8)
def evidence(s, scope, item_id, limit=500):
    it = s.get(m.LeakageItem, item_id)
    if it is None or not scope.customer_allowed(it.site_id, it.customer_id):
        raise NotAllowed("Item not found or not in your scope.")
    ev = s.execute(select(m.LeakageEvidence).where(m.LeakageEvidence.leakage_item_id == it.id, m.LeakageEvidence.run_id == it.run_id)).scalars().all()
    rated_ids = [e.rated_event_id for e in ev if e.rated_event_id]
    inv_ids = [e.invoice_line_id for e in ev if e.invoice_line_id]
    rated = s.execute(select(m.RatedEvent).where(m.RatedEvent.id.in_(rated_ids or [-1])).order_by(m.RatedEvent.service_date, m.RatedEvent.id)).scalars().all()
    act_ids = [r.activity_event_id for r in rated if r.activity_event_id]
    acts = {a.id: a for a in s.execute(select(m.ActivityEvent).where(m.ActivityEvent.id.in_(act_ids[:limit] or [-1]))).scalars()}
    # labor minutes from the labor-order-time evidence source (per warehouse order, worker and day)
    orders = {a.order_ref for a in acts.values() if a.order_ref and a.event_type == "pick_line"}
    wh_of_order = _warehouse_orders_for_sales_orders(s, it.site_id, [a.order_ref for a in acts.values() if a.event_type == "order_completed" and a.order_ref])
    for v in wh_of_order.values():
        orders |= v
    labor = defaultdict(list)
    if orders:
        for rec_key, payload in s.execute(select(m.SourceSnapshot.record_key, m.SourceSnapshot.payload).join(
                m.ImportBatch, m.ImportBatch.id == m.SourceSnapshot.import_batch_id).where(
                m.ImportBatch.site_id == it.site_id, m.ImportBatch.status == "accepted", m.SourceSnapshot.source_table == "LABOR_ORDER_TIME",
                m.SourceSnapshot.payload["TI102X3ID"].as_string().in_(list(orders)[:2000]))).all():
            labor[payload["TI102X3ID"]].append(payload)
    manual_ids = [r.manual_entry_id for r in rated if r.manual_entry_id]
    manual = {x.id: x for x in s.execute(select(m.ManualBillableEntry).where(m.ManualBillableEntry.id.in_(manual_ids or [-1]))).scalars()}
    events = []
    for r in rated[:limit]:
        a = acts.get(r.activity_event_id)
        attrs = (a.attributes or {}) if a else {}
        if a and a.event_type == "order_completed":
            lab = [x for w in sorted(wh_of_order.get(a.order_ref, ())) for x in labor.get(w, [])]
        else:
            lab = labor.get(a.order_ref, []) if a and a.order_ref else []
        mins = sum(Decimal(x["SECONDS"]) for x in lab) / 60 if lab else None
        mn = manual.get(r.manual_entry_id)
        events.append(dict(rated_id=r.id, source_ref=a.source_ref if a else (f"MANUAL:{r.manual_entry_id}" if r.manual_entry_id else f"expense_line:{r.expense_line_id}"),
                           service_date=r.service_date, quantity=D(r.quantity_in_rate_uom), unit_price=r.unit_price, amount=D(r.amount_unrounded),
                           order_ref=a.order_ref if a else None, task_ref=a.task_ref if a else None, worker=attrs.get("worker"),
                           completed_at=attrs.get("completed_at") or attrs.get("received_at"), created_at=attrs.get("created_at"),
                           activity=attrs.get("activity"), material=a.material_id if a else None,
                           labor_minutes=r1(mins) if mins is not None else None, labor_lines=sum(int(x.get("LINES") or 0) for x in lab) if lab else None,
                           note=mn.note if mn else None, photo=mn.photo_ref if mn else None))
    invoices = []
    for l, h in s.execute(select(m.InvoiceLine, m.InvoiceHeader).join(m.InvoiceHeader, m.InvoiceHeader.id == m.InvoiceLine.invoice_header_id)
                          .where(m.InvoiceLine.id.in_(inv_ids or [-1])).order_by(m.InvoiceLine.id)).all():
        invoices.append(dict(id=l.id, invoice_number=h.invoice_number, line_number=l.line_number, charge=l.raw_charge_code, quantity=l.quantity,
                             uom=l.uom, unit_price=l.unit_price, amount=D(l.amount), posted_on=l.posted_on))
    rate = s.get(m.Rate, rated[0].rate_id) if rated else None
    version = s.get(m.RateCardVersion, rate.rate_card_version_id) if rate else None
    contract = s.get(m.Contract, version.contract_id) if version else None
    return dict(item=it, events=events, total_events=len(rated), invoices=invoices, rate=rate, version=version, contract=contract,
                labor_source=bool(labor))


def _warehouse_orders_for_sales_orders(s, site_id, order_ids):
    """Sales order -> warehouse orders that picked it (ORDER_LINE -> TASK_ORDER_LINE -> TI102C), from the active snapshots."""
    if not order_ids:
        return {}
    active = select(m.ImportBatch.id).where(m.ImportBatch.site_id == site_id, m.ImportBatch.status == "accepted")
    lines = s.execute(select(m.SourceSnapshot.payload["ORDER_LINE_ID"].as_string(), m.SourceSnapshot.payload["ORDER_ID"].as_string()).where(
        m.SourceSnapshot.import_batch_id.in_(active), m.SourceSnapshot.source_table == "ORDER_LINE",
        m.SourceSnapshot.payload["ORDER_ID"].as_string().in_(list(order_ids)))).all()
    line_order = dict(lines)
    if not line_order:
        return {}
    tasks = s.execute(select(m.SourceSnapshot.payload["TASK_ID"].as_string(), m.SourceSnapshot.payload["ORDER_LINE_ID"].as_string()).where(
        m.SourceSnapshot.import_batch_id.in_(active), m.SourceSnapshot.source_table == "TASK_ORDER_LINE",
        m.SourceSnapshot.payload["ORDER_LINE_ID"].as_string().in_(list(line_order)))).all()
    task_order = {t: line_order[l] for t, l in tasks}
    out = defaultdict(set)
    for task, wh in s.execute(select(m.ActivityEvent.task_ref, m.ActivityEvent.order_ref).where(
            m.ActivityEvent.site_id == site_id, m.ActivityEvent.event_type == "pick_line", m.ActivityEvent.task_ref.in_(list(task_order) or ["-"]))).all():
        out[task_order[task]].add(wh)
    return out


# ------------------------------------------------------------------ reconciliation matrix
def reconciliation(s, scope, site_id, period_id):
    _check_site(scope, site_id)
    period = s.get(m.BillingPeriod, period_id)
    run = period_run(s, period)
    if run is None:
        return dict(run=None, rows=[], totals=None, charges=[], customers=[])
    rows = s.execute(select(m.ReconLine).where(m.ReconLine.run_id == run.id, _cust_filter(m.ReconLine.customer_id, scope, site_id))).scalars().all()
    custs, charges = customer_names(s, site_id), charge_names(s)
    out = []
    for r in rows:
        out.append(dict(customer_id=r.customer_id, customer=custs[r.customer_id].code, customer_name=custs[r.customer_id].name,
                        charge_id=r.charge_code_id, charge=charges[r.charge_code_id].code, billable_qty=D(r.billable_quantity),
                        billed_qty=D(r.billed_quantity), billable=D(r.billable_amount), billed=D(r.billed_amount), gap=D(r.gap_amount),
                        status=r.status, rules=r.rules_matched or [], note=r.note))
    out.sort(key=lambda x: (x["customer"], x["charge"]))
    def tot(rows_):
        return dict(billable=sum((x["billable"] for x in rows_), ZERO), billed=sum((x["billed"] for x in rows_), ZERO),
                    gap=sum((x["gap"] for x in rows_), ZERO))
    by_customer = {c: tot([x for x in out if x["customer"] == c]) for c in sorted({x["customer"] for x in out})}
    by_charge = {c: tot([x for x in out if x["charge"] == c]) for c in sorted({x["charge"] for x in out})}
    return dict(run=run, rows=out, totals=tot(out), by_customer=by_customer, by_charge=by_charge,
                charges=sorted(by_charge), customers=sorted(by_customer))


def recon_cell(s, scope, site_id, period_id, customer_id, charge_id, limit=500):
    _check_customer(s, scope, site_id, customer_id)
    period = s.get(m.BillingPeriod, period_id)
    run = period_run(s, period)
    rated = s.execute(select(m.RatedEvent).where(m.RatedEvent.run_id == run.id, m.RatedEvent.customer_id == customer_id,
                                                 m.RatedEvent.charge_code_id == charge_id).order_by(m.RatedEvent.service_date, m.RatedEvent.id)
                      .limit(limit)).scalars().all()
    n = s.execute(select(func.count()).select_from(m.RatedEvent).where(m.RatedEvent.run_id == run.id, m.RatedEvent.customer_id == customer_id,
                                                                       m.RatedEvent.charge_code_id == charge_id)).scalar()
    acts = {a.id: a for a in s.execute(select(m.ActivityEvent).where(m.ActivityEvent.id.in_([r.activity_event_id for r in rated if r.activity_event_id] or [-1]))).scalars()}
    inv = s.execute(select(m.InvoiceLine, m.InvoiceHeader).join(m.InvoiceHeader, m.InvoiceHeader.id == m.InvoiceLine.invoice_header_id).where(
        m.InvoiceHeader.period_id == period_id, m.InvoiceHeader.customer_id == customer_id, m.InvoiceLine.charge_code_id == charge_id,
        m.InvoiceHeader.import_batch_id.in_(run.input_batch_ids or [-1]))).all()
    return dict(events=[dict(r=r, a=acts.get(r.activity_event_id)) for r in rated], total=n,
                invoices=[dict(l=l, h=h) for l, h in inv])


# ------------------------------------------------------------------ customers and P&L
def customers_pnl(s, scope, site_id, period_id):
    _check_site(scope, site_id)
    period = s.get(m.BillingPeriod, period_id)
    run = period_run(s, period)
    if run is None:
        return dict(available=False, reason="No calculation run for this period yet.", rows=[], site=None, run=None)
    custs = customer_names(s, site_id)
    rows = []
    allowed = scope.customers(site_id)
    for p in s.execute(select(m.CustomerProfitability).where(m.CustomerProfitability.run_id == run.id)).scalars():
        if allowed is not None and p.customer_id not in allowed:
            continue
        c = custs[p.customer_id]
        rows.append(dict(customer_id=c.id, customer=c.code, customer_name=c.name, revenue=D(p.revenue_billed), direct_cost=D(p.direct_cost),
                         shared_cost=D(p.allocated_shared_cost), total_cost=D(p.total_cost), site_profit=D(p.site_profit),
                         margin_pct=D(p.margin_pct) if p.margin_pct is not None else None, margin_display=r1(p.margin_pct),
                         orders=p.orders_completed, cost_per_order=D(p.cost_per_order) if p.cost_per_order is not None else None,
                         confirmed_not_invoiced=D(p.confirmed_not_invoiced),
                         target=get_setting(s, scope.tenant_id, f"margin_target.{c.code}", site_id)))
    rows.sort(key=lambda r: (r["margin_pct"] is None, r["margin_pct"] if r["margin_pct"] is not None else 0, r["customer"]))
    ss = s.execute(select(m.SiteSummary).where(m.SiteSummary.run_id == run.id)).scalar_one_or_none()
    full_scope = allowed is None
    if full_scope and ss is not None:
        site = dict(revenue=D(ss.revenue), total_cost=D(ss.total_cost), allocated_cost=D(ss.allocated_cost), unassigned_cost=D(ss.unassigned_cost),
                    site_profit=D(ss.site_profit), margin_pct=D(ss.margin_pct) if ss.margin_pct is not None else None,
                    margin_display=r1(ss.margin_pct), central_overhead=D(ss.central_overhead_unallocated) if ss.central_overhead_unallocated is not None else None,
                    profit_before_financing_tax=D(ss.profit_before_financing_tax) if ss.profit_before_financing_tax is not None else None, scoped=False)
    else:
        rev = sum((r["revenue"] for r in rows), ZERO)
        cost = sum((r["total_cost"] for r in rows), ZERO)  # replaced by None below when the period has no expense data
        prof = rev - cost
        site = dict(revenue=rev, total_cost=cost, allocated_cost=cost, unassigned_cost=None, site_profit=prof, margin_pct=pct(prof, rev),
                    margin_display=r1(pct(prof, rev)), central_overhead=None, profit_before_financing_tax=None, scoped=True)
    has_costs = s.execute(select(func.count()).select_from(m.CostAllocation).where(m.CostAllocation.run_id == run.id)).scalar() > 0
    if not has_costs:  # P5: no expense data is not zero cost
        for r in rows:
            r.update(total_cost=None, direct_cost=None, shared_cost=None, site_profit=None, margin_pct=None, margin_display=None, cost_per_order=None)
        site.update(total_cost=None, allocated_cost=None, unassigned_cost=None, site_profit=None, margin_pct=None, margin_display=None,
                    profit_before_financing_tax=None)
    return dict(available=True, rows=rows, site=site, run=run, costs_available=has_costs,
                cost_note=None if has_costs else "No expense data for this period: cost, site profit and margin are not available.")


def customer_detail(s, scope, site_id, period_id, customer_id):
    _check_customer(s, scope, site_id, customer_id)
    pnl = customers_pnl(s, scope, site_id, period_id)
    row = next((r for r in pnl["rows"] if r["customer_id"] == customer_id), None)
    cust = s.get(m.Customer, customer_id)
    run = pnl["run"]
    cats = []
    if run is not None:
        drivers = {d.id: d for d in s.execute(select(m.AllocationDriver).where(m.AllocationDriver.site_id == site_id)).scalars()}
        agg = defaultdict(lambda: dict(amount=ZERO, methods=set(), share=None))
        for a in s.execute(select(m.CostAllocation).where(m.CostAllocation.run_id == run.id, m.CostAllocation.customer_id == customer_id)).scalars():
            x = agg[a.expense_category]
            x["amount"] += D(a.amount)
            if a.method == "direct":
                x["methods"].add("Direct")
            else:
                d = drivers.get(a.driver_id)
                x["methods"].add(DRIVER_LABELS.get(d.driver_type, d.driver_type) if d else "Driver")
                x["share"] = D(a.share) if a.share is not None else None
                x["proxy"] = bool(d and d.is_proxy)
        for k in EXPENSE_CATEGORIES:
            if k in agg:
                cats.append(dict(category=k, label=CATEGORY_LABELS[k], amount=agg[k]["amount"], method=", ".join(sorted(agg[k]["methods"])),
                                 share=agg[k]["share"], proxy=agg[k].get("proxy", False)))
    # C3 trend: closed periods only
    trend = []
    for p in periods(s, scope, site_id):
        if p.status != "closed" or not p.close_run_id:
            continue
        cp = s.execute(select(m.CustomerProfitability).where(m.CustomerProfitability.run_id == p.close_run_id,
                                                             m.CustomerProfitability.customer_id == customer_id)).scalar_one_or_none()
        ss = s.execute(select(m.SiteSummary).where(m.SiteSummary.run_id == p.close_run_id)).scalar_one_or_none()
        if cp is not None and cp.margin_pct is not None:
            trend.append(dict(period=p.label, margin=r1(cp.margin_pct), site_margin=r1(ss.margin_pct) if ss and ss.margin_pct is not None else None))
    return dict(customer=cust, row=row, site=pnl["site"], categories=cats, trend=trend, run=run,
                insight=margin_insight(s, scope, site_id, period_id, customer_id, row, pnl) if row else None)


def margin_insight(s, scope, site_id, period_id, customer_id, row, pnl):
    """9.5 'Why is margin low?' — compare the customer with the site on measured drivers only."""
    site = pnl["site"]
    target = row.get("target")
    below_site = site and site["margin_pct"] is not None and row["margin_pct"] is not None and row["margin_pct"] < site["margin_pct"]
    below_target = target is not None and row["margin_pct"] is not None and row["margin_pct"] < target
    if not (below_site or below_target):
        return dict(applies=False)
    what = f"{row['customer']} margin is {row['margin_display']}%"
    if below_site:
        what += f", below the site margin of {site['margin_display']}%"
    if below_target:
        what += f" and below its target of {target}%"
    what += "."
    drivers = []
    run = pnl["run"]
    rows = pnl["rows"]
    # 1. labor cost per order (labor categories) vs site
    labor_cats = ("warehouse_operators", "forklift_operators", "supervisors")
    alloc = defaultdict(lambda: defaultdict(lambda: ZERO))
    for a in s.execute(select(m.CostAllocation).where(m.CostAllocation.run_id == run.id, m.CostAllocation.customer_id.is_not(None))).scalars():
        alloc[a.customer_id][a.expense_category] += D(a.amount)
    total_orders = sum((r["orders"] or 0) for r in rows)
    if row["orders"] and total_orders:
        c_lab = sum((alloc[customer_id][k] for k in labor_cats), ZERO) / row["orders"]
        s_lab = sum((alloc[c][k] for c in alloc for k in labor_cats), ZERO) / total_orders
        if c_lab > s_lab:
            drivers.append(f"Labor cost per order {money_text(c_lab)} vs site {money_text(s_lab)}.")
    # 2. freight cost vs freight charged (markup actually earned)
    charges = charge_names(s)
    transport_ids = [cid for cid, c in charges.items() if c.category == "transport"]
    def freight(cid):
        cost = alloc[cid]["transport_carriers"] if cid is not None else sum((alloc[c]["transport_carriers"] for c in alloc), ZERO)
        q = select(func.coalesce(func.sum(m.ReconLine.billed_amount), 0)).where(m.ReconLine.run_id == run.id, m.ReconLine.charge_code_id.in_(transport_ids or [-1]))
        if cid is not None:
            q = q.where(m.ReconLine.customer_id == cid)
        return cost, D(s.execute(q).scalar())
    fc, fb = freight(customer_id)
    sc, sb = freight(None)
    if fc and sc:
        c_mu, s_mu = pct(fb - fc, fc), pct(sb - sc, sc)
        if c_mu is not None and s_mu is not None and c_mu < s_mu:
            drivers.append(f"Freight markup earned {r1(c_mu)}% vs site {r1(s_mu)}% (charged {money_text(fb)} on {money_text(fc)} cost).")
    # 3. storage space share vs storage revenue share
    storage_ids = [cid for cid, c in charges.items() if c.category == "storage"]
    rent_c, rent_s = alloc[customer_id]["warehouse_rent"], sum((alloc[c]["warehouse_rent"] for c in alloc), ZERO)
    st_c = D(s.execute(select(func.coalesce(func.sum(m.ReconLine.billed_amount), 0)).where(m.ReconLine.run_id == run.id, m.ReconLine.customer_id == customer_id, m.ReconLine.charge_code_id.in_(storage_ids or [-1]))).scalar())
    st_s = D(s.execute(select(func.coalesce(func.sum(m.ReconLine.billed_amount), 0)).where(m.ReconLine.run_id == run.id, m.ReconLine.charge_code_id.in_(storage_ids or [-1]))).scalar())
    if rent_s and st_s:
        space, revshare = rent_c / rent_s * 100, st_c / st_s * 100
        if space > revshare:
            drivers.append(f"Storage space share {r1(space)}% vs storage revenue share {r1(revshare)}%.")
    # 4. open leakage for the customer
    open_gap = sum((D(i.gap_amount) for i in s.execute(select(m.LeakageItem).where(
        m.LeakageItem.customer_id == customer_id, m.LeakageItem.status.in_(OPEN_STATUSES), m.LeakageItem.direction == "under")).scalars()), ZERO)
    if open_gap > 0:
        drivers.append(f"Open under-billing of {money_text(open_gap)} still under review.")
    # 5. rate-card age
    ages = []
    period = s.get(m.BillingPeriod, period_id)
    for v in s.execute(select(m.RateCardVersion).join(m.Contract, m.Contract.id == m.RateCardVersion.contract_id).where(
            m.Contract.customer_id == customer_id, m.RateCardVersion.status == "approved")).scalars():
        if v.effective_from:
            ages.append((period.end_date - v.effective_from).days)
    site_ages = []
    for v, c in s.execute(select(m.RateCardVersion, m.Contract).join(m.Contract, m.Contract.id == m.RateCardVersion.contract_id).join(
            m.Customer, m.Customer.id == m.Contract.customer_id).where(m.Customer.site_id == site_id, m.RateCardVersion.status == "approved")).all():
        if v.effective_from:
            site_ages.append((period.end_date - v.effective_from).days)
    if ages and site_ages and max(ages) > sum(site_ages) / len(site_ages):
        drivers.append(f"Rate card in force for {max(ages)} days vs site average {round(sum(site_ages) / len(site_ages))} days.")
    return dict(applies=True, what=what, why=drivers or ["Cause not established from the data."],
                check="Check task mix, labor cost per transaction, freight exposure, storage use and rate-card terms before concluding the customer is underpriced.")


# ------------------------------------------------------------------ costs and allocation
def costs(s, scope, site_id, period_id):
    """Costs page. Expense data is site-level: only users with the whole site in scope see it."""
    _check_site(scope, site_id)
    if scope.customers(site_id) is not None:
        raise NotAllowed("Cost pages need access to the whole site.")
    period = s.get(m.BillingPeriod, period_id)
    run = period_run(s, period)
    batches = run.input_batch_ids if run else None
    q = select(m.ExpenseLine).where(m.ExpenseLine.site_id == site_id, m.ExpenseLine.period_id == period_id)
    if batches is not None:
        q = q.where(or_(m.ExpenseLine.import_batch_id.in_(batches or [-1]), m.ExpenseLine.import_batch_id.is_(None)))
    else:
        q = q.join(m.ImportBatch, m.ImportBatch.id == m.ExpenseLine.import_batch_id, isouter=True).where(
            or_(m.ImportBatch.status == "accepted", m.ExpenseLine.import_batch_id.is_(None)))
    lines = s.execute(q.order_by(m.ExpenseLine.id)).scalars().all()
    total = sum((D(x.amount) for x in lines), ZERO)
    by_cat = defaultdict(list)
    for x in lines:
        by_cat[x.expense_category].append(x)
    k1 = []
    for cat in EXPENSE_CATEGORIES:
        if cat in by_cat:
            amt = sum((D(x.amount) for x in by_cat[cat]), ZERO)
            k1.append(dict(category=cat, label=CATEGORY_LABELS[cat], amount=amt, share=pct(amt, total),
                           lines=[dict(description=x.description, amount=D(x.amount), id=x.id) for x in by_cat[cat]]))
    k1.sort(key=lambda r: -r["amount"])
    rec = ledger_by_recipient(lines)
    who = [dict(recipient=k, label=RECIPIENT_LABELS[k], amount=rec["by_recipient"].get(k, ZERO), treatment=RECIPIENT_TREATMENT[k]) for k in PAYMENT_RECIPIENTS]
    if rec["by_recipient"].get("unclassified"):
        who.append(dict(recipient="unclassified", label="Unclassified", amount=rec["by_recipient"]["unclassified"], treatment="Fix the payment recipient type"))
    matrix, exceptions, customers = [], [], []
    allocated = unassigned = ZERO
    if run is not None:
        custs = customer_names(s, site_id)
        allocs = s.execute(select(m.CostAllocation).where(m.CostAllocation.run_id == run.id)).scalars().all()
        drivers = {d.id: d for d in s.execute(select(m.AllocationDriver).where(m.AllocationDriver.site_id == site_id)).scalars()}
        cust_ids = sorted({a.customer_id for a in allocs if a.customer_id}, key=lambda c: custs[c].code)
        customers = [dict(id=c, code=custs[c].code) for c in cust_ids]
        for cat in EXPENSE_CATEGORIES:
            rows = [a for a in allocs if a.expense_category == cat]
            if not rows:
                continue
            cells = {c: sum((D(a.amount) for a in rows if a.customer_id == c), ZERO) for c in cust_ids}
            un = sum((D(a.amount) for a in rows if a.customer_id is None), ZERO)
            pool = sum((D(x.amount) for x in by_cat.get(cat, [])), ZERO)
            drv = next((drivers.get(a.driver_id) for a in rows if a.driver_id), None)
            exc = next((a.exception for a in rows if a.exception and a.exception != "unassigned_activity"), None)
            methods = sorted({"Direct" if a.method == "direct" else DRIVER_LABELS.get(drivers[a.driver_id].driver_type) for a in rows if a.method != "none" and (a.method == "direct" or a.driver_id)})
            matrix.append(dict(category=cat, label=CATEGORY_LABELS[cat], cells=cells, unassigned=un, pool=pool,
                               check=pool == sum(cells.values(), ZERO) + un, driver=drv, method=", ".join(methods) or "None",
                               proxy=bool(drv and drv.is_proxy), exception=exc))
            allocated += sum(cells.values(), ZERO)
            unassigned += un
            if exc:
                exceptions.append(dict(category=cat, label=CATEGORY_LABELS[cat], exception=exc, exception_label=EXCEPTION_LABELS.get(exc, exc), amount=un,
                                       fix="/admin/drivers"))
    return dict(run=run, total=total, allocated=allocated, unassigned=unassigned, exceptions=exceptions, k1=k1, who=who,
                recipients_total=rec["recipient_total"], ledger_reconciles=rec["reconciles"], difference=rec["difference"],
                matrix=matrix, customers=customers, lines=lines)


def allocation_cell(s, scope, site_id, period_id, category, customer_id=None):
    data = costs(s, scope, site_id, period_id)
    run = data["run"]
    rows = s.execute(select(m.CostAllocation).where(m.CostAllocation.run_id == run.id, m.CostAllocation.expense_category == category)).scalars().all()
    qty = []
    drv_ids = {a.driver_id for a in rows if a.driver_id}
    if drv_ids:
        for q in s.execute(select(m.AllocationDriverQuantity).where(m.AllocationDriverQuantity.driver_id.in_(drv_ids),
                                                                    m.AllocationDriverQuantity.period_id == period_id)).scalars():
            qty.append(q)
    lines = [x for x in data["lines"] if x.expense_category == category]
    return dict(rows=rows, quantities=qty, lines=lines, drivers=[s.get(m.AllocationDriver, d) for d in drv_ids])


def labor_attribution(s, scope, site_id, period_id):
    """7.7 labor attribution with coverage; worker level, finance controller only (enforced by the caller's permission)."""
    _check_site(scope, site_id)
    period = s.get(m.BillingPeriod, period_id)
    run = period_run(s, period)
    batches = run.input_batch_ids if run else [b for (b,) in s.execute(select(m.ImportBatch.id).where(m.ImportBatch.site_id == site_id, m.ImportBatch.status == "accepted"))]
    workers = s.execute(select(m.EmployeeLaborCost).where(m.EmployeeLaborCost.period_id == period_id, m.EmployeeLaborCost.import_batch_id.in_(batches or [-1]))).scalars().all()
    attrs = s.execute(select(m.LaborTimeAttribution).where(m.LaborTimeAttribution.period_id == period_id,
                                                           or_(m.LaborTimeAttribution.import_batch_id.in_(batches or [-1]), m.LaborTimeAttribution.import_batch_id.is_(None)))).scalars().all()
    basis = get_setting(s, scope.tenant_id, "labor.cost_basis", site_id)
    wc = [labor_engine.WorkerCost(w.worker_id, w.employer_type, w.role, D(w.paid_hours), D(w.loaded_cost)) for w in workers]
    at = [labor_engine.Attribution(a.worker_id, a.customer_id, D(a.hours), a.basis, a.unassigned_reason) for a in attrs]
    rows, errors = labor_engine.attribute(wc, at)
    custs = customer_names(s, site_id)
    return dict(basis=basis, rows=[dict(worker=r.worker_id, customer=custs[r.customer_id].code if r.customer_id else None, hours=r.hours,
                                        rate=r.loaded_rate, cost=r.cost, basis=r.basis, reason=r.unassigned_reason) for r in rows],
                errors=errors, coverage=labor_engine.coverage(rows), total=sum((r.cost for r in rows), ZERO),
                attributed=sum((r.cost for r in rows if r.customer_id is not None), ZERO),
                unassigned=sum((r.cost for r in rows if r.customer_id is None), ZERO))


# ------------------------------------------------------------------ data health
def data_health(s, scope, site_id, period_id):
    _check_site(scope, site_id)
    from ..ingest.sources import SOURCE_TYPES
    batches = s.execute(select(m.ImportBatch).where(m.ImportBatch.site_id == site_id).order_by(m.ImportBatch.id.desc())).scalars().all()
    fresh = []
    for code, st in SOURCE_TYPES.items():
        bs = [b for b in batches if b.source_type == code]
        act = [b for b in bs if b.status == "accepted"]
        last = bs[0] if bs else None
        fresh.append(dict(source=code, label=st.label, last=last, active_batches=len(act), rows=sum(b.accepted_count for b in act),
                          status="Missing" if not act else ("Rejected rows" if any(b.rejected_count for b in act) else "OK")))
    period = s.get(m.BillingPeriod, period_id) if period_id else None
    run = period_run(s, period) if period else None
    unresolved, cannot = [], []
    unresolved_value = None
    if run:
        allowed = scope.customers(site_id)
        for c in s.execute(select(m.CannotRateItem).where(m.CannotRateItem.run_id == run.id).order_by(m.CannotRateItem.id).limit(2000)).scalars():
            if allowed is not None and c.customer_id is not None and c.customer_id not in allowed:
                continue
            if allowed is not None and c.customer_id is None:
                continue
            (unresolved if c.reason == "no_customer" else cannot).append(c)
    counts = Counter()
    if run:
        for reason, n in s.execute(select(m.CannotRateItem.reason, func.count()).where(m.CannotRateItem.run_id == run.id).group_by(m.CannotRateItem.reason)):
            counts[reason] = n
    return dict(freshness=fresh, batches=batches[:50], run=run, unresolved=unresolved, cannot=cannot, counts=counts,
                unresolved_count=counts.get("no_customer", 0), cannot_count=sum(v for k, v in counts.items() if k != "no_customer"),
                unresolved_value=unresolved_value)


# ------------------------------------------------------------------ pilot results (7.9 by period)
def pilot_metrics(s, scope, site_id):
    out = []
    for p in periods(s, scope, site_id):
        run = period_run(s, p)
        if run is None:
            continue
        pm = period_leakage_metrics(s, scope, site_id, p.id)
        labor = labor_attribution(s, scope, site_id, p.id) if scope.customers(site_id) is None else None
        cannot = s.execute(select(func.count()).select_from(m.CannotRateItem).where(m.CannotRateItem.run_id == run.id, m.CannotRateItem.reason != "no_customer")).scalar()
        unresolved = s.execute(select(func.count()).select_from(m.CannotRateItem).where(m.CannotRateItem.run_id == run.id, m.CannotRateItem.reason == "no_customer")).scalar()
        ttc = (p.closed_at.date() - p.end_date).days if p.closed_at else None
        out.append(dict(period=p.label, **pm, time_to_close=ttc, labor_coverage=labor["coverage"] if labor else None,
                        cannot_rate_count=cannot, unresolved_count=unresolved))
    return out


def site_scope_full(scope: Scope, site_id: int) -> bool:
    return scope.site_allowed(site_id) and scope.customers(site_id) is None
