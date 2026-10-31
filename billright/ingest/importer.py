"""Import centre (5.3): immutable batches, validation, plain-language import report.

* Every import creates an immutable ``import_batch`` with file hash, row count, user, time and source type.
* Re-importing an identical file (same hash, source type and site) is a no-op that returns the existing batch.
* A changed file for the same scope supersedes the previous batch of that scope; old rows remain stored.
* Rows are validated (required columns, types, duplicate keys, unknown customers, unknown charge codes,
  periods outside the open periods, negative quantities). Accepted rows are stored, rejected rows are listed
  with their reason in the batch report and can be downloaded. The report always states both counts.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import hashlib
import io
import json
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .. import models as m
from ..engine.allocation import EXPENSE_CATEGORIES, PAYMENT_RECIPIENTS
from ..engine.rating import PRICING_METHODS
from ..services.common import Actor, BusinessError, audit, get_setting
from .sources import SOURCE_TYPES
from .copy import copy_rows

VENDOR_RECIPIENT = {"staffing_agency": "staffing_agency", "carrier": "carrier"}
ROLE_CATEGORY = {"operator": "warehouse_operators", "forklift": "forklift_operators", "supervisor": "supervisors",
                 "manager": "managers", "admin": "administration"}


# ------------------------------------------------------------------ reading
def read_table(path: Path, raw: bytes | None = None):
    name = path.name.lower()
    data = raw if raw is not None else path.read_bytes()
    if name.endswith(".xlsx"):
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in next(it)]
        rows = []
        for vals in it:
            if vals is None or all(v in (None, "") for v in vals):
                continue
            rows.append({header[i]: ("" if v is None else (v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else str(v)))
                         for i, v in enumerate(vals) if i < len(header)})
        return header, rows
    if name.endswith(".gz"):
        data = gzip.decompress(data)
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows = [dict(r) for r in reader]
    return list(reader.fieldnames or []), rows


def apply_mapping(header, rows, st, mapping: dict | None):
    lower = {h.lower(): h for h in header}
    resolved = {}
    for f in st.fields:
        col = (mapping or {}).get(f.name) or lower.get(f.name.lower())
        if col:
            resolved[f.name] = col
    missing = [f.name for f in st.fields if f.required and f.name not in resolved]
    out = [{name: (r.get(col) or "").strip() for name, col in resolved.items()} for r in rows]
    return out, missing


# ------------------------------------------------------------------ parsing
def parse_decimal(v: str) -> Decimal:
    try:
        return Decimal(v.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"'{v}' is not a number") from exc


def parse_date(v: str) -> dt.date:
    try:
        return dt.date.fromisoformat(v[:10])
    except ValueError as exc:
        raise ValueError(f"'{v}' is not a date (use YYYY-MM-DD)") from exc


def parse_datetime(v: str, tz: ZoneInfo) -> dt.datetime:
    try:
        d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"'{v}' is not a date-time (use ISO 8601)") from exc
    return d if d.tzinfo else d.replace(tzinfo=tz)


def parse_bool(v: str) -> bool:
    t = v.strip().lower()
    if t in ("1", "true", "yes", "y"):
        return True
    if t in ("0", "false", "no", "n"):
        return False
    raise ValueError(f"'{v}' is not true/false")


def parse_row(st, row, tz):
    out = {}
    for f in st.fields:
        v = row.get(f.name, "")
        if v == "":
            if f.required:
                raise ValueError(f"{f.name} is required")
            out[f.name] = None
            continue
        if f.type == "decimal":
            x = parse_decimal(v)
            if f.non_negative and x < 0:
                raise ValueError(f"{f.name} must not be negative ({v})")
            out[f.name] = x
        elif f.type == "int":
            try:
                x = int(Decimal(v))
            except InvalidOperation as exc:
                raise ValueError(f"{f.name}: '{v}' is not a whole number") from exc
            if f.non_negative and x < 0:
                raise ValueError(f"{f.name} must not be negative ({v})")
            out[f.name] = x
        elif f.type == "date":
            out[f.name] = parse_date(v)
        elif f.type == "datetime":
            out[f.name] = parse_datetime(v, tz)
        elif f.type == "bool":
            out[f.name] = parse_bool(v)
        elif f.type == "json":
            try:
                out[f.name] = json.loads(v)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{f.name}: not valid JSON") from exc
        else:
            out[f.name] = v
    return out


def _payload(row: dict) -> dict:
    return {k: (str(v) if isinstance(v, Decimal) else v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v) for k, v in row.items()}


# ------------------------------------------------------------------ main entry point
class ImportContext:
    def __init__(self, s, actor: Actor, site: m.Site):
        self.s, self.actor, self.site = s, actor, site
        self.tz = ZoneInfo(site.time_zone)
        self.periods = s.execute(select(m.BillingPeriod).where(m.BillingPeriod.site_id == site.id)).scalars().all()
        self.period_by_label = {p.label: p for p in self.periods}
        self.customers = {c.code: c for c in s.execute(select(m.Customer).where(m.Customer.site_id == site.id)).scalars()}
        self.identity = defaultdict(list)
        for r in s.execute(select(m.CustomerIdentityMap).where(m.CustomerIdentityMap.source_key_type == "customer_code")).scalars():
            self.identity[r.source_key_value].append(r.customer_id)
        site_customer_ids = {c.id for c in self.customers.values()}
        self.identity = {k: v for k, v in self.identity.items() if any(c in site_customer_ids for c in v)}
        self.charges = {c.code: c for c in s.execute(select(m.ChargeCode)).scalars()}

    def customer_id(self, code):
        ids = sorted(set(self.identity.get(code, [])))
        return ids[0] if len(ids) == 1 else None

    def open_period_for(self, day: dt.date):
        for p in self.periods:
            if p.start_date <= day <= p.end_date:
                return p
        return None


def import_file(s, actor: Actor, site: m.Site, source_type: str, file_name: str, data: bytes, mapping: dict | None = None,
                scope_key: str | None = None, data_label: str | None = None) -> m.ImportBatch:
    st = SOURCE_TYPES.get(source_type)
    if st is None:
        raise BusinessError(f"Unknown source type {source_type}")
    digest = hashlib.sha256(data).hexdigest()
    existing = s.execute(select(m.ImportBatch).where(m.ImportBatch.site_id == site.id, m.ImportBatch.source_type == source_type,
                                                     m.ImportBatch.file_hash == digest, m.ImportBatch.status != "rejected")).scalars().first()
    if existing is not None:
        existing.report = dict(existing.report or {}, last_duplicate_attempt=dt.datetime.now(dt.timezone.utc).isoformat())
        return existing
    if mapping is None:
        saved = s.execute(select(m.ColumnMapping).where(m.ColumnMapping.source_type == source_type)).scalar_one_or_none()
        mapping = saved.mapping if saved else None
    header, raw_rows = read_table(Path(file_name), data)
    rows, missing_cols = apply_mapping(header, raw_rows, st, mapping)
    ctx = ImportContext(s, actor, site)
    batch = m.ImportBatch(tenant_id=actor.tenant_id, site_id=site.id, source_type=source_type, scope_key="pending",
                          file_name=Path(file_name).name, file_hash=digest, row_count=len(rows), status="accepted",
                          created_by=actor.user_id, data_label=data_label or site.data_label)
    s.add(batch)
    s.flush()
    if missing_cols:
        batch.status = "rejected"
        batch.rejected_count = len(rows)
        batch.scope_key = scope_key or "n/a"
        batch.report = {"summary": f"Rejected: required column(s) missing: {', '.join(missing_cols)}. Nothing was imported.",
                        "missing_columns": missing_cols, "accepted": 0, "rejected": len(rows)}
        audit(s, actor, "import.rejected", "import_batch", batch.id, None, batch.report)
        return batch
    if st.code == "gini_ti102c" and not get_setting(s, actor.tenant_id, "gini.worktype_activity"):
        batch.status = "rejected"
        batch.scope_key = scope_key or "n/a"
        batch.rejected_count = len(rows)
        batch.report = {"summary": "Rejected: fill the setting 'gini.worktype_activity' (WORKTYPE -> activity map) first. Nothing was imported.",
                        "accepted": 0, "rejected": len(rows)}
        return batch

    accepted, rejected = [], []
    seen = set()
    for i, raw in enumerate(rows, start=2):  # row 1 is the header
        try:
            parsed = parse_row(st, raw, ctx.tz)
            key = tuple(json.dumps(parsed.get(k), default=str) for k in st.key)
            if key in seen:
                raise ValueError("duplicate key " + ", ".join(f"{k}={parsed.get(k)}" for k in st.key))
            seen.add(key)
            _validate_refs(st, parsed, ctx)
            accepted.append((i, raw, parsed))
        except ValueError as exc:
            rejected.append((i, raw, str(exc)))

    # scope: files for the same scope replace each other
    if scope_key is None:
        if st.period_field:
            scope_key = ",".join(sorted({p[st.period_field] for _, _, p in accepted if p.get(st.period_field)})) or "none"
        else:
            scope_key = Path(file_name).name.split(".")[0]
    batch.scope_key = scope_key
    n_valid = len(accepted)
    handler_report = HANDLERS.get(st.code, _snapshot_only)(ctx, batch, st, accepted, rejected)
    batch.accepted_count = n_valid - len(handler_report.get("late_rejected", []))
    for i, raw, reason in handler_report.get("late_rejected", []):
        rejected.append((i, raw, reason))
    batch.rejected_count = len(rejected)
    if batch.accepted_count == 0 and rows:
        batch.status = "rejected"
    for i, raw, reason in rejected[:50000]:
        s.add(m.ImportRejectedRow(tenant_id=actor.tenant_id, import_batch_id=batch.id, row_no=i, reason=reason, payload=raw))
    reasons = Counter(r for _, _, r in rejected)
    summary = f"{batch.accepted_count:,} row(s) accepted, {batch.rejected_count:,} row(s) rejected."
    if batch.status == "rejected":
        summary = f"Rejected: no row could be imported ({batch.rejected_count:,} row(s) rejected)."
    batch.report = {"summary": summary, "accepted": batch.accepted_count, "rejected": batch.rejected_count,
                    "top_reasons": [{"reason": r, "rows": n} for r, n in reasons.most_common(10)],
                    **{k: v for k, v in handler_report.items() if k != "late_rejected"}}
    if batch.status == "accepted":
        prev = s.execute(select(m.ImportBatch).where(m.ImportBatch.site_id == site.id, m.ImportBatch.source_type == source_type,
                                                     m.ImportBatch.scope_key == scope_key, m.ImportBatch.status == "accepted",
                                                     m.ImportBatch.id != batch.id)).scalars().all()
        for p in prev:
            p.status = "superseded"
            p.superseded_by = batch.id
        if prev:
            batch.report["supersedes"] = [p.id for p in prev]
    s.flush()
    audit(s, actor, "import." + batch.status, "import_batch", batch.id, None,
          {"source_type": source_type, "file": batch.file_name, "hash": digest, "accepted": batch.accepted_count,
           "rejected": batch.rejected_count, "scope_key": scope_key})
    return batch


def _validate_refs(st, row, ctx: ImportContext):
    if st.customer_field and row.get(st.customer_field):
        if ctx.customer_id(row[st.customer_field]) is None:
            raise ValueError(f"unknown customer {row[st.customer_field]} (not in the customer register / identity map)")
    if st.charge_field and row.get(st.charge_field):
        if row[st.charge_field] not in ctx.charges:
            raise ValueError(f"unknown charge code {row[st.charge_field]}")
    if st.period_field:
        p = ctx.period_by_label.get(row[st.period_field])
        if p is None:
            raise ValueError(f"unknown period {row[st.period_field]}")
        if p.status == "closed":
            raise ValueError(f"period {p.label} is closed")


# ------------------------------------------------------------------ handlers
def _snapshots(ctx, batch, st, accepted):
    def key(p):
        return "|".join(str(p.get(k) if p.get(k) is not None else "") for k in st.key)

    rows = []
    for _, raw, parsed in accepted:
        payload = _payload(parsed)
        text = json.dumps(payload, sort_keys=True)
        rows.append((ctx.actor.tenant_id, batch.id, st.source_table, key(parsed), text, hashlib.sha256(text.encode()).hexdigest()))
    copy_rows(ctx.s, "source_snapshot", ("tenant_id", "import_batch_id", "source_table", "record_key", "payload", "hash"), rows,
              json_cols={4})


def _snapshot_only(ctx, batch, st, accepted, rejected):
    _snapshots(ctx, batch, st, accepted)
    return {}


def _events(ctx, batch, st, accepted, build):
    """Create activity events for rows whose service date is in an open billing period. Rows outside open
    periods stay as reference snapshots without a billable event (reported, never silently dropped)."""
    _snapshots(ctx, batch, st, accepted)
    out, outside, closed = [], 0, 0
    for _, raw, p in accepted:
        when = p.get(st.date_field)
        if when is None:
            continue
        day = when.astimezone(ctx.tz).date() if isinstance(when, dt.datetime) else when
        period = ctx.open_period_for(day)
        if period is None:
            outside += 1
            continue
        if period.status == "closed":
            closed += 1
            continue
        ev = build(p, day)
        if ev is None:
            continue
        out.append((ctx.actor.tenant_id, ctx.site.id, ev.get("customer_id"), ev["source_ref"], st.events, None, day, ev["quantity"],
                    ev["uom"], ev.get("material_id"), ev.get("order_ref"), ev.get("task_ref"), ev.get("shipment_ref"),
                    json.dumps(ev.get("attributes") or {}, default=str), batch.id))
    copy_rows(ctx.s, "activity_event", ("tenant_id", "site_id", "customer_id", "source_ref", "event_type", "charge_code_id", "service_date",
                                         "quantity", "uom", "material_id", "order_ref", "task_ref", "shipment_ref", "attributes",
                                         "import_batch_id"), out, json_cols={13})
    rep = {"events_created": len(out)}
    if outside:
        rep["outside_periods"] = f"{outside:,} row(s) fall outside every billing period: kept as reference, no billable event."
    if closed:
        rep["closed_periods"] = f"{closed:,} row(s) fall in a closed period: kept as reference, no billable event."
    return rep


def _material_attrs(ctx):
    rows = ctx.s.execute(select(m.SourceSnapshot.record_key, m.SourceSnapshot.payload).join(
        m.ImportBatch, m.ImportBatch.id == m.SourceSnapshot.import_batch_id).where(
        m.ImportBatch.site_id == ctx.site.id, m.ImportBatch.status == "accepted", m.SourceSnapshot.source_table == "MATERIAL")).all()
    return {k: p for k, p in rows}


def _h_ti102c(ctx, batch, st, accepted, rejected):
    worktypes = get_setting(ctx.s, ctx.actor.tenant_id, "gini.worktype_activity")
    mats = _material_attrs(ctx)
    late = []
    keep = []
    for item in accepted:
        if str(item[2]["WORKTYPE"]) not in worktypes:
            late.append((item[0], item[1], f"WORKTYPE {item[2]['WORKTYPE']} has no activity mapping (setting gini.worktype_activity)"))
        else:
            keep.append(item)
    accepted[:] = keep

    def build(p, day):
        mat = mats.get(p.get("MATERIALID") or "", {})
        attrs = {"activity": worktypes[str(p["WORKTYPE"])], "worktype": p["WORKTYPE"], "worker": p.get("WORKER_ID") or p.get("RESOURCENAME"),
                 "pieces": str(p["PROCESSEDQTY"]), "piece_uom": p.get("UOM"), "completed_at": p["COMPLETED_AT"].isoformat(),
                 "created_at": p["CREATEDATETIME"].isoformat() if p.get("CREATEDATETIME") else None, "location": p.get("SRCLOC"),
                 "clientid_tenant": p.get("CLIENTID")}
        for k in ("TEMPERATURE_CLASS", "HANDLING_CLASS"):
            if mat.get(k):
                attrs[k.lower()] = mat[k]
        return {"source_ref": f"TI102C:{p['TI102ID']}", "quantity": Decimal(1), "uom": "line", "material_id": p.get("MATERIALID"),
                "order_ref": p["TI102X3ID"], "task_ref": p["TI102ID"], "attributes": attrs}

    rep = _events(ctx, batch, st, accepted, build)
    rep["late_rejected"] = late
    return rep


def _h_ti202(ctx, batch, st, accepted, rejected):
    return _events(ctx, batch, st, accepted, lambda p, day: {
        "source_ref": f"TI202:{p['TI202ID']}", "quantity": p["QUANTITY"], "uom": p["UOM"], "material_id": p.get("MATERIALID"),
        "order_ref": p.get("RECEIPT_REF"), "attributes": {"owner_code": p["OWNER_CODE"], "received_at": p["RECEIVED_AT"].isoformat()}})


def _h_order_header(ctx, batch, st, accepted, rejected):
    return _events(ctx, batch, st, accepted, lambda p, day: {
        "source_ref": f"ORDER:{p['ORDER_ID']}", "quantity": Decimal(1), "uom": "order", "order_ref": p["ORDER_ID"],
        "attributes": {"order_code": p.get("ORDER_CODE"), "completed_at": p["COMPLETED_AT"].isoformat(), "line_count": p.get("LINE_COUNT"),
                       "order_type": p.get("ORDER_TYPE")}})


def _h_storage(ctx, batch, st, accepted, rejected):
    return _events(ctx, batch, st, accepted, lambda p, day: {
        "source_ref": f"STORAGE:{p['STORAGE_REF']}", "quantity": p["QUANTITY"], "uom": p["UOM"],
        "attributes": {"customer_code": p["CUSTOMER_CODE"]}})


def _h_customer_master(ctx, batch, st, accepted, rejected):
    _snapshots(ctx, batch, st, accepted)
    created = updated = 0
    for _, raw, p in accepted:
        c = ctx.customers.get(p["CUSTOMER_CODE"])
        if c is None:
            c = m.Customer(tenant_id=ctx.actor.tenant_id, site_id=ctx.site.id, code=p["CUSTOMER_CODE"], name=p["CUSTOMER_NAME"],
                           segment=p.get("SEGMENT"), active=True if p.get("ACTIVE") is None else p["ACTIVE"])
            ctx.s.add(c)
            ctx.s.flush()
            ctx.customers[c.code] = c
            created += 1
            audit(ctx.s, ctx.actor, "customer.create", "customer", c.id, None, {"code": c.code, "name": c.name, "batch": batch.id})
        else:
            before = {"name": c.name, "segment": c.segment, "active": c.active}
            c.name, c.segment = p["CUSTOMER_NAME"], p.get("SEGMENT")
            if p.get("ACTIVE") is not None:
                c.active = p["ACTIVE"]
            if before != {"name": c.name, "segment": c.segment, "active": c.active}:
                updated += 1
                audit(ctx.s, ctx.actor, "customer.update", "customer", c.id, before, {"name": c.name, "segment": c.segment, "active": c.active})
        if c.id not in ctx.identity.get(c.code, []):
            ctx.s.add(m.CustomerIdentityMap(tenant_id=ctx.actor.tenant_id, source_system=p.get("SOURCE_SYSTEM") or "GiNi M140",
                                            source_key_type="customer_code", source_key_value=c.code, customer_id=c.id,
                                            valid_from=dt.date(1900, 1, 1), method_note="Customer master code (M140 family)"))
            ctx.identity.setdefault(c.code, []).append(c.id)
    return {"customers_created": created, "customers_updated": updated}


def _h_uom(ctx, batch, st, accepted, rejected):
    _snapshots(ctx, batch, st, accepted)
    for _, raw, p in accepted:
        ctx.s.add(m.UomConversion(tenant_id=ctx.actor.tenant_id, material_id=p.get("MATERIAL_ID"), from_uom=p["FROM_UOM"],
                                  to_uom=p["TO_UOM"], multiplier=p["MULTIPLIER"], valid_from=p["VALID_FROM"], valid_to=p.get("VALID_TO"),
                                  import_batch_id=batch.id))
    return {}


def _h_invoices(ctx, batch, st, accepted, rejected):
    _snapshots(ctx, batch, st, accepted)
    headers = {}
    late = []
    by_inv = defaultdict(set)
    for _, _, p in accepted:
        by_inv[p["INVOICE_NUMBER"]].add((p["CUSTOMER_CODE"], p["PERIOD"]))
    for i, raw, p in accepted:
        if len(by_inv[p["INVOICE_NUMBER"]]) > 1:
            late.append((i, raw, f"invoice {p['INVOICE_NUMBER']} mixes customers or periods"))
            continue
        hkey = p["INVOICE_NUMBER"]
        if hkey not in headers:
            h = m.InvoiceHeader(tenant_id=ctx.actor.tenant_id, site_id=ctx.site.id, customer_id=ctx.customer_id(p["CUSTOMER_CODE"]),
                                invoice_number=hkey, invoice_date=p.get("INVOICE_DATE"), period_id=ctx.period_by_label[p["PERIOD"]].id,
                                import_batch_id=batch.id)
            ctx.s.add(h)
            ctx.s.flush()
            headers[hkey] = h
        cc = ctx.charges[p["CHARGE_CODE"]]
        ctx.s.add(m.InvoiceLine(tenant_id=ctx.actor.tenant_id, invoice_header_id=headers[hkey].id, line_number=p["LINE_NUMBER"],
                                charge_code_id=cc.id, raw_charge_code=p["CHARGE_CODE"], quantity=p.get("QUANTITY"), uom=p.get("UOM"),
                                unit_price=p.get("UNIT_PRICE"), amount=p["AMOUNT"], posted_on=p.get("POSTED_ON"),
                                adjustment_ref=p.get("ADJUSTMENT_REF")))
    return {"invoices": len(headers), "late_rejected": late}


def _h_labor_cost(ctx, batch, st, accepted, rejected):
    _snapshots(ctx, batch, st, accepted)
    late = []
    for i, raw, p in accepted:
        if p["EMPLOYER_TYPE"] not in ("employee", "agency"):
            late.append((i, raw, "EMPLOYER_TYPE must be employee or agency"))
            continue
        if p["ROLE"] not in ROLE_CATEGORY:
            late.append((i, raw, f"ROLE must be one of {', '.join(ROLE_CATEGORY)}"))
            continue
        ctx.s.add(m.EmployeeLaborCost(tenant_id=ctx.actor.tenant_id, site_id=ctx.site.id, period_id=ctx.period_by_label[p["PERIOD"]].id,
                                      worker_id=p["WORKER_ID"], employer_type=p["EMPLOYER_TYPE"], role=p["ROLE"], paid_hours=p["PAID_HOURS"],
                                      loaded_cost=p["LOADED_COST"], overtime_hours=p.get("OVERTIME_HOURS"), import_batch_id=batch.id))
    return {"late_rejected": late}


def _h_attribution(ctx, batch, st, accepted, rejected):
    _snapshots(ctx, batch, st, accepted)
    late = []
    for i, raw, p in accepted:
        if not p.get("CUSTOMER_CODE") and not p.get("UNASSIGNED_REASON"):
            late.append((i, raw, "UNASSIGNED_REASON is required when CUSTOMER_CODE is blank"))
            continue
        if p["BASIS"] not in ("task_time", "documented_time", "proxy"):
            late.append((i, raw, "BASIS must be task_time, documented_time or proxy"))
            continue
        ctx.s.add(m.LaborTimeAttribution(tenant_id=ctx.actor.tenant_id, site_id=ctx.site.id, period_id=ctx.period_by_label[p["PERIOD"]].id,
                                         worker_id=p["WORKER_ID"], customer_id=ctx.customer_id(p["CUSTOMER_CODE"]) if p.get("CUSTOMER_CODE") else None,
                                         hours=p["HOURS"], basis=p["BASIS"], unassigned_reason=p.get("UNASSIGNED_REASON"), import_batch_id=batch.id))
    return {"late_rejected": late}


def _h_vendor(ctx, batch, st, accepted, rejected):
    _snapshots(ctx, batch, st, accepted)
    late = []
    vendors = {v.name: v for v in ctx.s.execute(select(m.Vendor)).scalars()}
    headers = {}
    pass_through_customer_paid = 0
    for i, raw, p in accepted:
        if p["EXPENSE_CATEGORY"] not in EXPENSE_CATEGORIES:
            late.append((i, raw, f"EXPENSE_CATEGORY must be one of {', '.join(EXPENSE_CATEGORIES)}"))
            continue
        vtype = p["VENDOR_TYPE"]
        if vtype not in ("staffing_agency", "carrier", "landlord", "equipment", "packaging", "it", "facility_services", "other"):
            late.append((i, raw, "unknown VENDOR_TYPE"))
            continue
        v = vendors.get(p["VENDOR"])
        if v is None:
            v = m.Vendor(tenant_id=ctx.actor.tenant_id, name=p["VENDOR"], vendor_type=vtype)
            ctx.s.add(v)
            ctx.s.flush()
            vendors[v.name] = v
        hk = (p["VENDOR"], p["INVOICE_NUMBER"])
        period = ctx.period_by_label[p["PERIOD"]]
        if hk not in headers:
            h = m.VendorInvoiceHeader(tenant_id=ctx.actor.tenant_id, vendor_id=v.id, site_id=ctx.site.id, invoice_number=p["INVOICE_NUMBER"],
                                      invoice_date=p.get("INVOICE_DATE"), period_id=period.id, po_ref=p.get("PO_REF"), import_batch_id=batch.id)
            ctx.s.add(h)
            ctx.s.flush()
            headers[hk] = h
        cust = ctx.customer_id(p["CUSTOMER_CODE"]) if p.get("CUSTOMER_CODE") else None
        ctx.s.add(m.VendorInvoiceLine(tenant_id=ctx.actor.tenant_id, header_id=headers[hk].id, service=p["SERVICE"],
                                      expense_category=p["EXPENSE_CATEGORY"], expense_account=p.get("EXPENSE_ACCOUNT"), customer_id=cust,
                                      shipment_ref=p.get("SHIPMENT_REF"), amount=p["AMOUNT"],
                                      customer_pays_carrier=bool(p.get("CUSTOMER_PAYS_CARRIER"))))
        if p.get("CUSTOMER_PAYS_CARRIER"):
            # The customer contracts and pays the carrier: neither the 3PL's cost nor its revenue.
            pass_through_customer_paid += 1
            continue
        source = {"staffing_agency": "agency_invoice", "carrier": "carrier_invoice"}.get(vtype, "vendor_invoice")
        ctx.s.add(m.ExpenseLine(tenant_id=ctx.actor.tenant_id, site_id=ctx.site.id, period_id=period.id, expense_category=p["EXPENSE_CATEGORY"],
                                description=f"{p['VENDOR']}: {p['SERVICE']}", amount=p["AMOUNT"], source_type=source,
                                source_ref=f"{p['VENDOR']}/{p['INVOICE_NUMBER']}/{p['LINE_NUMBER']}",
                                payment_recipient_type=VENDOR_RECIPIENT.get(vtype, "facility_equipment_it_other"), customer_id=cust,
                                import_batch_id=batch.id))
    rep = {"vendor_invoices": len(headers), "late_rejected": late}
    if pass_through_customer_paid:
        rep["customer_paid_carrier_lines"] = f"{pass_through_customer_paid} line(s) paid by the customer directly: recorded, not a cost."
    return rep


def _h_ledger(ctx, batch, st, accepted, rejected):
    _snapshots(ctx, batch, st, accepted)
    late = []
    for i, raw, p in accepted:
        if p["EXPENSE_CATEGORY"] not in EXPENSE_CATEGORIES:
            late.append((i, raw, f"EXPENSE_CATEGORY must be one of {', '.join(EXPENSE_CATEGORIES)}"))
            continue
        if p["PAYMENT_RECIPIENT_TYPE"] not in PAYMENT_RECIPIENTS:
            late.append((i, raw, f"PAYMENT_RECIPIENT_TYPE must be one of {', '.join(PAYMENT_RECIPIENTS)}"))
            continue
        if p["SOURCE_TYPE"] not in ("payroll", "agency_invoice", "carrier_invoice", "vendor_invoice", "manual"):
            late.append((i, raw, "SOURCE_TYPE must be payroll, agency_invoice, carrier_invoice, vendor_invoice or manual"))
            continue
        ctx.s.add(m.ExpenseLine(tenant_id=ctx.actor.tenant_id, site_id=ctx.site.id, period_id=ctx.period_by_label[p["PERIOD"]].id,
                                expense_category=p["EXPENSE_CATEGORY"], description=p["DESCRIPTION"], amount=p["AMOUNT"],
                                source_type=p["SOURCE_TYPE"], source_ref=p["REF"], payment_recipient_type=p["PAYMENT_RECIPIENT_TYPE"],
                                customer_id=ctx.customer_id(p["CUSTOMER_CODE"]) if p.get("CUSTOMER_CODE") else None, import_batch_id=batch.id))
    return {"late_rejected": late}


def _h_drivers(ctx, batch, st, accepted, rejected):
    _snapshots(ctx, batch, st, accepted)
    late = []
    drivers = ctx.s.execute(select(m.AllocationDriver).where(m.AllocationDriver.site_id == ctx.site.id)).scalars().all()
    for i, raw, p in accepted:
        period = ctx.period_by_label[p["PERIOD"]]
        cands = [d for d in drivers if d.expense_category == p["EXPENSE_CATEGORY"] and d.valid_from <= period.start_date
                 and (d.valid_to is None or d.valid_to >= period.start_date)]
        if not cands:
            late.append((i, raw, f"no allocation driver defined for {p['EXPENSE_CATEGORY']} in {period.label}; define it in Admin first"))
            continue
        d = sorted(cands, key=lambda x: (x.status != "approved", -x.id))[0]
        ctx.s.add(m.AllocationDriverQuantity(tenant_id=ctx.actor.tenant_id, driver_id=d.id, period_id=period.id,
                                             customer_id=ctx.customer_id(p["CUSTOMER_CODE"]) if p.get("CUSTOMER_CODE") else None,
                                             quantity=p["QUANTITY"], source_ref=p["SOURCE_REF"], entered_by=ctx.actor.user_id,
                                             import_batch_id=batch.id))
    return {"late_rejected": late}


def _h_inventory(ctx, batch, st, accepted, rejected):
    _snapshots(ctx, batch, st, accepted)
    rows = [(ctx.actor.tenant_id, ctx.site.id, ctx.customer_id(p["CUSTOMER_CODE"]), p["CUSTOMER_CODE"], p["SNAPSHOT_DATE"], p["UNIT_TYPE"],
             p["QUANTITY"], p.get("RECEIVED_DATE"), batch.id) for _, _, p in accepted]
    copy_rows(ctx.s, "inventory_snapshot", ("tenant_id", "site_id", "customer_id", "customer_key", "snapshot_date", "unit_type", "quantity",
                                             "received_date", "import_batch_id"), rows)
    return {}


def _h_rate_card(ctx, batch, st, accepted, rejected):
    from ..services.contracts import draft_from_rows

    _snapshots(ctx, batch, st, accepted)
    late, versions = draft_from_rows(ctx.s, ctx.actor, ctx.site, accepted)
    return {"draft_versions": versions, "late_rejected": late,
            "next_step": "Submit each draft version for approval; a controller who is not the author approves it."}


HANDLERS = {
    "gini_ti102c": _h_ti102c, "gini_ti202": _h_ti202, "gini_order_header": _h_order_header, "storage_activity": _h_storage,
    "gini_customer_master": _h_customer_master, "gini_uom_conversion": _h_uom, "invoice_lines": _h_invoices,
    "employee_labor_cost": _h_labor_cost, "labor_time_attribution": _h_attribution, "vendor_invoices": _h_vendor,
    "expense_ledger": _h_ledger, "driver_quantities": _h_drivers, "inventory_snapshot": _h_inventory, "rate_card": _h_rate_card,
}

# PRICING_METHODS imported for the data contract documentation generator
__all__ = ["import_file", "read_table", "SOURCE_TYPES", "PRICING_METHODS"]
