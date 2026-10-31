"""Calculation run (workflow 8.2).

snapshot inputs -> resolve customers -> rate -> build billable lines -> compare with invoice lines ->
raise leakage items -> allocate costs -> calculate P&L -> write run record.

A run never deletes decisions: items that are found again keep their status and history; items of the
period that are no longer found are marked "Resolved by data" with the run that resolved them.
One run per site-period at a time (PostgreSQL advisory lock). A run on a closed period is only possible
in ``verify`` mode, which recomputes from the inputs recorded by the close run and compares result hashes.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
from collections import Counter, defaultdict
from decimal import Decimal

from sqlalchemy import and_, func, or_, select, text

from .. import models as m
from ..config import CODE_VERSION
from ..engine import allocation as alloc_engine
from ..engine.billable import build_billable_lines
from ..engine.leakage import InvoiceLineSpec, reconcile
from ..engine.money import ZERO, D
from ..engine.pnl import customer_pnl
from ..engine.rating import (Conversion, CostSpec, EventSpec, RateSpec, UomConverter, normalise_condition, rate_costs,
                             rate_events, rate_fixed_fees)
from ..engine.resolution import resolve
from ..ingest.copy import copy_rows
from .common import RULES, SETTINGS, Actor, BusinessError, audit, get_setting, missing_settings

log = logging.getLogger("billright.run")

EVENT_SOURCES = ("gini_ti102c", "gini_ti202", "gini_order_header", "storage_activity")


class RunBlocked(BusinessError):
    pass


# ------------------------------------------------------------------ loading inputs
def load_rates(s, site_id: int, as_of: dt.datetime | None = None) -> list[RateSpec]:
    q = (select(m.RateCardVersion, m.Contract).join(m.Contract, m.Contract.id == m.RateCardVersion.contract_id)
         .join(m.Customer, m.Customer.id == m.Contract.customer_id)
         .where(m.Customer.site_id == site_id, m.RateCardVersion.status.in_(("approved", "superseded"))))
    if as_of is not None:
        q = q.where(m.RateCardVersion.approved_at <= as_of)
    by_contract = defaultdict(list)
    for v, c in s.execute(q).all():
        by_contract[c.id].append((v, c))
    charges = {c.id: c for c in s.execute(select(m.ChargeCode)).scalars()}
    out = []
    for cid, items in by_contract.items():
        items.sort(key=lambda vc: (vc[0].approved_at, vc[0].version_no))
        for idx, (v, c) in enumerate(items):
            nxt = items[idx + 1][0].effective_from if idx + 1 < len(items) else None
            status = "approved" if idx == len(items) - 1 else "superseded"
            for r in s.execute(select(m.Rate).where(m.Rate.rate_card_version_id == v.id)).scalars():
                cc = charges[r.charge_code_id]
                out.append(RateSpec(
                    id=r.id, rate_card_version_id=v.id, version_no=v.version_no, version_status=status, version_from=v.effective_from,
                    version_to=nxt, contract_reference=c.reference, customer_id=c.customer_id, charge_code_id=cc.id, charge_code=cc.code,
                    category=cc.category, event_type=cc.event_type, activity_code=r.activity_code, pricing_method=r.pricing_method,
                    unit_price=D(r.unit_price) if r.unit_price is not None else None, uom=r.uom,
                    markup_pct=D(r.markup_pct) if r.markup_pct is not None else None,
                    eligible_cost_categories=tuple(r.eligible_cost_categories or ()),
                    minimum_charge=D(r.minimum_charge) if r.minimum_charge is not None else None, billing_period=r.billing_period,
                    tier_method=r.tier_method or "none", tiers=tuple(r.tiers or ()), condition=normalise_condition(r.condition),
                    valid_from=r.valid_from, valid_to=r.valid_to))
    return sorted(out, key=lambda r: r.id)


def build_indexes(s, batch_ids, join_paths, events=None):
    """Join-path lookups over the active snapshots. With ``events``, only the keys reachable from the period's
    events are loaded (step by step, through the partial indexes of migration 0003), so a run does not scale with
    the whole snapshot history."""
    idx = {}
    for etype, path in join_paths.items():
        values = None
        if events is not None:
            from ..engine.resolution import start_values
            values = set()
            for e in events:
                if e.event_type == etype:
                    values |= start_values(e, path[0]["from"])
        for step in path[1:]:
            if "table" not in step:
                break
            key = (step["table"], step["match"], step["take"])
            d = idx.setdefault(key, defaultdict(set))
            match_col = m.SourceSnapshot.payload[step["match"]].as_string()
            base = select(match_col, m.SourceSnapshot.payload[step["take"]].as_string()).where(
                m.SourceSnapshot.source_table == step["table"], m.SourceSnapshot.import_batch_id.in_(batch_ids))
            nxt = set()
            if values is None:
                chunks = [None]
            else:
                todo = sorted(v for v in values if v not in d)
                chunks = [todo[i:i + 20000] for i in range(0, len(todo), 20000)]
            for chunk in chunks:
                q = base if chunk is None else base.where(match_col.in_(chunk))
                for a, b in s.execute(q).all():
                    if a not in (None, "") and b not in (None, ""):
                        d[a].add(b)
            if values is not None:
                for v in values:
                    nxt |= d.get(v, set())
                values = nxt
    return idx


def load_identity(s):
    ident = defaultdict(list)
    for r in s.execute(select(m.CustomerIdentityMap)).scalars():
        ident[(r.source_key_type, r.source_key_value)].append((r.customer_id, r.valid_from, r.valid_to))
    return ident


class Inputs:
    """Everything one run reads, loaded once (the run's input snapshot)."""

    def __init__(self, s, tenant_id, site, period, as_of=None, frozen_batches=None):
        self.site, self.period = site, period
        all_batches = s.execute(select(m.ImportBatch).where(m.ImportBatch.site_id == site.id)).scalars().all()
        if frozen_batches is not None:
            self.batch_ids = sorted(b.id for b in all_batches if b.id in set(frozen_batches))
        else:
            self.batch_ids = sorted(b.id for b in all_batches if b.status == "accepted")
        self.periods = s.execute(select(m.BillingPeriod).where(m.BillingPeriod.site_id == site.id)).scalars().all()
        self.settings = {}
        for k in SETTINGS:
            v = get_setting(s, tenant_id, k, site.id)
            self.settings[k] = v
        self.join_paths = self.settings.get("customer_resolution.join_path") or {}
        self.rates = load_rates(s, site.id, as_of)
        self.charges = {c.id: c for c in s.execute(select(m.ChargeCode)).scalars()}
        self.customers = {c.id: c for c in s.execute(select(m.Customer).where(m.Customer.site_id == site.id)).scalars()}
        conv = s.execute(select(m.UomConversion).where(or_(m.UomConversion.import_batch_id.in_(self.batch_ids),
                                                           m.UomConversion.import_batch_id.is_(None)))).scalars().all()
        self.converter = UomConverter([Conversion(c.material_id, c.from_uom, c.to_uom, D(c.multiplier), c.valid_from, c.valid_to) for c in conv])
        # events of the period
        rows = s.execute(select(m.ActivityEvent.id, m.ActivityEvent.source_ref, m.ActivityEvent.event_type, m.ActivityEvent.service_date,
                                m.ActivityEvent.quantity, m.ActivityEvent.uom, m.ActivityEvent.material_id, m.ActivityEvent.attributes,
                                m.ActivityEvent.customer_id, m.ActivityEvent.order_ref, m.ActivityEvent.task_ref, m.ActivityEvent.shipment_ref)
                         .where(m.ActivityEvent.site_id == site.id, m.ActivityEvent.import_batch_id.in_(self.batch_ids),
                                m.ActivityEvent.service_date >= period.start_date, m.ActivityEvent.service_date <= period.end_date)
                         .order_by(m.ActivityEvent.id)).all()
        self.events = [_Ev(*r) for r in rows]
        mq = select(m.ManualBillableEntry).where(m.ManualBillableEntry.site_id == site.id, m.ManualBillableEntry.status == "accepted",
                                                 m.ManualBillableEntry.service_date >= period.start_date,
                                                 m.ManualBillableEntry.service_date <= period.end_date)
        if as_of is not None:
            mq = mq.where(m.ManualBillableEntry.reviewed_at <= as_of)
        self.manual = s.execute(mq.order_by(m.ManualBillableEntry.id)).scalars().all()
        self.identity = load_identity(s)
        self.indexes = build_indexes(s, self.batch_ids, self.join_paths, self.events)
        # invoices
        inv = s.execute(select(m.InvoiceLine, m.InvoiceHeader).join(m.InvoiceHeader, m.InvoiceHeader.id == m.InvoiceLine.invoice_header_id)
                        .where(m.InvoiceHeader.site_id == site.id, m.InvoiceHeader.period_id == period.id,
                               m.InvoiceHeader.import_batch_id.in_(self.batch_ids))).all()
        self.invoice_lines = [InvoiceLineSpec(l.id, h.customer_id, l.charge_code_id, l.raw_charge_code, h.period_id,
                                              D(l.quantity) if l.quantity is not None else None,
                                              D(l.unit_price) if l.unit_price is not None else None, D(l.amount), l.adjustment_ref)
                              for l, h in inv]
        # expenses (imported lines of active batches + manual lines)
        self.expenses = s.execute(select(m.ExpenseLine).where(m.ExpenseLine.site_id == site.id, m.ExpenseLine.period_id == period.id,
                                                              or_(m.ExpenseLine.import_batch_id.in_(self.batch_ids),
                                                                  m.ExpenseLine.import_batch_id.is_(None)))
                                  .order_by(m.ExpenseLine.id)).scalars().all()
        dq = select(m.AllocationDriver).where(m.AllocationDriver.site_id == site.id, m.AllocationDriver.status == "approved",
                                              m.AllocationDriver.valid_from <= period.start_date,
                                              or_(m.AllocationDriver.valid_to.is_(None), m.AllocationDriver.valid_to >= period.start_date))
        if as_of is not None:
            dq = dq.where(m.AllocationDriver.approved_at <= as_of)
        self.drivers = s.execute(dq).scalars().all()
        self.driver_qty = s.execute(select(m.AllocationDriverQuantity).where(
            m.AllocationDriverQuantity.period_id == period.id,
            m.AllocationDriverQuantity.driver_id.in_([d.id for d in self.drivers] or [-1]),
            or_(m.AllocationDriverQuantity.import_batch_id.in_(self.batch_ids), m.AllocationDriverQuantity.import_batch_id.is_(None)))).scalars().all()
        self.labor_present = s.execute(select(func.count()).select_from(m.EmployeeLaborCost).where(
            m.EmployeeLaborCost.period_id == period.id, m.EmployeeLaborCost.import_batch_id.in_(self.batch_ids))).scalar() > 0
        co = s.execute(select(m.CentralOverhead).where(m.CentralOverhead.site_id == site.id, m.CentralOverhead.period_id == period.id)
                       .order_by(m.CentralOverhead.id.desc())).scalars().first()
        self.central_overhead = D(co.amount) if co else None
        sq = select(m.SurchargeRule, m.ChargeCode).join(m.ChargeCode, m.ChargeCode.id == m.SurchargeRule.surcharge_charge_code_id).where(
            m.SurchargeRule.site_id == site.id, m.SurchargeRule.status == "approved")
        self.surcharge_rules = [dict(kind=r.kind, parameters=r.parameters, event_type=r.applies_to_event_type, charge=c.code)
                                for r, c in s.execute(sq).all()]


class _Ev:
    __slots__ = ("id", "source_ref", "event_type", "service_date", "quantity", "uom", "material_id", "attributes", "customer_id",
                 "order_ref", "task_ref", "shipment_ref")

    def __init__(self, *vals):
        for k, v in zip(self.__slots__, vals):
            setattr(self, k, v)


# ------------------------------------------------------------------ compute
class Results:
    pass


def compute(inp: Inputs, progress=lambda msg: None) -> Results:
    R = Results()
    period = inp.period
    progress("Resolving customers")
    resolved, unresolved = resolve(inp.events, inp.join_paths, inp.indexes, inp.identity)
    events = []
    for i, e in enumerate(inp.events):
        cust = e.customer_id if e.customer_id is not None else resolved.get(i)
        if cust is not None and cust not in inp.customers:
            cust = None
        events.append(EventSpec(e.id, e.source_ref, e.event_type, e.service_date, D(e.quantity) if e.quantity is not None else None,
                                e.uom, e.material_id, e.attributes or {}, cust))
    if inp.surcharge_rules:  # G4: approved surcharge rules flag qualifying events
        from ..ga.features import apply_surcharge_rules
        events = apply_surcharge_rules(inp.surcharge_rules, events)
    unresolved_detail = {inp.events[u.index].id: u for u in unresolved}
    for me in inp.manual:
        cc = inp.charges[me.charge_code_id]
        events.append(EventSpec(None, f"MANUAL:{me.id}", cc.event_type or "manual", me.service_date, D(me.quantity), me.uom, None,
                                {"note": me.note, "work_order_ref": me.work_order_ref}, me.customer_id, me.id, me.charge_code_id))
    progress(f"Rating {len(events):,} events")

    def period_of(day):
        return period.id if period.start_date <= day <= period.end_date else None

    rates = [r for r in inp.rates if r.billing_period == period.period_type]
    rated, cannot = rate_events(events, rates, inp.converter, period_of)
    for c in cannot:
        if c.reason == "no_customer" and c.event is not None and c.event.id in unresolved_detail:
            u = unresolved_detail[c.event.id]
            c.details.update({"resolution": u.reason, "broken_at": u.step, "candidates": u.values})
    costs = [CostSpec(x.id, x.customer_id, x.expense_category, D(x.amount), period.id, x.description)
             for x in inp.expenses if x.customer_id is not None]
    r2, c2 = rate_costs(costs, rates, {period.id: period.start_date})
    rated += r2
    cannot += c2
    active_customers = sorted({r.customer_id for r in rates if r.pricing_method == "fixed_fee"})
    rated += rate_fixed_fees(rates, [(c, period.id, period.start_date, period.end_date, period.period_type) for c in active_customers])
    R.rated, R.cannot, R.events = rated, cannot, events
    progress("Building billable lines")
    R.billable = build_billable_lines(rated)
    progress("Reconciling with invoices")
    tol = {r: (inp.settings[f"tolerance.{r}.amount"], inp.settings[f"tolerance.{r}.pct"]) for r in RULES}
    history = defaultdict(list)
    for r in inp.rates:
        history[(r.customer_id, r.charge_code_id)].append(r)
    cannot_keys = {(c.customer_id, c.charge_code_id, period.id) for c in cannot if c.customer_id and c.charge_code_id}
    categories = {cid: c.category for cid, c in inp.charges.items()}
    quantity_based = {k for k, b in R.billable.items() if b.governing_rate.pricing_method in ("per_unit", "hourly")}
    R.recon = reconcile(R.billable, inp.invoice_lines, categories, history, cannot_keys, tol)
    for row in R.recon:  # L2/L4 compare units: only meaningful for unit-priced charges
        key = (row.customer_id, row.charge_code_id, row.period_id)
        if key in R.billable and key not in quantity_based and row.finding and row.finding.rule_code in ("L2", "L4"):
            row.finding = None
            row.status = "reconciled" if row.gap_amount == 0 else row.status
        if key in R.billable and key not in quantity_based:
            row.rules_matched = [x for x in row.rules_matched if x not in ("L2", "L4")]
    progress("Allocating costs")
    expense_specs = [alloc_engine.ExpenseSpec(x.id, x.expense_category, D(x.amount), x.customer_id, x.payment_recipient_type, x.description)
                     for x in inp.expenses]
    drivers = defaultdict(list)
    for d in inp.drivers:
        drivers[d.expense_category].append(alloc_engine.DriverSpec(d.id, d.expense_category, d.driver_type, d.is_proxy, d.description))
    qty = defaultdict(lambda: defaultdict(lambda: ZERO))
    for q in inp.driver_qty:
        qty[q.driver_id][q.customer_id] += D(q.quantity)
    R.pools = alloc_engine.allocate(expense_specs, drivers, qty)
    progress("Calculating customer P&L")
    revenue = defaultdict(lambda: ZERO)
    for l in inp.invoice_lines:
        revenue[l.customer_id] += l.amount
    orders = Counter()
    seen_orders = set()
    for e in events:
        if e.event_type == "order_completed" and e.customer_id is not None and e.source_ref not in seen_orders:
            seen_orders.add(e.source_ref)
            orders[e.customer_id] += 1
    R.orders = orders
    R.revenue = revenue
    R.unresolved = [e for e in events if e.customer_id is None and e.manual_entry_id is None]
    return R


def finish_pnl(R, inp: Inputs, confirmed: dict):
    customer_ids = [c for c in inp.customers]
    has_orders = bool(R.orders) or any(e.event_type == "order_completed" for e in R.events)
    orders = {c: R.orders.get(c, 0) for c in customer_ids} if has_orders else {}
    R.pnl_rows, R.site = customer_pnl(customer_ids, R.revenue, R.pools, orders, confirmed, inp.central_overhead)
    # customers without revenue, cost or activity are left out of the P&L table
    R.pnl_rows = [r for r in R.pnl_rows if r.revenue_billed or r.total_cost or (r.orders_completed or 0) or confirmed.get(r.customer_id)]


def result_hash(R) -> str:
    """Hash of all result tables (identity of results for re-runs of a closed period)."""
    def s(v):
        return str(v) if v is not None else None
    doc = {
        "rated": sorted([s(r.rate.customer_id), s(r.rate.charge_code_id), s(r.rate.id), r.event.source_ref if r.event else f"exp:{r.expense_line_id}",
                         s(r.quantity), s(r.unit_price), s(r.amount), r.service_date.isoformat()] for r in R.rated),
        "cannot": sorted([c.source_ref, c.reason, s(c.customer_id), s(c.charge_code_id)] for c in R.cannot),
        "billable": sorted([*map(s, k), s(b.quantity), s(b.amount), b.minimum_applied] for k, b in R.billable.items()),
        "recon": sorted([s(r.customer_id), s(r.charge_code_id), s(r.billable_amount), s(r.billed_amount), s(r.gap_amount), r.status,
                         r.rules_matched, r.finding.rule_code if r.finding else None] for r in R.recon),
        "allocations": sorted([p.category, s(row.customer_id), s(row.amount), row.method, s(row.driver_id), s(row.share), row.exception]
                              for p in R.pools.values() for row in p.rows),
        "pnl": sorted([s(r.customer_id), s(r.revenue_billed), s(r.direct_cost), s(r.allocated_shared_cost), s(r.site_profit), s(r.margin_pct),
                       s(r.orders_completed)] for r in R.pnl_rows),
        "site": [s(R.site.revenue), s(R.site.total_cost), s(R.site.unassigned_cost), s(R.site.site_profit), s(R.site.margin_pct),
                 s(R.site.profit_before_financing_tax)],
    }
    return hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()


# ------------------------------------------------------------------ entry point
def check_blockers(inp: Inputs) -> list[str]:
    missing = [k for k, v in SETTINGS.items() if v["required"] == "run" and inp.settings.get(k) is None]
    if inp.labor_present and inp.settings.get("labor.cost_basis") is None:
        missing.append("labor.cost_basis")
    return missing


def start_run(s, actor: Actor, period_id: int, run_type: str = "on_demand") -> m.CalculationRun:
    period = s.get(m.BillingPeriod, period_id)
    if period is None:
        raise BusinessError("Period not found")
    if period.status == "closed" and run_type != "verify":
        raise BusinessError(f"Period {period.label} is closed: its results are frozen. Use 'Verify' to re-run it read-only.")
    running = s.execute(select(m.CalculationRun).where(m.CalculationRun.period_id == period_id, m.CalculationRun.status == "running",
                                                       m.CalculationRun.started_at > func.now() - text("interval '2 hours'"))).scalars().first()
    if running:
        raise BusinessError(f"Run #{running.id} for {period.label} is still running.")
    run = m.CalculationRun(tenant_id=actor.tenant_id, site_id=period.site_id, period_id=period_id, run_type=run_type, status="running",
                           code_version=CODE_VERSION, triggered_by=actor.user_id, progress="Queued")
    s.add(run)
    s.flush()
    return run


def execute_run(s, actor: Actor, run_id: int, progress_cb=None) -> m.CalculationRun:
    """Do the work for a run row created by ``start_run`` (same or separate transaction)."""
    t0 = time.monotonic()
    run = s.get(m.CalculationRun, run_id)
    period = s.get(m.BillingPeriod, run.period_id)
    site = s.get(m.Site, run.site_id)
    got = s.execute(text("select pg_try_advisory_xact_lock(:k)"), {"k": 7_000_000 + period.id}).scalar()
    if not got:
        raise BusinessError("Another run for this site-period is in progress.")

    def progress(msg):
        run.progress = msg
        if progress_cb:
            progress_cb(msg)
        log.info("run %s: %s", run_id, msg)

    verify = run.run_type == "verify"
    as_of = frozen = close_run = None
    if verify:
        close_run = s.get(m.CalculationRun, period.close_run_id) if period.close_run_id else None
        if close_run is None:
            raise BusinessError("This period has no close run to verify against.")
        as_of, frozen = close_run.started_at, close_run.input_batch_ids
    progress("Snapshotting inputs")
    inp = Inputs(s, actor.tenant_id, site, period, as_of=as_of, frozen_batches=frozen)
    if verify:
        inp.settings = {**inp.settings, **{k: _parse_snap(k, v) for k, v in (close_run.settings_snapshot or {}).items()}}
        inp.join_paths = inp.settings.get("customer_resolution.join_path") or {}
        inp.indexes = build_indexes(s, inp.batch_ids, inp.join_paths, inp.events)
    run.input_batch_ids = inp.batch_ids
    run.settings_snapshot = {k: (json.dumps(v) if isinstance(v, (dict, list)) else (None if v is None else str(v))) for k, v in inp.settings.items()}
    blockers = check_blockers(inp)
    if blockers:
        run.status = "blocked"
        run.message = "Fill these settings first: " + ", ".join(blockers)
        run.finished_at = dt.datetime.now(dt.timezone.utc)
        return run
    R = compute(inp, progress)
    confirmed = confirmed_not_invoiced(s, period.id)
    finish_pnl(R, inp, confirmed)
    run.result_hash = result_hash(R)
    run.counts = {"events": len(R.events), "rated": len(R.rated), "cannot_rate": len(R.cannot), "billable_lines": len(R.billable),
                  "findings": sum(1 for r in R.recon if r.finding), "unresolved": len(R.unresolved), "seconds": round(time.monotonic() - t0, 1)}
    if verify:
        run.status = "succeeded"
        same = run.result_hash == close_run.result_hash
        run.message = ("Identical to the close run" if same else "DIFFERENT from the close run") + f" (#{close_run.id})"
        run.counts["matches_close_run"] = same
        run.finished_at = dt.datetime.now(dt.timezone.utc)
        return run
    progress("Writing results")
    persist(s, actor, run, period, R, inp)
    run.status = "succeeded"
    run.finished_at = dt.datetime.now(dt.timezone.utc)
    run.counts["seconds"] = round(time.monotonic() - t0, 1)
    run.progress = "Done"
    log.info("run %s done: %s", run.id, run.counts)
    audit(s, actor, "run.finish", "calculation_run", run.id, None, {"period": period.label, "counts": run.counts, "hash": run.result_hash})
    return run


def _parse_snap(k, v):
    if v is None:
        return None
    from .common import parse_setting
    return parse_setting(SETTINGS.get(k, {}).get("type", "str"), v)


def run_period(s, actor: Actor, period_id: int, run_type: str = "on_demand") -> m.CalculationRun:
    run = start_run(s, actor, period_id, run_type)
    return execute_run(s, actor, run.id)


def confirmed_not_invoiced(s, period_id: int) -> dict:
    rows = s.execute(select(m.LeakageItem).where(m.LeakageItem.period_id == period_id, m.LeakageItem.direction == "under",
                                                 m.LeakageItem.decision.in_(("bill_now", "bill_next_cycle")),
                                                 m.LeakageItem.status.in_(("decided", "approved")))).scalars().all()
    out = defaultdict(lambda: ZERO)
    for it in rows:
        out[it.customer_id] += max(D(it.gap_amount) - D(it.invoiced_amount or 0), ZERO)
    return dict(out)


def _ids(s, seq: str, n: int) -> list[int]:
    if n == 0:
        return []
    return list(s.execute(text(f"select nextval('{seq}') from generate_series(1, :n)"), {"n": n}).scalars())


def persist(s, actor: Actor, run, period, R, inp: Inputs):
    t = actor.tenant_id
    rid = run.id
    ids = _ids(s, "rated_event_id_seq", len(R.rated))
    rated_rows = []
    rated_id_of = {}
    for new_id, r in zip(ids, R.rated):
        rated_id_of[id(r)] = new_id
        ev = r.event
        rated_rows.append((new_id, t, rid, ev.id if ev and ev.manual_entry_id is None else None, ev.manual_entry_id if ev else None,
                           r.expense_line_id, r.rate.customer_id, r.rate.charge_code_id, r.rate.id, r.rate.rate_card_version_id,
                           r.quantity, r.unit_price, r.amount, r.service_date, r.period_id))
    copy_rows(s, "rated_event", ("id", "tenant_id", "run_id", "activity_event_id", "manual_entry_id", "expense_line_id", "customer_id",
                                 "charge_code_id", "rate_id", "rate_card_version_id", "quantity_in_rate_uom", "unit_price", "amount_unrounded",
                                 "service_date", "period_id"), rated_rows)
    copy_rows(s, "cannot_rate_item", ("tenant_id", "run_id", "activity_event_id", "source_ref", "reason", "details", "customer_id",
                                      "charge_code_id", "estimated_quantity"),
              [(t, rid, c.event.id if c.event else None, c.source_ref, c.reason, json.dumps(c.details, default=str), c.customer_id,
                c.charge_code_id, c.estimated_quantity) for c in R.cannot], json_cols={5})
    copy_rows(s, "billable_line", ("tenant_id", "run_id", "customer_id", "charge_code_id", "period_id", "billable_quantity", "billable_amount",
                                   "unrounded_amount", "minimum_applied", "tier_applied", "unit_prices", "rate_ids"),
              [(t, rid, b.customer_id, b.charge_code_id, b.period_id, b.quantity, b.amount, b.unrounded, b.minimum_applied, b.tier_applied,
                json.dumps(b.unit_prices), json.dumps(b.rate_ids)) for b in R.billable.values()])
    copy_rows(s, "recon_line", ("tenant_id", "run_id", "customer_id", "charge_code_id", "period_id", "billable_quantity", "billed_quantity",
                                "billable_amount", "billed_amount", "gap_amount", "status", "rules_matched", "note"),
              [(t, rid, r.customer_id, r.charge_code_id, r.period_id, r.billable_quantity, r.billed_quantity, r.billable_amount, r.billed_amount,
                r.gap_amount, r.status, json.dumps(r.rules_matched), r.note) for r in R.recon])
    # leakage items: keep decisions and history
    existing = {(i.customer_id, i.charge_code_id, i.direction): i for i in s.execute(
        select(m.LeakageItem).where(m.LeakageItem.period_id == period.id)).scalars()}
    seen = set()
    for row in R.recon:
        f = row.finding
        if f is None:
            continue
        key = (f.customer_id, f.charge_code_id, f.direction)
        seen.add(key)
        item = existing.get(key)
        values = dict(rule_code=f.rule_code, also_matches=f.also_matches, billable_quantity=f.billable_quantity, billed_quantity=f.billed_quantity,
                      billable_amount=f.billable_amount, billed_amount=f.billed_amount, gap_amount=f.gap_amount,
                      contract_unit_price=f.contract_unit_price, billed_unit_prices=f.billed_unit_prices, run_id=rid)
        if item is None:
            item = m.LeakageItem(tenant_id=t, first_run_id=rid, site_id=period.site_id, customer_id=f.customer_id,
                                 charge_code_id=f.charge_code_id, period_id=period.id, direction=f.direction, status="new", **values)
            s.add(item)
            s.flush()
            audit(s, actor, "leakage.raise", "leakage_item", item.id, None,
                  {"rule": f.rule_code, "gap": f.gap_amount, "run": rid})
        else:
            before = {"rule_code": item.rule_code, "gap_amount": item.gap_amount, "status": item.status}
            for k, v in values.items():
                setattr(item, k, v)
            if item.status == "resolved_by_data":
                item.status = "new" if item.decision is None else ("approved" if item.approved_by else "decided")
                item.resolved_by_run_id = None
            if before != {"rule_code": item.rule_code, "gap_amount": D(item.gap_amount), "status": item.status}:
                audit(s, actor, "leakage.update_by_run", "leakage_item", item.id, before,
                      {"rule_code": item.rule_code, "gap_amount": item.gap_amount, "status": item.status, "run": rid})
        ev_rows = [(t, item.id, rid, rated_id_of.get(id(r)), None, r.event.id if (r.event and r.event.manual_entry_id is None) else None)
                   for r in f.rated]
        ev_rows += [(t, item.id, rid, None, lid, None) for lid in f.invoice_line_ids]
        copy_rows(s, "leakage_evidence", ("tenant_id", "leakage_item_id", "run_id", "rated_event_id", "invoice_line_id", "activity_event_id"), ev_rows)
    for key, item in existing.items():
        if key not in seen and item.status != "resolved_by_data":
            audit(s, actor, "leakage.resolved_by_data", "leakage_item", item.id, {"status": item.status}, {"status": "resolved_by_data", "run": rid})
            item.status = "resolved_by_data"
            item.resolved_by_run_id = rid
    # cost allocation
    alloc_rows = []
    for pool in R.pools.values():
        for a in pool.rows:
            alloc_rows.append((t, rid, period.id, a.category, a.expense_line_id, a.customer_id, a.amount, a.method, a.driver_id, a.share, a.exception))
    copy_rows(s, "cost_allocation", ("tenant_id", "run_id", "period_id", "expense_category", "expense_line_id", "customer_id", "amount", "method",
                                     "driver_id", "share", "exception"), alloc_rows)
    for p in R.pnl_rows:
        s.add(m.CustomerProfitability(tenant_id=t, run_id=rid, period_id=period.id, customer_id=p.customer_id, revenue_billed=p.revenue_billed,
                                      direct_cost=p.direct_cost, allocated_shared_cost=p.allocated_shared_cost, total_cost=p.total_cost,
                                      site_profit=p.site_profit, margin_pct=p.margin_pct, orders_completed=p.orders_completed,
                                      cost_per_order=p.cost_per_order, confirmed_not_invoiced=p.confirmed_not_invoiced))
    st = R.site
    s.add(m.SiteSummary(tenant_id=t, run_id=rid, period_id=period.id, revenue=st.revenue, total_cost=st.total_cost, allocated_cost=st.allocated_cost,
                        unassigned_cost=st.unassigned_cost, site_profit=st.site_profit, margin_pct=st.margin_pct,
                        central_overhead_unallocated=st.central_overhead_unallocated, profit_before_financing_tax=st.profit_before_financing_tax))
    update_invoiced(s, period.site_id, inp.batch_ids)
    s.flush()


def update_invoiced(s, site_id: int, batch_ids):
    """Leakage invoiced (recovered): confirmed items whose adjustment appears on a later imported invoice line (ADJUSTMENT_REF = LKG-<id>)."""
    rows = s.execute(select(m.InvoiceLine.adjustment_ref, func.sum(m.InvoiceLine.amount), func.min(m.InvoiceLine.posted_on))
                     .join(m.InvoiceHeader, m.InvoiceHeader.id == m.InvoiceLine.invoice_header_id)
                     .where(m.InvoiceHeader.site_id == site_id, m.InvoiceHeader.import_batch_id.in_(batch_ids),
                            m.InvoiceLine.adjustment_ref.is_not(None)).group_by(m.InvoiceLine.adjustment_ref)).all()
    for ref, amount, posted in rows:
        if not ref or not ref.upper().startswith("LKG-"):
            continue
        try:
            item = s.get(m.LeakageItem, int(ref.split("-", 1)[1]))
        except ValueError:
            continue
        if item is not None and item.site_id == site_id:
            item.invoiced_amount = abs(D(amount))
            item.invoiced_on = posted


def inputs_changed_since(s, site_id: int, run) -> list[str]:
    """Inputs that changed after a run started (the run must be repeated before close)."""
    t = run.started_at
    out = []
    active = sorted(b for (b,) in s.execute(select(m.ImportBatch.id).where(m.ImportBatch.site_id == site_id, m.ImportBatch.status == "accepted")))
    if sorted(run.input_batch_ids or []) != active:
        out.append("imported data")
    q = [("rate cards", select(func.max(m.RateCardVersion.approved_at))),
         ("manual entries", select(func.max(m.ManualBillableEntry.reviewed_at)).where(m.ManualBillableEntry.site_id == site_id)),
         ("allocation drivers", select(func.max(m.AllocationDriver.approved_at)).where(m.AllocationDriver.site_id == site_id)),
         ("settings", select(func.max(m.Setting.updated_at))),
         ("central overhead", select(func.max(m.CentralOverhead.entered_at)).where(m.CentralOverhead.site_id == site_id))]
    for label, qq in q:
        v = s.execute(qq).scalar()
        if v is not None and v > t:
            out.append(label)
    v = s.execute(select(func.max(m.AuditLog.at)).where(m.AuditLog.action == "expense.create")).scalar()
    if v is not None and v > t:
        out.append("manual expense lines")
    return out
