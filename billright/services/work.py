"""Manual billable entries (F3 manual form, workflow 8.4, G2 mobile capture)."""
from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from .. import models as m
from ..config import Config
from .common import Actor, BusinessError, audit, ensure_open, period_for_date, row_dict

ALLOWED_PHOTO = {".jpg", ".jpeg", ".png", ".heic", ".webp"}


def save_photo(file_storage) -> str | None:
    """Store an uploaded photo outside the database; return its reference."""
    if file_storage is None or not getattr(file_storage, "filename", ""):
        return None
    ext = Path(file_storage.filename).suffix.lower()
    if ext not in ALLOWED_PHOTO:
        raise BusinessError("Photo must be a JPG, PNG, HEIC or WebP image.")
    Config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ref = f"photo/{uuid.uuid4().hex}{ext}"
    (Config.UPLOAD_DIR / "photo").mkdir(parents=True, exist_ok=True)
    file_storage.save(Config.UPLOAD_DIR / ref)
    return ref


def submit_entry(s, actor: Actor, site_id, customer_id, charge_code_id, quantity, uom, service_date: dt.date, work_start, work_end, note,
                 photo_ref=None, work_order_ref=None):
    if not note or not note.strip():
        raise BusinessError("Add a short note describing the work.")
    q = Decimal(str(quantity))
    if q <= 0:
        raise BusinessError("Quantity must be greater than zero.")
    cc = s.get(m.ChargeCode, charge_code_id)
    if cc is None or not cc.manual_capture:
        raise BusinessError("This service cannot be captured manually.")
    cust = s.get(m.Customer, customer_id)
    if cust is None or cust.site_id != site_id:
        raise BusinessError("Choose one of your customers.")
    periods = s.execute(select(m.BillingPeriod).where(m.BillingPeriod.site_id == site_id)).scalars().all()
    p = period_for_date(periods, service_date)
    if p is None:
        raise BusinessError(f"No billing period covers {service_date:%d %b %Y}.")
    ensure_open(p, "Recording work")
    if work_start and work_end and work_end < work_start:
        raise BusinessError("End time is before start time.")
    e = m.ManualBillableEntry(tenant_id=actor.tenant_id, site_id=site_id, customer_id=customer_id, charge_code_id=charge_code_id, quantity=q,
                              uom=uom or cc.default_uom, service_date=service_date, work_start=work_start, work_end=work_end, note=note.strip(),
                              photo_ref=photo_ref, work_order_ref=work_order_ref, entered_by=actor.user_id, status="submitted")
    s.add(e)
    s.flush()
    audit(s, actor, "manual_entry.submit", "manual_billable_entry", e.id, None, row_dict(e))
    return e


def review_entry(s, actor: Actor, entry_id, accept: bool, reason: str | None = None):
    e = s.get(m.ManualBillableEntry, entry_id)
    if e is None or e.status != "submitted":
        raise BusinessError("Only submitted entries can be reviewed.")
    if e.entered_by == actor.user_id:
        raise BusinessError("You cannot review your own entry.")
    if not accept and not (reason or "").strip():
        raise BusinessError("A reason is required to reject an entry.")
    periods = s.execute(select(m.BillingPeriod).where(m.BillingPeriod.site_id == e.site_id)).scalars().all()
    ensure_open(period_for_date(periods, e.service_date), "Reviewing this entry")
    before = {"status": e.status}
    e.status = "accepted" if accept else "rejected"
    e.reviewed_by = actor.user_id
    e.reviewed_at = dt.datetime.now(dt.timezone.utc)
    e.review_reason = reason
    audit(s, actor, "manual_entry." + e.status, "manual_billable_entry", e.id, before, {"status": e.status}, reason)
    return e
