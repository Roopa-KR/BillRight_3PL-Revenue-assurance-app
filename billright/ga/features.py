"""GA features G1-G8. Each is additive: with no GA configuration, Release 1 results are unchanged.

G1 storage engine (``generate_storage_events``) · G2 mobile capture (Work capture page) · G3 daily run and alerts
(``daily_run``) · G4 surcharge rules (``apply_surcharge_rules``, used by the run) · G5 minimums and tiers checker
(``minimums_tiers``) · G6 contract drift (``contract_drift``, ``drift_alerts``) · G7 SLA penalty tracker
(``sla_exposure``) · G8 accounting export (``push_adjustments`` with an adapter interface).
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import func, select

from .. import models as m
from ..engine.money import ZERO, D, round_cents
from ..engine.storage import storage_quantities
from ..metrics import service as ms
from ..services.common import Actor, BusinessError, audit, get_setting, row_dict
from ..services.contracts import latest_run

# ------------------------------------------------------------------ G1 storage


def storage_preview(s, scope, site_id, period_id):
    policy = get_setting(s, scope.tenant_id, "storage.policy", site_id)
    period = s.get(m.BillingPeriod, period_id)
    snaps = s.execute(select(m.InventorySnapshot).join(m.ImportBatch, m.ImportBatch.id == m.InventorySnapshot.import_batch_id).where(
        m.InventorySnapshot.site_id == site_id, m.ImportBatch.status == "accepted", m.InventorySnapshot.snapshot_date >= period.start_date,
        m.InventorySnapshot.snapshot_date <= period.end_date)).scalars().all()
    if not snaps:
        return dict(available=False, reason="No daily inventory snapshots imported for this period (open decision 8).", policy=policy)
    if not policy:
        return dict(available=False, reason="Fill the setting 'storage.policy' (unit type, method, output unit, grace days, split-month rule).", policy=None)
    q = storage_quantities([dict(customer=x.customer_key, date=x.snapshot_date, unit_type=x.unit_type, quantity=x.quantity, received_date=x.received_date)
                            for x in snaps], period.start_date, period.end_date, policy)
    return dict(available=True, policy=policy, rows=sorted(q.items()), snapshots=len(snaps))


def generate_storage_events(s, actor: Actor, site_id, period_id):
    from ..ingest.importer import import_file
    from ..services.common import Actor as A
    scope = type("S", (), {"tenant_id": actor.tenant_id})
    prev = storage_preview(s, scope, site_id, period_id)
    if not prev["available"]:
        raise BusinessError(prev["reason"])
    period = s.get(m.BillingPeriod, period_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["STORAGE_REF", "CUSTOMER_CODE", "SERVICE_DATE", "QUANTITY", "UOM"])
    for cust, qty in prev["rows"]:
        w.writerow([f"G1-{cust}-{period.label}", cust, period.end_date.isoformat(), str(qty), prev["policy"]["output_uom"]])
    site = s.get(m.Site, site_id)
    return import_file(s, actor, site, "storage_activity", f"g1_storage_{period.label}.csv", buf.getvalue().encode(), scope_key=f"g1-storage-{period.label}")


# ------------------------------------------------------------------ G3 daily run and alerts
def daily_run(s, actor: Actor, site_id, today: dt.date | None = None) -> int:
    """Rate and run the rules for every open period up to today, then alert billing analysts of new items before invoicing."""
    from ..services.run import run_period
    today = today or dt.date.today()
    n = 0
    for p in s.execute(select(m.BillingPeriod).where(m.BillingPeriod.site_id == site_id, m.BillingPeriod.status == "open",
                                                     m.BillingPeriod.start_date <= today)).scalars().all():
        run = run_period(s, actor, p.id, "nightly")
        if run.status != "succeeded":
            continue
        new = s.execute(select(m.LeakageItem).where(m.LeakageItem.period_id == p.id, m.LeakageItem.alerted_at.is_(None),
                                                    m.LeakageItem.status == "new")).scalars().all()
        analysts = [u for (u,) in s.execute(select(m.UserRole.user_id).where(m.UserRole.role == "billing_analyst"))]
        cust = ms.customer_names(s, site_id)
        charges = ms.charge_names(s)
        for it in new:
            text = ms.what_happened(it, cust[it.customer_id], charges[it.charge_code_id], p.label)
            for uid in analysts:
                sc = s.execute(select(m.UserScope).where(m.UserScope.user_id == uid, m.UserScope.site_id == site_id)).scalars().all()
                if any(x.customer_id in (None, it.customer_id) for x in sc):
                    s.add(m.Alert(tenant_id=actor.tenant_id, site_id=site_id, kind="daily_leakage", user_id=uid, message=text,
                                  link=f"/leakage/{it.id}", entity="leakage_item", entity_id=it.id))
            it.alerted_at = dt.datetime.now(dt.timezone.utc)
            n += 1
    return n


# ------------------------------------------------------------------ G4 surcharge rules
SURCHARGE_KINDS = ("peak_season", "rush", "after_hours", "weekend", "cold_chain", "fuel")


def create_surcharge_rule(s, actor, site_id, name, kind, event_type, charge_id, parameters):
    if kind not in SURCHARGE_KINDS:
        raise BusinessError(f"Kind must be one of {', '.join(SURCHARGE_KINDS)}.")
    validate_surcharge_parameters(kind, parameters)
    cc = s.get(m.ChargeCode, charge_id)
    if cc is None or cc.category != "surcharge" or cc.event_type != event_type:
        raise BusinessError("Choose a surcharge charge code that prices the same event type.")
    r = m.SurchargeRule(tenant_id=actor.tenant_id, site_id=site_id, name=name, kind=kind, applies_to_event_type=event_type,
                        surcharge_charge_code_id=charge_id, parameters=parameters, status="draft", created_by=actor.user_id)
    s.add(r)
    s.flush()
    audit(s, actor, "surcharge_rule.create", "surcharge_rule", r.id, None, row_dict(r))
    return r


def approve_surcharge_rule(s, actor, rule_id):
    r = s.get(m.SurchargeRule, rule_id)
    if r.created_by == actor.user_id:
        raise BusinessError("Segregation of duties: the author of a surcharge rule cannot approve it.")
    r.status, r.approved_by = "approved", actor.user_id
    audit(s, actor, "surcharge_rule.approve", "surcharge_rule", r.id, {"status": "draft"}, {"status": "approved"})


def validate_surcharge_parameters(kind, p):
    need = {"peak_season": ("from", "to"), "rush": ("attribute", "values"), "after_hours": ("start", "end"), "weekend": ("weekdays",),
            "cold_chain": ("attribute", "values"), "fuel": ("from", "to")}[kind]
    missing = [k for k in need if k not in p]
    if missing:
        raise BusinessError(f"Parameters missing: {', '.join(missing)}.")


def surcharge_flag(rule_kind, p, event) -> bool:
    attrs = event.attributes or {}
    ts = attrs.get(p.get("timestamp_attribute", "completed_at"))
    if rule_kind in ("peak_season", "fuel"):
        md = event.service_date.strftime("%m-%d") if rule_kind == "peak_season" else event.service_date.isoformat()
        lo, hi = p["from"], p["to"]
        return lo <= md <= hi if lo <= hi else (md >= lo or md <= hi)
    if rule_kind == "weekend":
        return event.service_date.weekday() in set(p["weekdays"])
    if rule_kind in ("rush", "cold_chain"):
        return attrs.get(p["attribute"]) in set(p["values"])
    if rule_kind == "after_hours":
        if not ts:
            return False
        t = dt.datetime.fromisoformat(ts).strftime("%H:%M")
        return t >= p["start"] or t < p["end"] if p["start"] > p["end"] else p["start"] <= t < p["end"]
    return False


def apply_surcharge_rules(rules, events):
    """Mark events that meet an approved rule with attribute ``surcharge:<CHARGE>`` = true; the surcharge rate's
    condition {"surcharge:<CHARGE>": true} then prices them, and L4 flags them when they are not billed."""
    if not rules:
        return events
    out = []
    for e in events:
        extra = {}
        for r in rules:
            if r["event_type"] == e.event_type and surcharge_flag(r["kind"], r["parameters"], e):
                extra[f"surcharge:{r['charge']}"] = True
        if extra:
            import dataclasses
            e = dataclasses.replace(e, attributes=dict(e.attributes or {}, **extra))
        out.append(e)
    return out


# ------------------------------------------------------------------ G5 minimums and tiers checker
def minimums_tiers(s, scope, site_id, period_id):
    period = s.get(m.BillingPeriod, period_id)
    run = ms.period_run(s, period)
    if run is None:
        return []
    custs, charges = ms.customer_names(s, site_id), ms.charge_names(s)
    recon = {(r.customer_id, r.charge_code_id): r for r in s.execute(select(m.ReconLine).where(m.ReconLine.run_id == run.id)).scalars()}
    out = []
    for b in s.execute(select(m.BillableLine).where(m.BillableLine.run_id == run.id)).scalars():
        if not scope.customer_allowed(site_id, b.customer_id):
            continue
        rates = [s.get(m.Rate, rid) for rid in (b.rate_ids or [])]
        gov = rates[-1] if rates else None
        if gov is None or not (gov.minimum_charge or gov.tiers):
            continue
        rc = recon.get((b.customer_id, b.charge_code_id))
        billed = D(rc.billed_amount) if rc else ZERO
        if gov.minimum_charge:
            ok = billed >= D(gov.minimum_charge) or not b.minimum_applied
            out.append(dict(customer=custs[b.customer_id].code, charge=charges[b.charge_code_id].code, check="Monthly minimum",
                            expected=f"minimum {D(gov.minimum_charge)} {'applies' if b.minimum_applied else 'not reached'}", billable=D(b.billable_amount),
                            billed=billed, ok=ok))
        if gov.tiers:
            ok = rc is not None and D(rc.billed_amount) == D(b.billable_amount)
            out.append(dict(customer=custs[b.customer_id].code, charge=charges[b.charge_code_id].code, check="Volume tier",
                            expected=b.tier_applied or "", billable=D(b.billable_amount), billed=billed, ok=ok))
    return out


# ------------------------------------------------------------------ G6 contract drift
def contract_drift(s, scope, site_id, period_id):
    period = s.get(m.BillingPeriod, period_id)
    days = get_setting(s, scope.tenant_id, "drift.renewal_warning_days", site_id)
    out = []
    custs = ms.customer_names(s, site_id)
    for it in s.execute(select(m.LeakageItem).where(m.LeakageItem.period_id == period_id, m.LeakageItem.rule_code == "L6")).scalars():
        if scope.customer_allowed(site_id, it.customer_id):
            out.append(dict(kind="expired_rate", customer=custs[it.customer_id].code, message=f"An expired rate is still being billed (item {it.id}).", link=f"/leakage/{it.id}"))
    for k in s.execute(select(m.Contract).join(m.Customer, m.Customer.id == m.Contract.customer_id).where(m.Customer.site_id == site_id)).scalars():
        if not scope.customer_allowed(site_id, k.customer_id) or k.end_date is None:
            continue
        if k.end_date < period.start_date:
            out.append(dict(kind="contract_ended", customer=custs[k.customer_id].code, message=f"Contract {k.reference} ended {k.end_date:%d %b %Y} and is still in use.", link="/contracts"))
        elif days is not None and (k.end_date - period.end_date).days <= days:
            out.append(dict(kind="renewal", customer=custs[k.customer_id].code, message=f"Contract {k.reference} ends {k.end_date:%d %b %Y} (renewal within {days} days).", link="/contracts"))
    if days is None:
        out.append(dict(kind="setting", customer="—", message="Renewal warnings are off: fill the setting drift.renewal_warning_days.", link="/admin?tab=settings"))
    pnl = ms.customers_pnl(s, scope, site_id, period_id)
    for r in pnl["rows"]:
        if r["target"] is not None and r["margin_pct"] is not None and r["margin_pct"] < r["target"]:
            out.append(dict(kind="margin_below_target", customer=r["customer"], message=f"Margin {r['margin_display']}% is below its target {r['target']}%.",
                            link=f"/customers/{r['customer_id']}"))
    return out


def drift_alerts(s, actor, site_id, period_id):
    scope = type("S", (), {"tenant_id": actor.tenant_id, "customer_allowed": lambda self, a, b: True, "customers": lambda self, a: None,
                           "site_allowed": lambda self, a: True})()
    n = 0
    for d in contract_drift(s, scope, site_id, period_id):
        if d["kind"] == "setting":
            continue
        s.add(m.Alert(tenant_id=actor.tenant_id, site_id=site_id, kind="drift_" + d["kind"], user_id=None, message=f"{d['customer']}: {d['message']}", link=d["link"]))
        n += 1
    return n


# ------------------------------------------------------------------ G7 SLA penalties
def sla_exposure(s, scope, site_id, period_id):
    period = s.get(m.BillingPeriod, period_id)
    rules = s.execute(select(m.SlaRule).join(m.Customer, m.Customer.id == m.SlaRule.customer_id).where(m.Customer.site_id == site_id)).scalars().all()
    if not rules:
        return dict(available=False, reason="No SLA rules set up.")
    batches = [b for (b,) in s.execute(select(m.ImportBatch.id).where(m.ImportBatch.site_id == site_id, m.ImportBatch.status == "accepted"))]
    so = s.execute(select(m.SourceSnapshot.payload).where(m.SourceSnapshot.import_batch_id.in_(batches or [-1]), m.SourceSnapshot.source_table == "SHIPMENT_ORDER")).scalars().all()
    if not so:
        return dict(available=False, reason="No shipment-order data imported (needed to measure lateness).")
    heads = {p["ORDER_ID"]: p["CUSTOMER_CODE"] for p in s.execute(select(m.SourceSnapshot.payload).where(
        m.SourceSnapshot.import_batch_id.in_(batches), m.SourceSnapshot.source_table == "ORDER_HEADER")).scalars()}
    code_to_id = {c.code: c.id for c in ms.customer_names(s, site_id).values()}
    late = defaultdict(int)
    for p in so:
        staged = p.get("STAGED_AT")
        if not staged:
            continue
        day = dt.datetime.fromisoformat(staged).date()
        if not (period.start_date <= day <= period.end_date):
            continue
        cid = code_to_id.get(heads.get(p["ORDER_ID"]))
        if cid is None:
            continue
        late[cid, Decimal(p.get("LATE_SECONDS") or 0)] += 1
    out = []
    custs = ms.customer_names(s, site_id)
    for r in rules:
        if not scope.customer_allowed(site_id, r.customer_id) or r.valid_from > period.end_date or (r.valid_to and r.valid_to < period.start_date):
            continue
        n = sum(cnt for (cid, secs), cnt in late.items() if cid == r.customer_id and secs > r.allowed_late_seconds)
        exposure = D(r.penalty_amount) * n if r.penalty_basis == "per_order" else (D(r.penalty_amount) if n else ZERO)
        if r.cap_amount is not None:
            exposure = min(exposure, D(r.cap_amount))
        booked = ZERO
        if r.penalty_charge_code:
            booked = abs(D(s.execute(select(func.coalesce(func.sum(m.InvoiceLine.amount), 0)).join(m.InvoiceHeader, m.InvoiceHeader.id == m.InvoiceLine.invoice_header_id)
                                     .where(m.InvoiceHeader.customer_id == r.customer_id, m.InvoiceHeader.period_id == period_id,
                                            m.InvoiceHeader.import_batch_id.in_(batches), m.InvoiceLine.raw_charge_code == r.penalty_charge_code)).scalar()))
        out.append(dict(customer=custs[r.customer_id].code, milestone=r.milestone, late_orders=n, penalty=D(r.penalty_amount), basis=r.penalty_basis,
                        cap=D(r.cap_amount) if r.cap_amount is not None else None, exposure=round_cents(exposure), booked=booked,
                        difference=round_cents(exposure) - booked, reference=r.contract_reference))
    return dict(available=True, rows=out, total_exposure=sum((x["exposure"] for x in out), ZERO), total_booked=sum((x["booked"] for x in out), ZERO))


# ------------------------------------------------------------------ G8 accounting export
class AccountingAdapter:
    """Interface: push approved adjustments and credit notes; return (file name, bytes, external reference)."""
    name = "base"

    def push(self, rows: list[dict], context: dict):
        raise NotImplementedError


class FileAdapter(AccountingAdapter):
    """Documented starting format (docs/DATA_CONTRACT.md, 'Accounting export file'): one CSV line per adjustment."""
    name = "file"
    COLUMNS = ["DOCUMENT_TYPE", "CUSTOMER_CODE", "ORIGINAL_INVOICE", "CHARGE_CODE", "QUANTITY", "UNIT_PRICE", "AMOUNT", "CURRENCY", "PERIOD",
               "LEAKAGE_ITEM_ID", "REASON", "APPROVER"]

    def push(self, rows, context):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(self.COLUMNS)
        for r in rows:
            w.writerow([r.get(k) if r.get(k) is not None else "" for k in self.COLUMNS])
        data = buf.getvalue().encode()
        name = f"billright_adjustments_{context['site']}_{context['period']}_{hashlib.sha256(data).hexdigest()[:8]}.csv"
        return name, data, None


class _NotYet(AccountingAdapter):
    def push(self, rows, context):
        raise BusinessError(f"The {self.name} adapter is an extension point; use the 'file' adapter until it is implemented.")


ADAPTERS = {"file": FileAdapter, **{n: type(n.title() + "Adapter", (_NotYet,), {"name": n}) for n in ("quickbooks", "netsuite", "sap", "dynamics")}}


def push_adjustments(s, cur, site_id, period_id):
    from ..services.exports import adjustment_rows
    adapter_name = get_setting(s, cur.tenant_id, "accounting.adapter")
    if adapter_name is None:
        raise BusinessError("Fill the setting 'accounting.adapter' first.")
    period = s.get(m.BillingPeriod, period_id)
    site = s.get(m.Site, site_id)
    done = set()
    for e in s.execute(select(m.AccountingExport).where(m.AccountingExport.period_id == period_id, m.AccountingExport.status == "sent")).scalars():
        done |= set(e.item_ids)
    rows = []
    ids = []
    for r in adjustment_rows(s, cur, site_id, period_id):
        iid = int(r[7].split("-")[1])
        if iid in done:
            continue
        ids.append(iid)
        rows.append(dict(DOCUMENT_TYPE="credit_note" if D(r[5]) < 0 else "invoice_adjustment", CUSTOMER_CODE=r[0], ORIGINAL_INVOICE=r[1], CHARGE_CODE=r[2],
                         QUANTITY=r[3], UNIT_PRICE=r[4], AMOUNT=r[5], CURRENCY=site.currency_code, PERIOD=period.label, LEAKAGE_ITEM_ID=r[7], REASON=r[6], APPROVER=r[8]))
    if not rows:
        raise BusinessError("No approved 'Bill now' or 'Issue credit' items left to export for this period.")
    name, data, _ref = ADAPTERS[adapter_name]().push(rows, dict(site=site.code, period=period.label))
    e = m.AccountingExport(tenant_id=cur.tenant_id, site_id=site_id, period_id=period_id, adapter=adapter_name, item_ids=ids, file_name=name, content=data,
                           status="sent", created_by=cur.user.id)
    s.add(e)
    s.flush()
    audit(s, cur.actor, "accounting.export", "accounting_export", e.id, None, {"items": ids, "file": name})
    return e
