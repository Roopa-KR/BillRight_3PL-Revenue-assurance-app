"""Shared service helpers: audit log, settings catalogue, periods, active batches."""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from .. import models as m


class BusinessError(Exception):
    """A rule of the business prevents the action; the message is shown to the user as written."""


@dataclass
class Actor:
    tenant_id: int
    user_id: int | None
    roles: frozenset = frozenset()


def _jsonable(v):
    if isinstance(v, (Decimal,)):
        return str(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    return v


def row_dict(obj, fields=None) -> dict:
    cols = fields or [c.key for c in obj.__table__.columns]
    return {c: _jsonable(getattr(obj, c)) for c in cols}


def audit(s, actor: Actor, action: str, entity: str, entity_id, before=None, after=None, reason=None):
    s.add(m.AuditLog(tenant_id=actor.tenant_id, user_id=actor.user_id, action=action, entity=entity,
                     entity_id=None if entity_id is None else str(entity_id), before=_jsonable(before),
                     after=_jsonable(after), reason=reason))


# ------------------------------------------------------------------ settings catalogue (no invented defaults)
RULES = ("L1", "L2", "L3", "L4", "L5", "L6", "L7")
SETTINGS = {}
for _r in RULES:
    SETTINGS[f"tolerance.{_r}.amount"] = dict(desc=f"{_r}: gap amount (USD) that must be exceeded to raise an item", type="decimal", required="run", scope="site")
    SETTINGS[f"tolerance.{_r}.pct"] = dict(desc=f"{_r}: gap as % of billable that must be exceeded to raise an item", type="decimal", required="run", scope="site")
SETTINGS.update({
    "leakage.approval_threshold": dict(desc="Leakage decisions with |gap| above this amount (USD) need a second person's approval", type="decimal", required="decide", scope="site"),
    "leakage.back_billing_window_days": dict(desc="'Bill now' is not allowed for periods that ended more than this many days ago", type="int", required="decide", scope="site"),
    "labor.cost_basis": dict(desc="Labor cost basis: employee_labor_cost (imported paid hours and loaded cost) or standard_rate", type="choice:employee_labor_cost,standard_rate", required="labor", scope="site"),
    "labor.standard_rates": dict(desc="Standard loaded rate per activity (JSON {activity: rate}); used only when the basis is standard_rate", type="json", required=None, scope="site"),
    "customer_resolution.join_path": dict(desc="Join path per event type from each operational record to the 3PL customer (JSON). Must be verified against the deployed GiNi schema.", type="json", required="run", scope="tenant"),
    "gini.worktype_activity": dict(desc="GiNi WORKTYPE -> activity code map for TI102/TI102C (JSON)", type="json", required="import:gini_ti102c", scope="tenant"),
    "close.margin_range_low": dict(desc="Close check 6: customer margins below this % need acknowledgement", type="decimal", required="close", scope="site"),
    "close.margin_range_high": dict(desc="Close check 6: customer margins above this % need acknowledgement", type="decimal", required="close", scope="site"),
    "invoice.source_system": dict(desc="Invoice source system and format for the pilot (open decision 7)", type="str", required=None, scope="tenant"),
    "pilot.subscription_cost": dict(desc="Subscription cost (USD) for the pilot results export (return on subscription)", type="decimal", required=None, scope="tenant"),
    "storage.daily_snapshots_available": dict(desc="Whether daily inventory snapshots per customer exist in GiNi (open decision 8; needed for G1)", type="choice:yes,no", required=None, scope="site"),
    "storage.policy": dict(desc='G1 storage policy JSON: {"unit_type","method":"average_daily|unit_days|peak","output_uom","grace_days","split_month":"prorate|full_month"}', type="json", required="storage", scope="site"),
    "alerts.daily_run_hour": dict(desc="Local hour (0-23) for the daily rating and rule run (G3); empty = off", type="int", required=None, scope="site"),
    "drift.renewal_warning_days": dict(desc="G6: warn when a contract ends within this many days", type="int", required="drift", scope="site"),
    "accounting.adapter": dict(desc="G8: accounting adapter (file is the documented starting format)", type="choice:file,quickbooks,netsuite,sap,dynamics", required="export", scope="tenant"),
})


def get_setting(s, tenant_id: int, key: str, site_id: int | None = None):
    rows = s.execute(select(m.Setting).where(m.Setting.key == key, m.Setting.tenant_id == tenant_id)).scalars().all()
    site_row = next((r for r in rows if site_id is not None and r.site_id == site_id), None)
    row = site_row or next((r for r in rows if r.site_id is None), None)
    if row is None or row.value in (None, ""):
        return None
    t = SETTINGS.get(key, {}).get("type", "str")
    if key.startswith("margin_target."):
        t = "decimal"
    return parse_setting(t, row.value)


def parse_setting(t: str, value: str):
    if t == "decimal":
        return Decimal(value)
    if t == "int":
        return int(value)
    if t == "json":
        return json.loads(value)
    return value


def set_setting(s, actor: Actor, key: str, value, site_id: int | None = None):
    spec = SETTINGS.get(key)
    if spec is None and not key.startswith("margin_target."):
        raise BusinessError(f"Unknown setting {key}")
    t = spec["type"] if spec else "decimal"
    text = None if value in (None, "") else (json.dumps(value) if (t == "json" and not isinstance(value, str)) else str(value))
    if text is not None:
        try:
            parsed = parse_setting(t, text)
        except Exception as exc:
            raise BusinessError(f"{key}: value is not a valid {t}") from exc
        if t.startswith("choice:") and text not in t.split(":", 1)[1].split(","):
            raise BusinessError(f"{key}: choose one of {t.split(':', 1)[1]}")
        if t in ("decimal", "int") and parsed < 0:
            raise BusinessError(f"{key}: must not be negative")
    row = s.execute(select(m.Setting).where(m.Setting.key == key, m.Setting.tenant_id == actor.tenant_id,
                                            (m.Setting.site_id == site_id) if site_id is not None else m.Setting.site_id.is_(None))).scalar_one_or_none()
    before = row.value if row else None
    if row is None:
        row = m.Setting(tenant_id=actor.tenant_id, site_id=site_id, key=key, value=text, updated_by=actor.user_id)
        s.add(row)
    else:
        row.value = text
        row.updated_by = actor.user_id
        row.updated_at = dt.datetime.now(dt.timezone.utc)
    s.flush()
    audit(s, actor, "setting.update", "setting", key, {"value": before, "site_id": site_id}, {"value": text, "site_id": site_id})


def missing_settings(s, tenant_id: int, site_id: int, purpose: str) -> list[str]:
    return [k for k, v in SETTINGS.items() if v["required"] == purpose and get_setting(s, tenant_id, k, site_id) is None]


# ------------------------------------------------------------------ periods
def period_for_date(periods, day: dt.date):
    for p in periods:
        if p.start_date <= day <= p.end_date:
            return p
    return None


def ensure_open(period: m.BillingPeriod, what: str = "This change"):
    if period.status == "closed":
        raise BusinessError(f"{what} is not allowed: period {period.label} is closed. A controller can reopen it with a reason.")


def get_period(s, period_id: int) -> m.BillingPeriod:
    p = s.get(m.BillingPeriod, period_id)
    if p is None:
        raise BusinessError("Period not found")
    return p


def active_batch_ids(s, site_id: int, source_types=None, as_of_ids=None) -> list[int]:
    q = select(m.ImportBatch.id).where(m.ImportBatch.site_id == site_id, m.ImportBatch.status == "accepted")
    if source_types:
        q = q.where(m.ImportBatch.source_type.in_(list(source_types)))
    ids = list(s.execute(q).scalars())
    if as_of_ids is not None:
        frozen = set(as_of_ids)
        # Re-running a closed period uses exactly the batches recorded by the close run, even if superseded since.
        q2 = select(m.ImportBatch.id).where(m.ImportBatch.site_id == site_id, m.ImportBatch.id.in_(frozen))
        if source_types:
            q2 = q2.where(m.ImportBatch.source_type.in_(list(source_types)))
        ids = list(s.execute(q2).scalars())
    return sorted(ids)
