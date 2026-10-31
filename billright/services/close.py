"""Monthly / weekly period close (F10, workflow 8.6). Each step is a check the app runs and shows as passed or failed.

Closing freezes the period: inputs, run, results and exports become read-only (imports, manual entries,
decisions and runs on the period are refused). Reopening needs a controller and a reason and increments the
close version; the earlier close run and its results stay stored.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, or_, select

from .. import models as m
from ..engine.money import ZERO, D
from ..engine.pnl import ledger_by_recipient
from ..metrics import service as ms
from .common import Actor, BusinessError, audit, get_setting
from .contracts import latest_run

CHECK_TITLES = {
    1: "Payroll hours and loaded pay reconcile to labor expense; temporary-labor invoices reconciled separately",
    2: "Approved vendor and carrier invoices captured for the period",
    3: "Every WMS transaction maps to a customer",
    4: "Every cost pool allocated with an approved driver and reconciled to the ledger",
    5: "Billable activity rated with versioned contract terms; billed vs billable reconciled; no item New or Under review",
    6: "Customer P&Ls reviewed: margins outside range, manual adjustments and allocation exceptions acknowledged",
    7: "Receivables and payables reported separately (export)",
}
ACKNOWLEDGEABLE = {3, 6}


@dataclass
class Check:
    no: int
    title: str
    passed: bool
    detail: str
    items: list = field(default_factory=list)
    acknowledged: dict | None = None
    can_acknowledge: bool = False

    @property
    def ok(self):
        return self.passed or self.acknowledged is not None


def run_checks(s, actor: Actor, scope, period_id: int) -> list[Check]:
    period = s.get(m.BillingPeriod, period_id)
    site_id = period.site_id
    run = latest_run(s, period_id) if period.status != "closed" else s.get(m.CalculationRun, period.close_run_id)
    acks = {a.check_no: a for a in s.execute(select(m.CloseAcknowledgement).where(m.CloseAcknowledgement.period_id == period_id,
                                                                                 m.CloseAcknowledgement.close_version == period.close_version)).scalars()}
    checks = []
    from .run import inputs_changed_since
    active = sorted(b for (b,) in s.execute(select(m.ImportBatch.id).where(m.ImportBatch.site_id == site_id, m.ImportBatch.status == "accepted")))
    changed = inputs_changed_since(s, site_id, run) if run is not None and period.status != "closed" else []
    stale = bool(changed)
    batches = run.input_batch_ids if run else active
    exp = s.execute(select(m.ExpenseLine).where(m.ExpenseLine.site_id == site_id, m.ExpenseLine.period_id == period_id,
                                                or_(m.ExpenseLine.import_batch_id.in_(batches or [-1]), m.ExpenseLine.import_batch_id.is_(None)))).scalars().all()
    # 1 payroll / agency reconciliation
    labor = s.execute(select(m.EmployeeLaborCost).where(m.EmployeeLaborCost.period_id == period_id,
                                                        m.EmployeeLaborCost.import_batch_id.in_(batches or [-1]))).scalars().all()
    pay_src = sum((D(w.loaded_cost) for w in labor if w.employer_type == "employee"), ZERO)
    agy_src = sum((D(w.loaded_cost) for w in labor if w.employer_type == "agency"), ZERO)
    pay_led = sum((D(x.amount) for x in exp if x.payment_recipient_type == "direct_payroll"), ZERO)
    agy_led = sum((D(x.amount) for x in exp if x.payment_recipient_type == "staffing_agency"), ZERO)
    ok1 = bool(labor) and pay_src == pay_led and agy_src == agy_led
    checks.append(Check(1, CHECK_TITLES[1], ok1,
                        "No payroll / labor-cost import for this period." if not labor else
                        f"Payroll {ms.money_text(pay_src)} vs ledger {ms.money_text(pay_led)}; agency workers {ms.money_text(agy_src)} vs agency invoices {ms.money_text(agy_led)}."))
    # 2 vendor and carrier invoices captured (and approved)
    vend = s.execute(select(func.count()).select_from(m.VendorInvoiceHeader).where(m.VendorInvoiceHeader.site_id == site_id,
                                                                                   m.VendorInvoiceHeader.period_id == period_id,
                                                                                   m.VendorInvoiceHeader.import_batch_id.in_(batches or [-1]))).scalar()
    unapproved = s.execute(select(func.count()).select_from(m.VendorInvoiceHeader).where(m.VendorInvoiceHeader.period_id == period_id,
                                                                                         m.VendorInvoiceHeader.approved.is_(False))).scalar()
    checks.append(Check(2, CHECK_TITLES[2], vend > 0 and unapproved == 0,
                        f"{vend} vendor / carrier invoice(s) captured" + (f"; {unapproved} not approved." if unapproved else ".") if vend else "No vendor or carrier invoices imported for this period."))
    # 3 customer resolution
    unresolved = s.execute(select(func.count()).select_from(m.CannotRateItem).where(m.CannotRateItem.run_id == run.id, m.CannotRateItem.reason == "no_customer")).scalar() if run else None
    checks.append(Check(3, CHECK_TITLES[3], run is not None and unresolved == 0,
                        "No run yet." if run is None else f"{unresolved:,} unresolved record(s).", can_acknowledge=bool(unresolved)))
    # 4 allocation
    if run is not None:
        c = ms.costs(s, scope, site_id, period_id)
        rec = ledger_by_recipient(exp)
        ok4 = all(r["check"] for r in c["matrix"]) and not c["exceptions"] and rec["reconciles"] and bool(c["matrix"])
        detail = ("No expense lines for this period." if not c["matrix"] else
                  f"{len(c['matrix'])} pool(s); {len(c['exceptions'])} exception(s); ledger vs payment recipients difference {ms.money_text(rec['difference'])}.")
        checks.append(Check(4, CHECK_TITLES[4], ok4, detail, [e["label"] + ": " + e["exception_label"] for e in c["exceptions"]]))
    else:
        checks.append(Check(4, CHECK_TITLES[4], False, "No run yet."))
    # 5 rating, reconciliation and review queue
    if run is not None:
        open_items = s.execute(select(func.count()).select_from(m.LeakageItem).where(m.LeakageItem.period_id == period_id,
                                                                                    m.LeakageItem.status.in_(("new", "under_review")))).scalar()
        waiting = s.execute(select(m.LeakageItem).where(m.LeakageItem.period_id == period_id, m.LeakageItem.status == "decided")).scalars().all()
        threshold = get_setting(s, actor.tenant_id, "leakage.approval_threshold", site_id)
        need_appr = [i for i in waiting if threshold is not None and abs(D(i.gap_amount)) > threshold]
        cannot = s.execute(select(func.count()).select_from(m.CannotRateItem).where(m.CannotRateItem.run_id == run.id, m.CannotRateItem.reason != "no_customer")).scalar()
        ok5 = open_items == 0 and not need_appr and cannot == 0 and not stale
        parts = [f"{open_items} item(s) New or Under review", f"{len(need_appr)} decision(s) awaiting approval", f"{cannot:,} cannot-rate item(s)"]
        if stale:
            parts.append("inputs changed since the last run (" + ", ".join(changed) + "): run again")
        checks.append(Check(5, CHECK_TITLES[5], ok5, "; ".join(parts) + "."))
    else:
        checks.append(Check(5, CHECK_TITLES[5], False, "No run yet."))
    # 6 P&L review
    low = get_setting(s, actor.tenant_id, "close.margin_range_low", site_id)
    high = get_setting(s, actor.tenant_id, "close.margin_range_high", site_id)
    if run is None:
        checks.append(Check(6, CHECK_TITLES[6], False, "No run yet."))
    elif low is None or high is None:
        checks.append(Check(6, CHECK_TITLES[6], False, "Fill the settings close.margin_range_low and close.margin_range_high."))
    else:
        pnl = ms.customers_pnl(s, scope, site_id, period_id)
        outside = [r for r in pnl["rows"] if r["margin_pct"] is None or r["margin_pct"] < low or r["margin_pct"] > high]
        manual = s.execute(select(func.count()).select_from(m.ManualBillableEntry).where(m.ManualBillableEntry.site_id == site_id,
                                                                                         m.ManualBillableEntry.status == "accepted",
                                                                                         m.ManualBillableEntry.service_date >= period.start_date,
                                                                                         m.ManualBillableEntry.service_date <= period.end_date)).scalar()
        exc = len(ms.costs(s, scope, site_id, period_id)["exceptions"])
        items = [f"{r['customer']}: margin {('Not available' if r['margin_display'] is None else str(r['margin_display']) + '%')}" for r in outside]
        if manual:
            items.append(f"{manual} accepted manual billable entr{'y' if manual == 1 else 'ies'}")
        if exc:
            items.append(f"{exc} allocation exception(s)")
        checks.append(Check(6, CHECK_TITLES[6], not items, f"Margin range {low}%–{high}%." + ("" if not items else " Needs acknowledgement."), items,
                            can_acknowledge=bool(items)))
    # 7 receivables and payables reported separately
    checks.append(Check(7, CHECK_TITLES[7], run is not None, "Receivables (customer invoices) and payables (vendor, carrier, payroll) are exported "
                        "on separate sheets of the close report; cash is out of scope." if run else "No run yet."))
    for c in checks:
        a = acks.get(c.no)
        if a is not None and c.no in ACKNOWLEDGEABLE and not c.passed:
            c.acknowledged = dict(reason=a.reason, by=a.acknowledged_by, at=a.acknowledged_at)
    return checks


def acknowledge(s, actor: Actor, period_id: int, check_no: int, reason: str):
    if check_no not in ACKNOWLEDGEABLE:
        raise BusinessError("This check cannot be acknowledged; it must pass.")
    if not (reason or "").strip():
        raise BusinessError("A reason is required.")
    p = s.get(m.BillingPeriod, period_id)
    if p.status == "closed":
        raise BusinessError("The period is closed.")
    a = m.CloseAcknowledgement(tenant_id=actor.tenant_id, period_id=period_id, check_no=check_no, reason=reason.strip(),
                               acknowledged_by=actor.user_id, close_version=p.close_version)
    s.add(a)
    s.flush()
    audit(s, actor, "close.acknowledge", "billing_period", period_id, None, {"check": check_no}, reason)


def close_period(s, actor: Actor, scope, period_id: int):
    p = s.get(m.BillingPeriod, period_id)
    if p.status == "closed":
        raise BusinessError("Already closed.")
    checks = run_checks(s, actor, scope, period_id)
    failing = [c for c in checks if not c.ok]
    if failing:
        raise BusinessError("Close is blocked by check(s): " + ", ".join(str(c.no) for c in failing))
    run = latest_run(s, period_id)
    p.status = "closed"
    p.closed_at = dt.datetime.now(dt.timezone.utc)
    p.closed_by = actor.user_id
    p.close_run_id = run.id
    run.run_type = "close"
    audit(s, actor, "period.close", "billing_period", p.id, {"status": "open"}, {"status": "closed", "run": run.id, "hash": run.result_hash,
                                                                                 "close_version": p.close_version})
    return p


def reopen_period(s, actor: Actor, period_id: int, reason: str):
    if not (reason or "").strip():
        raise BusinessError("A reason is required to reopen a period.")
    p = s.get(m.BillingPeriod, period_id)
    if p.status != "closed":
        raise BusinessError("Only a closed period can be reopened.")
    before = {"status": p.status, "close_run_id": p.close_run_id, "close_version": p.close_version}
    p.status = "open"
    p.close_version += 1
    p.close_run_id = None
    p.closed_at = None
    audit(s, actor, "period.reopen", "billing_period", p.id, before, {"status": "open", "close_version": p.close_version}, reason)
    return p
