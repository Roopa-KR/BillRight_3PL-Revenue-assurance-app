"""Customer and contract register (F1) and rate-card editor with approval (F2, workflow 8.1)."""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import func, select

from .. import models as m
from ..engine.allocation import EXPENSE_CATEGORIES
from ..engine.rating import PRICING_METHODS
from .common import Actor, BusinessError, audit, row_dict

RATE_FIELDS = ("charge_code_id", "activity_code", "pricing_method", "unit_price", "uom", "markup_pct", "eligible_cost_categories",
               "minimum_charge", "billing_period", "tier_method", "tiers", "condition", "valid_from", "valid_to", "currency_code")


def create_contract(s, actor: Actor, customer_id: int, reference: str, start_date: dt.date, end_date=None, document_ref=None):
    c = m.Contract(tenant_id=actor.tenant_id, customer_id=customer_id, reference=reference, start_date=start_date, end_date=end_date,
                   status="active", document_ref=document_ref)
    s.add(c)
    s.flush()
    audit(s, actor, "contract.create", "contract", c.id, None, row_dict(c))
    return c


def update_contract(s, actor: Actor, contract_id: int, reason: str, **changes):
    c = s.get(m.Contract, contract_id)
    before = row_dict(c)
    for k, v in changes.items():
        if k in ("reference", "start_date", "end_date", "status", "document_ref"):
            setattr(c, k, v)
    c.version += 1
    audit(s, actor, "contract.update", "contract", c.id, before, row_dict(c), reason)
    return c


def new_draft(s, actor: Actor, contract_id: int, change_reason: str, copy_from_current: bool = True):
    if not change_reason:
        raise BusinessError("A change reason is required for a new rate-card version.")
    n = s.execute(select(func.coalesce(func.max(m.RateCardVersion.version_no), 0)).where(m.RateCardVersion.contract_id == contract_id)).scalar()
    v = m.RateCardVersion(tenant_id=actor.tenant_id, contract_id=contract_id, version_no=n + 1, status="draft", created_by=actor.user_id,
                          change_reason=change_reason)
    s.add(v)
    s.flush()
    if copy_from_current:
        cur = current_version(s, contract_id)
        if cur:
            for r in s.execute(select(m.Rate).where(m.Rate.rate_card_version_id == cur.id)).scalars():
                s.add(m.Rate(tenant_id=actor.tenant_id, rate_card_version_id=v.id, **{f: getattr(r, f) for f in RATE_FIELDS}, source_ref=r.source_ref))
    audit(s, actor, "rate_version.create", "rate_card_version", v.id, None, row_dict(v), change_reason)
    return v


def current_version(s, contract_id: int):
    return s.execute(select(m.RateCardVersion).where(m.RateCardVersion.contract_id == contract_id, m.RateCardVersion.status == "approved")
                     .order_by(m.RateCardVersion.version_no.desc())).scalars().first()


def validate_rate(s, data: dict, siblings=()) -> list[str]:
    """Plain-language validation messages for one rate (empty list = valid)."""
    errs = []
    pm = data.get("pricing_method")
    if pm not in PRICING_METHODS:
        errs.append("Choose a pricing method: per unit, hourly, fixed fee, cost plus or pass-through.")
    if pm in ("per_unit", "hourly") and (data.get("unit_price") is None or not data.get("uom")):
        errs.append("Per-unit and hourly rates need a unit price and a unit of measure.")
    if pm == "fixed_fee" and data.get("unit_price") is None:
        errs.append("A fixed-fee rate needs the fee amount (unit price).")
    if pm == "cost_plus" and (data.get("markup_pct") is None or not data.get("eligible_cost_categories")):
        errs.append("A cost-plus rate needs a markup % and the eligible cost categories.")
    if pm == "pass_through" and not data.get("eligible_cost_categories"):
        errs.append("A pass-through rate needs the eligible cost categories.")
    for cat in data.get("eligible_cost_categories") or []:
        if cat not in EXPENSE_CATEGORIES:
            errs.append(f"'{cat}' is not an expense category.")
    if data.get("billing_period") not in ("week", "month"):
        errs.append("Billing period must be week or month.")
    tiers = data.get("tiers")
    if tiers and data.get("tier_method") not in ("all_units", "incremental"):
        errs.append("Tiers are defined: choose how they apply (all units at the reached tier, or incremental).")
    if tiers:
        for t in tiers:
            if "from" not in t or "unit_price" not in t:
                errs.append("Each tier needs 'from' and 'unit_price'.")
                break
    if data.get("tier_method") not in (None, "none", "all_units", "incremental"):
        errs.append("Tier method must be none, all_units or incremental.")
    vf, vt = data.get("valid_from"), data.get("valid_to")
    if vf is None:
        errs.append("Valid-from date is required.")
    if vf and vt and vt < vf:
        errs.append("Valid-to must be on or after valid-from.")
    for x in ("unit_price", "minimum_charge", "markup_pct"):
        if data.get(x) is not None and Decimal(data[x]) < 0:
            errs.append(f"{x.replace('_', ' ').capitalize()} must not be negative.")
    cond = json.dumps(data.get("condition") or {}, sort_keys=True)
    for o in siblings:
        if o.get("id") == data.get("id"):
            continue
        if o["charge_code_id"] == data.get("charge_code_id") and json.dumps(o.get("condition") or {}, sort_keys=True) == cond \
                and (o.get("activity_code") or None) == (data.get("activity_code") or None):
            o_to, d_to = o.get("valid_to") or dt.date.max, vt or dt.date.max
            if vf and o["valid_from"] <= d_to and vf <= o_to:
                errs.append("This rate's validity overlaps another rate for the same charge code and condition.")
                break
    return errs


def _siblings(s, version_id):
    return [dict(id=r.id, charge_code_id=r.charge_code_id, condition=r.condition, activity_code=r.activity_code, valid_from=r.valid_from,
                 valid_to=r.valid_to) for r in s.execute(select(m.Rate).where(m.Rate.rate_card_version_id == version_id)).scalars()]


def save_rate(s, actor: Actor, version_id: int, data: dict, rate_id: int | None = None):
    v = s.get(m.RateCardVersion, version_id)
    if v is None or v.status != "draft":
        raise BusinessError("Only draft versions can be edited. Create a new version to change approved rates.")
    data = dict(data, id=rate_id)
    errs = validate_rate(s, data, _siblings(s, version_id))
    if errs:
        raise BusinessError(" ".join(errs))
    fields = {f: data.get(f) for f in RATE_FIELDS}
    fields["tier_method"] = fields.get("tier_method") or "none"
    if rate_id:
        r = s.get(m.Rate, rate_id)
        before = row_dict(r)
        for k, val in fields.items():
            setattr(r, k, val)
        audit(s, actor, "rate.update", "rate", r.id, before, row_dict(r))
    else:
        r = m.Rate(tenant_id=actor.tenant_id, rate_card_version_id=version_id, **fields)
        s.add(r)
        s.flush()
        audit(s, actor, "rate.create", "rate", r.id, None, row_dict(r))
    return r


def delete_rate(s, actor: Actor, rate_id: int):
    r = s.get(m.Rate, rate_id)
    v = s.get(m.RateCardVersion, r.rate_card_version_id)
    if v.status != "draft":
        raise BusinessError("Only rates of a draft version can be removed.")
    audit(s, actor, "rate.delete", "rate", r.id, row_dict(r), None)
    s.delete(r)


def submit(s, actor: Actor, version_id: int, effective_from: dt.date | None = None):
    v = s.get(m.RateCardVersion, version_id)
    if v.status != "draft":
        raise BusinessError("Only a draft can be submitted.")
    rates = s.execute(select(m.Rate).where(m.Rate.rate_card_version_id == v.id)).scalars().all()
    if not rates:
        raise BusinessError("The version has no rates.")
    v.effective_from = effective_from or min(r.valid_from for r in rates)
    v.status = "pending_approval"
    v.submitted_at = dt.datetime.now(dt.timezone.utc)
    audit(s, actor, "rate_version.submit", "rate_card_version", v.id, {"status": "draft"}, {"status": v.status, "effective_from": v.effective_from})
    return v


def approve(s, actor: Actor, version_id: int):
    v = s.get(m.RateCardVersion, version_id)
    if v.status != "pending_approval":
        raise BusinessError("Only a submitted version can be approved.")
    if v.created_by == actor.user_id:
        raise BusinessError("Segregation of duties: the author of a rate version cannot approve it.")
    contract = s.get(m.Contract, v.contract_id)
    customer = s.get(m.Customer, contract.customer_id)
    closed = s.execute(select(func.max(m.BillingPeriod.end_date)).where(m.BillingPeriod.site_id == customer.site_id,
                                                                       m.BillingPeriod.status == "closed")).scalar()
    if closed and v.effective_from <= closed:
        raise BusinessError(f"Effective date {v.effective_from} falls in a closed period (closed through {closed}). "
                            "Approved rates apply to open periods only.")
    prev = current_version(s, v.contract_id)
    now = dt.datetime.now(dt.timezone.utc)
    if prev is not None:
        prev.status = "superseded"
        prev.superseded_from = v.effective_from
        audit(s, actor, "rate_version.supersede", "rate_card_version", prev.id, {"status": "approved"},
              {"status": "superseded", "superseded_from": v.effective_from})
    v.status = "approved"
    v.approved_by = actor.user_id
    v.approved_at = now
    audit(s, actor, "rate_version.approve", "rate_card_version", v.id, {"status": "pending_approval"}, {"status": "approved"})
    return v


def reject(s, actor: Actor, version_id: int, reason: str):
    if not reason:
        raise BusinessError("A reason is required to reject a rate version.")
    v = s.get(m.RateCardVersion, version_id)
    if v.status != "pending_approval":
        raise BusinessError("Only a submitted version can be rejected.")
    if v.created_by == actor.user_id:
        raise BusinessError("Segregation of duties: the author of a rate version cannot decide on it.")
    v.status = "rejected"
    v.rejection_reason = reason
    audit(s, actor, "rate_version.reject", "rate_card_version", v.id, {"status": "pending_approval"}, {"status": "rejected"}, reason)
    return v


def diff(s, a_id: int, b_id: int):
    """Side-by-side difference of two versions keyed by charge code + activity + condition + valid_from."""
    def load(vid):
        out = {}
        for r in s.execute(select(m.Rate).where(m.Rate.rate_card_version_id == vid)).scalars():
            key = (r.charge_code_id, r.activity_code or "", json.dumps(r.condition or {}, sort_keys=True))
            out.setdefault(key, []).append(r)
        return out
    a, b = load(a_id) if a_id else {}, load(b_id)
    charges = {c.id: c.code for c in s.execute(select(m.ChargeCode)).scalars()}
    rows = []
    for key in sorted(set(a) | set(b), key=lambda k: (charges.get(k[0], ""), k[1], k[2])):
        ra = a.get(key, [None])[-1]
        rb = b.get(key, [None])[-1]
        changed = []
        for f in ("pricing_method", "unit_price", "uom", "markup_pct", "minimum_charge", "tier_method", "tiers", "valid_from", "valid_to"):
            va, vb = (getattr(ra, f) if ra else None), (getattr(rb, f) if rb else None)
            if va != vb:
                changed.append(f)
        status = "added" if ra is None else "removed" if rb is None else ("changed" if changed else "same")
        rows.append(dict(charge=charges.get(key[0]), activity=key[1], condition=key[2], a=ra, b=rb, changed=changed, status=status))
    return rows


def repricing_impact(s, version_id: int) -> int:
    """Number of open-period events of the customer, from the effective date on, that the change re-prices
    (counted from the latest successful run of each open period)."""
    v = s.get(m.RateCardVersion, version_id)
    contract = s.get(m.Contract, v.contract_id)
    customer = s.get(m.Customer, contract.customer_id)
    eff = v.effective_from or s.execute(select(func.min(m.Rate.valid_from)).where(m.Rate.rate_card_version_id == v.id)).scalar()
    charge_ids = list(s.execute(select(m.Rate.charge_code_id).where(m.Rate.rate_card_version_id == v.id)).scalars())
    total = 0
    for p in s.execute(select(m.BillingPeriod).where(m.BillingPeriod.site_id == customer.site_id, m.BillingPeriod.status != "closed")).scalars():
        run = latest_run(s, p.id)
        if run is None:
            continue
        total += s.execute(select(func.count()).select_from(m.RatedEvent).where(
            m.RatedEvent.run_id == run.id, m.RatedEvent.customer_id == customer.id, m.RatedEvent.charge_code_id.in_(charge_ids),
            m.RatedEvent.service_date >= eff)).scalar()
    return total


def latest_run(s, period_id: int):
    return s.execute(select(m.CalculationRun).where(m.CalculationRun.period_id == period_id, m.CalculationRun.status == "succeeded",
                                                    m.CalculationRun.run_type != "verify")
                     .order_by(m.CalculationRun.id.desc())).scalars().first()


def draft_from_rows(s, actor: Actor, site, accepted_rows):
    """Load rate-card CSV rows into one draft version per (customer, contract reference)."""
    charges = {c.code: c for c in s.execute(select(m.ChargeCode)).scalars()}
    customers = {c.code: c for c in s.execute(select(m.Customer).where(m.Customer.site_id == site.id)).scalars()}
    groups = defaultdict(list)
    for item in accepted_rows:
        p = item[2]
        groups[(p["CUSTOMER_CODE"], p["CONTRACT_REFERENCE"])].append(item)
    late, versions = [], []
    for (ccode, ref), items in sorted(groups.items()):
        cust = customers[ccode]
        contract = s.execute(select(m.Contract).where(m.Contract.customer_id == cust.id, m.Contract.reference == ref)).scalar_one_or_none()
        if contract is None:
            contract = create_contract(s, actor, cust.id, ref, min(i[2]["VALID_FROM"] for i in items))
        v = new_draft(s, actor, contract.id, "Loaded from rate-card file", copy_from_current=False)
        versions.append(v.id)
        for i, raw, p in items:
            cats = p.get("ELIGIBLE_COST_CATEGORIES")
            data = dict(charge_code_id=charges[p["CHARGE_CODE"]].id, activity_code=p.get("ACTIVITY_CODE"), pricing_method=p["PRICING_METHOD"],
                        unit_price=p.get("UNIT_PRICE"), uom=p.get("UOM"), markup_pct=p.get("MARKUP_PCT"),
                        eligible_cost_categories=[c.strip() for c in cats.split(";")] if cats else None,
                        minimum_charge=p.get("MINIMUM_CHARGE"), billing_period=p["BILLING_PERIOD"], tier_method=p.get("TIER_METHOD") or "none",
                        tiers=p.get("TIERS"), condition=p.get("CONDITION"), valid_from=p["VALID_FROM"], valid_to=p.get("VALID_TO"),
                        currency_code=p["CURRENCY_CODE"])
            try:
                r = save_rate(s, actor, v.id, data)
                r.source_ref = p.get("SOURCE_REF")
            except BusinessError as exc:
                late.append((i, raw, str(exc)))
    return late, versions
