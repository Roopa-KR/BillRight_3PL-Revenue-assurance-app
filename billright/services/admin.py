"""Administration: tenants (owner connection only), sites, users, roles, scopes, periods, charge codes,
allocation drivers (draft/approve), central overhead, manual expense lines."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select

from .. import models as m
from ..engine.allocation import DRIVER_TYPES, EXPENSE_CATEGORIES, PAYMENT_RECIPIENTS
from ..security import ROLES
from .common import Actor, BusinessError, audit, ensure_open, row_dict

CHARGE_CATEGORIES = ("receiving", "storage", "picking", "packing", "transport", "vas", "special_project", "fee", "surcharge")


def create_tenant(owner_session, name: str) -> m.Tenant:
    t = m.Tenant(name=name)
    owner_session.add(t)
    owner_session.flush()
    return t


def create_site(s, actor: Actor, code, name, time_zone, currency_code, data_label=None):
    site = m.Site(tenant_id=actor.tenant_id, code=code, name=name, time_zone=time_zone, currency_code=currency_code, data_label=data_label)
    s.add(site)
    s.flush()
    audit(s, actor, "site.create", "site", site.id, None, row_dict(site))
    return site


def create_user(s, actor: Actor, email, name, roles, scopes):
    """``scopes`` = [(site_id, customer_id or None)]."""
    bad = [r for r in roles if r not in ROLES]
    if bad:
        raise BusinessError(f"Unknown role(s): {', '.join(bad)}")
    u = m.AppUser(tenant_id=actor.tenant_id, email=email.lower(), name=name, active=True)
    s.add(u)
    s.flush()
    for r in roles:
        s.add(m.UserRole(tenant_id=actor.tenant_id, user_id=u.id, role=r))
    for site_id, customer_id in scopes:
        s.add(m.UserScope(tenant_id=actor.tenant_id, user_id=u.id, site_id=site_id, customer_id=customer_id))
    s.flush()
    audit(s, actor, "user.create", "app_user", u.id, None, {"email": u.email, "roles": list(roles), "scopes": scopes})
    return u


def update_user(s, actor: Actor, user_id, roles=None, scopes=None, active=None):
    u = s.get(m.AppUser, user_id)
    before = {"roles": sorted(r.role for r in s.execute(select(m.UserRole).where(m.UserRole.user_id == user_id)).scalars()),
              "scopes": [(x.site_id, x.customer_id) for x in s.execute(select(m.UserScope).where(m.UserScope.user_id == user_id)).scalars()],
              "active": u.active}
    if roles is not None:
        bad = [r for r in roles if r not in ROLES]
        if bad:
            raise BusinessError(f"Unknown role(s): {', '.join(bad)}")
        for r in s.execute(select(m.UserRole).where(m.UserRole.user_id == user_id)).scalars():
            s.delete(r)
        s.flush()
        for r in roles:
            s.add(m.UserRole(tenant_id=actor.tenant_id, user_id=user_id, role=r))
    if scopes is not None:
        for x in s.execute(select(m.UserScope).where(m.UserScope.user_id == user_id)).scalars():
            s.delete(x)
        s.flush()
        for site_id, customer_id in scopes:
            s.add(m.UserScope(tenant_id=actor.tenant_id, user_id=user_id, site_id=site_id, customer_id=customer_id))
    if active is not None:
        u.active = active
    s.flush()
    audit(s, actor, "user.update", "app_user", user_id, before, {"roles": roles, "scopes": scopes, "active": active})


def create_period(s, actor: Actor, site_id, label, start: dt.date, end: dt.date, period_type: str):
    if period_type not in ("week", "month"):
        raise BusinessError("Period type must be week or month.")
    if end < start:
        raise BusinessError("Period end must be on or after its start.")
    overlap = s.execute(select(m.BillingPeriod).where(m.BillingPeriod.site_id == site_id, m.BillingPeriod.start_date <= end,
                                                      m.BillingPeriod.end_date >= start)).scalars().first()
    if overlap:
        raise BusinessError(f"Overlaps period {overlap.label}.")
    p = m.BillingPeriod(tenant_id=actor.tenant_id, site_id=site_id, label=label, start_date=start, end_date=end, period_type=period_type, status="open")
    s.add(p)
    s.flush()
    audit(s, actor, "period.create", "billing_period", p.id, None, row_dict(p))
    return p


def create_charge_code(s, actor: Actor, code, name, category, default_uom=None, event_type=None, description=None, manual_capture=False):
    if category not in CHARGE_CATEGORIES:
        raise BusinessError(f"Category must be one of {', '.join(CHARGE_CATEGORIES)}.")
    if manual_capture and not event_type:
        raise BusinessError("A manually capturable charge needs an event type (e.g. 'vas_hours').")
    c = m.ChargeCode(tenant_id=actor.tenant_id, code=code, name=name, category=category, default_uom=default_uom, event_type=event_type,
                     description=description, manual_capture=manual_capture)
    s.add(c)
    s.flush()
    audit(s, actor, "charge_code.create", "charge_code", c.id, None, row_dict(c))
    return c


def create_driver(s, actor: Actor, site_id, expense_category, driver_type, description, valid_from, valid_to=None, data_label=None):
    if expense_category not in EXPENSE_CATEGORIES:
        raise BusinessError("Unknown expense category.")
    if driver_type not in DRIVER_TYPES:
        raise BusinessError("Unknown driver type.")
    if not description:
        raise BusinessError("Describe how the driver is measured.")
    d = m.AllocationDriver(tenant_id=actor.tenant_id, site_id=site_id, expense_category=expense_category, driver_type=driver_type,
                           description=description, status="draft", created_by=actor.user_id, valid_from=valid_from, valid_to=valid_to,
                           is_proxy=driver_type == "completed_line_share_proxy", data_label=data_label)
    s.add(d)
    s.flush()
    audit(s, actor, "driver.create", "allocation_driver", d.id, None, row_dict(d))
    return d


def approve_driver(s, actor: Actor, driver_id):
    d = s.get(m.AllocationDriver, driver_id)
    if d.status != "draft":
        raise BusinessError("Only a draft driver can be approved.")
    if d.created_by == actor.user_id:
        raise BusinessError("Segregation of duties: the person who defined a driver cannot approve it.")
    others = s.execute(select(m.AllocationDriver).where(m.AllocationDriver.site_id == d.site_id, m.AllocationDriver.expense_category == d.expense_category,
                                                        m.AllocationDriver.status == "approved", m.AllocationDriver.id != d.id)).scalars().all()
    for o in others:
        if o.valid_to is None or o.valid_to >= d.valid_from:
            before = row_dict(o)
            o.valid_to = d.valid_from - dt.timedelta(days=1)
            audit(s, actor, "driver.end", "allocation_driver", o.id, before, row_dict(o), f"replaced by driver {d.id}")
    d.status = "approved"
    d.approved_by = actor.user_id
    d.approved_at = dt.datetime.now(dt.timezone.utc)
    audit(s, actor, "driver.approve", "allocation_driver", d.id, {"status": "draft"}, {"status": "approved"})
    return d


def enter_driver_quantity(s, actor: Actor, driver_id, period_id, customer_id, quantity, source_ref):
    p = s.get(m.BillingPeriod, period_id)
    ensure_open(p, "Entering driver quantities")
    if Decimal(quantity) < 0:
        raise BusinessError("Driver quantity must not be negative.")
    q = m.AllocationDriverQuantity(tenant_id=actor.tenant_id, driver_id=driver_id, period_id=period_id, customer_id=customer_id,
                                   quantity=quantity, source_ref=source_ref, entered_by=actor.user_id)
    s.add(q)
    s.flush()
    audit(s, actor, "driver_quantity.create", "allocation_driver_quantity", q.id, None, row_dict(q))
    return q


def enter_central_overhead(s, actor: Actor, site_id, period_id, amount, note):
    p = s.get(m.BillingPeriod, period_id)
    ensure_open(p, "Entering central overhead")
    c = m.CentralOverhead(tenant_id=actor.tenant_id, site_id=site_id, period_id=period_id, amount=amount, note=note, entered_by=actor.user_id)
    s.add(c)
    s.flush()
    audit(s, actor, "central_overhead.create", "central_overhead", c.id, None, row_dict(c))
    return c


def enter_expense(s, actor: Actor, site_id, period_id, category, description, amount, recipient, customer_id=None, source_ref="manual"):
    p = s.get(m.BillingPeriod, period_id)
    ensure_open(p, "Entering expenses")
    if category not in EXPENSE_CATEGORIES or recipient not in PAYMENT_RECIPIENTS:
        raise BusinessError("Choose a valid expense category and payment recipient.")
    e = m.ExpenseLine(tenant_id=actor.tenant_id, site_id=site_id, period_id=period_id, expense_category=category, description=description,
                      amount=amount, source_type="manual", source_ref=source_ref, payment_recipient_type=recipient, customer_id=customer_id,
                      entered_by=actor.user_id)
    s.add(e)
    s.flush()
    audit(s, actor, "expense.create", "expense_line", e.id, None, row_dict(e))
    return e
