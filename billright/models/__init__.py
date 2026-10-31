"""BillRight data model (section 6 of the build prompt).

Every business table carries ``tenant_id``. Tenant isolation is enforced by one mechanism:
PostgreSQL row-level security policies (see migrations) that compare ``tenant_id`` with the
``app.tenant_id`` session setting, which ``billright.db`` sets at the start of every transaction.
Site and customer scoping inside a tenant is applied by ``billright.security.Scope``.

Money: ``NUMERIC(18,6)`` for unit prices, quantities and unrounded amounts; ``NUMERIC(18,2)``
for amounts rounded once at the invoice-line level.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (JSON, BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer,
                        LargeBinary, Numeric, String, Text, UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

PRICE = Numeric(18, 6)
QTY = Numeric(18, 6)
MONEY = Numeric(18, 2)
JSONT = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


def _ts():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TenantMixin:
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"), index=True, nullable=False)


# ---------------------------------------------------------------- 6.1 reference and configuration
class Tenant(Base):
    __tablename__ = "tenant"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[dt.datetime] = _ts()


class Site(TenantMixin, Base):
    __tablename__ = "site"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(200))
    time_zone: Mapped[str] = mapped_column(String(64))
    currency_code: Mapped[str] = mapped_column(String(3))
    data_label: Mapped[str | None] = mapped_column(String(30))  # 'Synthetic' | 'Illustrative' | None (P10)
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)


class AppUser(TenantMixin, Base):
    __tablename__ = "app_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254))
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    oidc_subject: Mapped[str | None] = mapped_column(String(255))
    __table_args__ = (UniqueConstraint("tenant_id", "email"),)


class UserRole(TenantMixin, Base):
    __tablename__ = "user_role"
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(40), primary_key=True)


class UserScope(TenantMixin, Base):
    __tablename__ = "user_scope"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customer.id"))  # NULL = every customer of the site


class BillingPeriod(TenantMixin, Base):
    __tablename__ = "billing_period"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"), index=True)
    label: Mapped[str] = mapped_column(String(40))
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date] = mapped_column(Date)  # inclusive
    period_type: Mapped[str] = mapped_column(String(10))  # week | month
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | closing | closed
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    close_version: Mapped[int] = mapped_column(Integer, default=0)
    close_run_id: Mapped[int | None] = mapped_column(BigInteger)
    __table_args__ = (UniqueConstraint("site_id", "label"),)


class Setting(TenantMixin, Base):
    __tablename__ = "setting"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("site.id"))
    key: Mapped[str] = mapped_column(String(120))
    value: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    updated_at: Mapped[dt.datetime] = _ts()
    __table_args__ = (UniqueConstraint("tenant_id", "site_id", "key"),)


class ChargeCode(TenantMixin, Base):
    __tablename__ = "charge_code"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    default_uom: Mapped[str | None] = mapped_column(String(30))
    category: Mapped[str] = mapped_column(String(30))
    # Which activity events this charge prices (pick_line, order_completed, receipt, storage, hours, ...).
    # NULL for cost-based (cost_plus / pass_through) and fixed-fee charges.
    event_type: Mapped[str | None] = mapped_column(String(40))
    manual_capture: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)


class UomConversion(TenantMixin, Base):
    __tablename__ = "uom_conversion"
    id: Mapped[int] = mapped_column(primary_key=True)
    material_id: Mapped[str | None] = mapped_column(String(60), index=True)
    from_uom: Mapped[str] = mapped_column(String(30))
    to_uom: Mapped[str] = mapped_column(String(30))
    multiplier: Mapped[Decimal] = mapped_column(QTY)
    valid_from: Mapped[dt.date] = mapped_column(Date)
    valid_to: Mapped[dt.date | None] = mapped_column(Date)
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batch.id"))


# ---------------------------------------------------------------- 6.2 customers, contracts, rates
class Customer(TenantMixin, Base):
    __tablename__ = "customer"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"), index=True)
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    segment: Mapped[str | None] = mapped_column(String(80))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("tenant_id", "site_id", "code"),)


class CustomerIdentityMap(TenantMixin, Base):
    __tablename__ = "customer_identity_map"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_system: Mapped[str] = mapped_column(String(40))
    source_key_type: Mapped[str] = mapped_column(String(60))
    source_key_value: Mapped[str] = mapped_column(String(120))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    valid_from: Mapped[dt.date] = mapped_column(Date)
    valid_to: Mapped[dt.date | None] = mapped_column(Date)
    method_note: Mapped[str] = mapped_column(Text)
    __table_args__ = (Index("ix_cim_key", "tenant_id", "source_key_type", "source_key_value"),)


class Contract(TenantMixin, Base):
    __tablename__ = "contract"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"), index=True)
    reference: Mapped[str] = mapped_column(String(120))
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="active")
    document_ref: Mapped[str | None] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(Integer, default=1)


class RateCardVersion(TenantMixin, Base):
    __tablename__ = "rate_card_version"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contract.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    created_by: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[dt.datetime] = _ts()
    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    change_reason: Mapped[str | None] = mapped_column(Text)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    effective_from: Mapped[dt.date | None] = mapped_column(Date)
    superseded_from: Mapped[dt.date | None] = mapped_column(Date)
    __table_args__ = (UniqueConstraint("contract_id", "version_no"),)


class Rate(TenantMixin, Base):
    __tablename__ = "rate"
    id: Mapped[int] = mapped_column(primary_key=True)
    rate_card_version_id: Mapped[int] = mapped_column(ForeignKey("rate_card_version.id"), index=True)
    charge_code_id: Mapped[int] = mapped_column(ForeignKey("charge_code.id"))
    activity_code: Mapped[str | None] = mapped_column(String(60))
    pricing_method: Mapped[str] = mapped_column(String(20))
    unit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    uom: Mapped[str | None] = mapped_column(String(30))
    markup_pct: Mapped[Decimal | None] = mapped_column(PRICE)
    eligible_cost_categories: Mapped[list | None] = mapped_column(JSONT)
    minimum_charge: Mapped[Decimal | None] = mapped_column(MONEY)
    billing_period: Mapped[str] = mapped_column(String(10))
    tier_method: Mapped[str] = mapped_column(String(20), default="none")
    tiers: Mapped[list | None] = mapped_column(JSONT)
    condition: Mapped[dict | None] = mapped_column(JSONT)
    valid_from: Mapped[dt.date] = mapped_column(Date)
    valid_to: Mapped[dt.date | None] = mapped_column(Date)
    currency_code: Mapped[str] = mapped_column(String(3))
    source_ref: Mapped[str | None] = mapped_column(String(120))


# ---------------------------------------------------------------- imports and snapshots
class ImportBatch(TenantMixin, Base):
    __tablename__ = "import_batch"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    source_type: Mapped[str] = mapped_column(String(60))
    scope_key: Mapped[str] = mapped_column(String(60))
    file_name: Mapped[str] = mapped_column(String(255))
    file_hash: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    accepted_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20))  # accepted | rejected | superseded
    superseded_by: Mapped[int | None] = mapped_column(ForeignKey("import_batch.id"))
    report: Mapped[dict | None] = mapped_column(JSONT)
    data_label: Mapped[str | None] = mapped_column(String(30))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[dt.datetime] = _ts()
    __table_args__ = (Index("ix_batch_scope", "tenant_id", "site_id", "source_type", "scope_key"),)


class ImportRejectedRow(TenantMixin, Base):
    __tablename__ = "import_rejected_row"
    id: Mapped[int] = mapped_column(primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batch.id"), index=True)
    row_no: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONT)


class ColumnMapping(TenantMixin, Base):
    __tablename__ = "column_mapping"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(String(60))
    mapping: Mapped[dict] = mapped_column(JSONT)  # canonical field -> file column
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    updated_at: Mapped[dt.datetime] = _ts()
    __table_args__ = (UniqueConstraint("tenant_id", "source_type"),)


class SourceSnapshot(TenantMixin, Base):
    __tablename__ = "source_snapshot"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batch.id"), index=True)
    source_table: Mapped[str] = mapped_column(String(60))
    record_key: Mapped[str] = mapped_column(String(160))
    payload: Mapped[dict] = mapped_column(JSONT)
    hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (Index("ix_snapshot_key", "import_batch_id", "source_table", "record_key"),)


# ---------------------------------------------------------------- 6.3 events and billing transactions
class ActivityEvent(TenantMixin, Base):
    __tablename__ = "activity_event"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customer.id"))  # only when the source states it
    source_ref: Mapped[str] = mapped_column(String(160))
    event_type: Mapped[str] = mapped_column(String(40))
    charge_code_id: Mapped[int | None] = mapped_column(ForeignKey("charge_code.id"))
    service_date: Mapped[dt.date] = mapped_column(Date)
    quantity: Mapped[Decimal | None] = mapped_column(QTY)
    uom: Mapped[str | None] = mapped_column(String(30))
    material_id: Mapped[str | None] = mapped_column(String(60))
    order_ref: Mapped[str | None] = mapped_column(String(80))
    task_ref: Mapped[str | None] = mapped_column(String(80))
    shipment_ref: Mapped[str | None] = mapped_column(String(80))
    attributes: Mapped[dict | None] = mapped_column(JSONT)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batch.id"), index=True)
    snapshot_id: Mapped[int | None] = mapped_column(BigInteger)
    __table_args__ = (Index("ix_event_site_date", "site_id", "service_date"),)


class ManualBillableEntry(TenantMixin, Base):
    __tablename__ = "manual_billable_entry"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    charge_code_id: Mapped[int] = mapped_column(ForeignKey("charge_code.id"))
    quantity: Mapped[Decimal] = mapped_column(QTY)
    uom: Mapped[str] = mapped_column(String(30))
    service_date: Mapped[dt.date] = mapped_column(Date)
    work_start: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    work_end: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str] = mapped_column(Text)
    photo_ref: Mapped[str | None] = mapped_column(String(255))
    work_order_ref: Mapped[str | None] = mapped_column(String(80))
    entered_by: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    entered_at: Mapped[dt.datetime] = _ts()
    status: Mapped[str] = mapped_column(String(20), default="submitted")
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(Text)


class CalculationRun(TenantMixin, Base):
    __tablename__ = "calculation_run"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    period_id: Mapped[int] = mapped_column(ForeignKey("billing_period.id"), index=True)
    run_type: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[dt.datetime] = _ts()
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20))  # running | succeeded | failed | blocked
    progress: Mapped[str | None] = mapped_column(String(200))
    message: Mapped[str | None] = mapped_column(Text)
    input_batch_ids: Mapped[list | None] = mapped_column(JSONT)
    settings_snapshot: Mapped[dict | None] = mapped_column(JSONT)
    code_version: Mapped[str] = mapped_column(String(40))
    result_hash: Mapped[str | None] = mapped_column(String(64))
    counts: Mapped[dict | None] = mapped_column(JSONT)
    triggered_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))


class RatedEvent(TenantMixin, Base):
    __tablename__ = "rated_event"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("calculation_run.id"), index=True)
    activity_event_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    manual_entry_id: Mapped[int | None] = mapped_column(ForeignKey("manual_billable_entry.id"))
    expense_line_id: Mapped[int | None] = mapped_column(BigInteger)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    charge_code_id: Mapped[int] = mapped_column(ForeignKey("charge_code.id"))
    rate_id: Mapped[int] = mapped_column(ForeignKey("rate.id"))
    rate_card_version_id: Mapped[int] = mapped_column(ForeignKey("rate_card_version.id"))
    quantity_in_rate_uom: Mapped[Decimal] = mapped_column(QTY)
    unit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    amount_unrounded: Mapped[Decimal] = mapped_column(PRICE)
    service_date: Mapped[dt.date] = mapped_column(Date)
    period_id: Mapped[int] = mapped_column(ForeignKey("billing_period.id"))
    __table_args__ = (Index("ix_rated_key", "run_id", "customer_id", "charge_code_id"),)


class CannotRateItem(TenantMixin, Base):
    __tablename__ = "cannot_rate_item"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("calculation_run.id"), index=True)
    activity_event_id: Mapped[int | None] = mapped_column(BigInteger)
    source_ref: Mapped[str] = mapped_column(String(160))
    reason: Mapped[str] = mapped_column(String(30))
    details: Mapped[dict | None] = mapped_column(JSONT)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customer.id"))
    charge_code_id: Mapped[int | None] = mapped_column(ForeignKey("charge_code.id"))
    estimated_quantity: Mapped[Decimal | None] = mapped_column(QTY)


# ---------------------------------------------------------------- 6.4 invoices
class InvoiceHeader(TenantMixin, Base):
    __tablename__ = "invoice_header"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    invoice_number: Mapped[str] = mapped_column(String(80))
    invoice_date: Mapped[dt.date | None] = mapped_column(Date)
    period_id: Mapped[int] = mapped_column(ForeignKey("billing_period.id"), index=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batch.id"), index=True)


class InvoiceLine(TenantMixin, Base):
    __tablename__ = "invoice_line"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_header_id: Mapped[int] = mapped_column(ForeignKey("invoice_header.id"), index=True)
    line_number: Mapped[int] = mapped_column(Integer)
    charge_code_id: Mapped[int | None] = mapped_column(ForeignKey("charge_code.id"))
    raw_charge_code: Mapped[str] = mapped_column(String(60))
    quantity: Mapped[Decimal | None] = mapped_column(QTY)
    uom: Mapped[str | None] = mapped_column(String(30))
    unit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    posted_on: Mapped[dt.date | None] = mapped_column(Date)
    adjustment_ref: Mapped[str | None] = mapped_column(String(40))  # leakage item id an adjustment line settles


# ---------------------------------------------------------------- 6.5 leakage and decisions
class BillableLine(TenantMixin, Base):
    __tablename__ = "billable_line"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("calculation_run.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    charge_code_id: Mapped[int] = mapped_column(ForeignKey("charge_code.id"))
    period_id: Mapped[int] = mapped_column(ForeignKey("billing_period.id"))
    billable_quantity: Mapped[Decimal] = mapped_column(QTY)
    billable_amount: Mapped[Decimal] = mapped_column(MONEY)
    unrounded_amount: Mapped[Decimal] = mapped_column(PRICE)
    minimum_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    tier_applied: Mapped[str | None] = mapped_column(String(80))
    unit_prices: Mapped[list | None] = mapped_column(JSONT)
    rate_ids: Mapped[list | None] = mapped_column(JSONT)


class ReconLine(TenantMixin, Base):
    """Billable vs billed per customer x charge code x period for one run (the reconciliation matrix)."""
    __tablename__ = "recon_line"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("calculation_run.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    charge_code_id: Mapped[int] = mapped_column(ForeignKey("charge_code.id"))
    period_id: Mapped[int] = mapped_column(ForeignKey("billing_period.id"))
    billable_quantity: Mapped[Decimal] = mapped_column(QTY)
    billed_quantity: Mapped[Decimal] = mapped_column(QTY)
    billable_amount: Mapped[Decimal] = mapped_column(MONEY)
    billed_amount: Mapped[Decimal] = mapped_column(MONEY)
    gap_amount: Mapped[Decimal] = mapped_column(MONEY)
    status: Mapped[str] = mapped_column(String(20))  # reconciled | gap | rounding | cannot_rate
    rules_matched: Mapped[list | None] = mapped_column(JSONT)
    note: Mapped[str | None] = mapped_column(Text)


class LeakageItem(TenantMixin, Base):
    __tablename__ = "leakage_item"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("calculation_run.id"))  # latest run that saw the item
    first_run_id: Mapped[int] = mapped_column(ForeignKey("calculation_run.id"))
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"), index=True)
    charge_code_id: Mapped[int] = mapped_column(ForeignKey("charge_code.id"))
    period_id: Mapped[int] = mapped_column(ForeignKey("billing_period.id"), index=True)
    rule_code: Mapped[str] = mapped_column(String(3))
    also_matches: Mapped[list | None] = mapped_column(JSONT)
    billable_quantity: Mapped[Decimal] = mapped_column(QTY)
    billed_quantity: Mapped[Decimal] = mapped_column(QTY)
    billable_amount: Mapped[Decimal] = mapped_column(MONEY)
    billed_amount: Mapped[Decimal] = mapped_column(MONEY)
    gap_amount: Mapped[Decimal] = mapped_column(MONEY)
    direction: Mapped[str] = mapped_column(String(5))
    contract_unit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    billed_unit_prices: Mapped[list | None] = mapped_column(JSONT)
    status: Mapped[str] = mapped_column(String(20), default="new")
    decision: Mapped[str | None] = mapped_column(String(30))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    carried_from_item_id: Mapped[int | None] = mapped_column(BigInteger)
    resolved_by_run_id: Mapped[int | None] = mapped_column(BigInteger)
    invoiced_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    invoiced_on: Mapped[dt.date | None] = mapped_column(Date)
    alerted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))  # G3
    created_at: Mapped[dt.datetime] = _ts()
    __table_args__ = (UniqueConstraint("customer_id", "charge_code_id", "period_id", "direction"),)


class LeakageEvidence(TenantMixin, Base):
    __tablename__ = "leakage_evidence"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    leakage_item_id: Mapped[int] = mapped_column(ForeignKey("leakage_item.id"), index=True)
    run_id: Mapped[int] = mapped_column(BigInteger)
    rated_event_id: Mapped[int | None] = mapped_column(BigInteger)
    invoice_line_id: Mapped[int | None] = mapped_column(ForeignKey("invoice_line.id"))
    activity_event_id: Mapped[int | None] = mapped_column(BigInteger)


class LeakageNote(TenantMixin, Base):
    __tablename__ = "leakage_note"
    id: Mapped[int] = mapped_column(primary_key=True)
    leakage_item_id: Mapped[int] = mapped_column(ForeignKey("leakage_item.id"), index=True)
    author: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[dt.datetime] = _ts()
    text: Mapped[str] = mapped_column(Text)
    attachment_ref: Mapped[str | None] = mapped_column(String(255))


# ---------------------------------------------------------------- 6.6 expenses
class Vendor(TenantMixin, Base):
    __tablename__ = "vendor"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    vendor_type: Mapped[str] = mapped_column(String(30))
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)


class VendorInvoiceHeader(TenantMixin, Base):
    __tablename__ = "vendor_invoice_header"
    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendor.id"))
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    invoice_number: Mapped[str] = mapped_column(String(80))
    invoice_date: Mapped[dt.date | None] = mapped_column(Date)
    period_id: Mapped[int] = mapped_column(ForeignKey("billing_period.id"), index=True)
    po_ref: Mapped[str | None] = mapped_column(String(80))
    approved: Mapped[bool] = mapped_column(Boolean, default=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batch.id"))


class VendorInvoiceLine(TenantMixin, Base):
    __tablename__ = "vendor_invoice_line"
    id: Mapped[int] = mapped_column(primary_key=True)
    header_id: Mapped[int] = mapped_column(ForeignKey("vendor_invoice_header.id"), index=True)
    service: Mapped[str] = mapped_column(String(200))
    expense_category: Mapped[str] = mapped_column(String(40))
    expense_account: Mapped[str | None] = mapped_column(String(40))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customer.id"))
    shipment_ref: Mapped[str | None] = mapped_column(String(80))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    customer_pays_carrier: Mapped[bool] = mapped_column(Boolean, default=False)


class EmployeeLaborCost(TenantMixin, Base):
    __tablename__ = "employee_labor_cost"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    period_id: Mapped[int] = mapped_column(ForeignKey("billing_period.id"), index=True)
    worker_id: Mapped[str] = mapped_column(String(40))
    employer_type: Mapped[str] = mapped_column(String(10))
    role: Mapped[str] = mapped_column(String(20))
    paid_hours: Mapped[Decimal] = mapped_column(QTY)
    loaded_cost: Mapped[Decimal] = mapped_column(MONEY)
    overtime_hours: Mapped[Decimal | None] = mapped_column(QTY)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batch.id"))


class LaborTimeAttribution(TenantMixin, Base):
    __tablename__ = "labor_time_attribution"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    period_id: Mapped[int] = mapped_column(ForeignKey("billing_period.id"), index=True)
    worker_id: Mapped[str] = mapped_column(String(40))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customer.id"))
    hours: Mapped[Decimal] = mapped_column(QTY)
    basis: Mapped[str] = mapped_column(String(20))
    unassigned_reason: Mapped[str | None] = mapped_column(String(60))
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batch.id"))


class ExpenseLine(TenantMixin, Base):
    __tablename__ = "expense_line"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    period_id: Mapped[int] = mapped_column(ForeignKey("billing_period.id"), index=True)
    expense_category: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(String(255))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    source_type: Mapped[str] = mapped_column(String(20))
    source_ref: Mapped[str] = mapped_column(String(120))
    payment_recipient_type: Mapped[str] = mapped_column(String(40))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customer.id"))  # direct cost when set
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batch.id"))
    entered_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))


class CentralOverhead(TenantMixin, Base):
    __tablename__ = "central_overhead"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    period_id: Mapped[int] = mapped_column(ForeignKey("billing_period.id"))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    note: Mapped[str | None] = mapped_column(Text)
    entered_by: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    entered_at: Mapped[dt.datetime] = _ts()


# ---------------------------------------------------------------- 6.7 allocation
class AllocationDriver(TenantMixin, Base):
    __tablename__ = "allocation_driver"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    expense_category: Mapped[str] = mapped_column(String(40))
    driver_type: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    valid_from: Mapped[dt.date] = mapped_column(Date)
    valid_to: Mapped[dt.date | None] = mapped_column(Date)
    is_proxy: Mapped[bool] = mapped_column(Boolean, default=False)
    data_label: Mapped[str | None] = mapped_column(String(30))


class AllocationDriverQuantity(TenantMixin, Base):
    __tablename__ = "allocation_driver_quantity"
    id: Mapped[int] = mapped_column(primary_key=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey("allocation_driver.id"), index=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("billing_period.id"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customer.id"))  # NULL = unassigned quantity
    quantity: Mapped[Decimal] = mapped_column(QTY)
    source_ref: Mapped[str] = mapped_column(String(160))
    entered_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batch.id"))


class CostAllocation(TenantMixin, Base):
    __tablename__ = "cost_allocation"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("calculation_run.id"), index=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("billing_period.id"))
    expense_category: Mapped[str] = mapped_column(String(40))
    expense_line_id: Mapped[int | None] = mapped_column(BigInteger)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customer.id"))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    method: Mapped[str] = mapped_column(String(10))  # direct | driver | none
    driver_id: Mapped[int | None] = mapped_column(ForeignKey("allocation_driver.id"))
    share: Mapped[Decimal | None] = mapped_column(Numeric(9, 8))
    exception: Mapped[str | None] = mapped_column(String(60))


# ---------------------------------------------------------------- 6.8 results
class CustomerProfitability(TenantMixin, Base):
    __tablename__ = "customer_profitability"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("calculation_run.id"), index=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("billing_period.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    revenue_billed: Mapped[Decimal] = mapped_column(MONEY)
    direct_cost: Mapped[Decimal] = mapped_column(MONEY)
    allocated_shared_cost: Mapped[Decimal] = mapped_column(MONEY)
    total_cost: Mapped[Decimal] = mapped_column(MONEY)
    site_profit: Mapped[Decimal] = mapped_column(MONEY)
    margin_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    orders_completed: Mapped[int | None] = mapped_column(Integer)
    cost_per_order: Mapped[Decimal | None] = mapped_column(PRICE)
    confirmed_not_invoiced: Mapped[Decimal] = mapped_column(MONEY, default=0)


class SiteSummary(TenantMixin, Base):
    __tablename__ = "site_summary"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("calculation_run.id"), index=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("billing_period.id"))
    revenue: Mapped[Decimal] = mapped_column(MONEY)
    total_cost: Mapped[Decimal] = mapped_column(MONEY)
    allocated_cost: Mapped[Decimal] = mapped_column(MONEY)
    unassigned_cost: Mapped[Decimal] = mapped_column(MONEY)
    site_profit: Mapped[Decimal] = mapped_column(MONEY)
    margin_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    central_overhead_unallocated: Mapped[Decimal | None] = mapped_column(MONEY)
    profit_before_financing_tax: Mapped[Decimal | None] = mapped_column(MONEY)


# ---------------------------------------------------------------- period close
class CloseAcknowledgement(TenantMixin, Base):
    __tablename__ = "close_acknowledgement"
    id: Mapped[int] = mapped_column(primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("billing_period.id"), index=True)
    check_no: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    acknowledged_by: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    acknowledged_at: Mapped[dt.datetime] = _ts()
    close_version: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------- GA support tables
class InventorySnapshot(TenantMixin, Base):
    """G1: one row per customer, storage unit type and day (daily inventory snapshot)."""
    __tablename__ = "inventory_snapshot"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customer.id"))
    customer_key: Mapped[str | None] = mapped_column(String(80))
    snapshot_date: Mapped[dt.date] = mapped_column(Date)
    unit_type: Mapped[str] = mapped_column(String(20))  # pallet | bin | cubic_foot | square_foot
    quantity: Mapped[Decimal] = mapped_column(QTY)
    received_date: Mapped[dt.date | None] = mapped_column(Date)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batch.id"))


class SurchargeRule(TenantMixin, Base):
    """G4: rule that adds a surcharge event when an activity event meets a condition."""
    __tablename__ = "surcharge_rule"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20))  # peak_season | rush | after_hours | weekend | cold_chain | fuel
    applies_to_event_type: Mapped[str] = mapped_column(String(40))
    surcharge_charge_code_id: Mapped[int] = mapped_column(ForeignKey("charge_code.id"))
    parameters: Mapped[dict] = mapped_column(JSONT)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))


class SlaRule(TenantMixin, Base):
    """G7: contractual SLA penalty terms."""
    __tablename__ = "sla_rule"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"))
    milestone: Mapped[str] = mapped_column(String(60))
    allowed_late_seconds: Mapped[int] = mapped_column(Integer)
    penalty_amount: Mapped[Decimal] = mapped_column(MONEY)
    penalty_basis: Mapped[str] = mapped_column(String(20))
    cap_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    penalty_charge_code: Mapped[str | None] = mapped_column(String(60))
    valid_from: Mapped[dt.date] = mapped_column(Date)
    valid_to: Mapped[dt.date | None] = mapped_column(Date)
    contract_reference: Mapped[str | None] = mapped_column(String(120))


class Alert(TenantMixin, Base):
    """G3/G6 notifications shown in the app (and optionally e-mailed)."""
    __tablename__ = "alert"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    kind: Mapped[str] = mapped_column(String(30))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    message: Mapped[str] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(String(255))
    entity: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = _ts()
    read_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class AccountingExport(TenantMixin, Base):
    """G8: one row per push of approved adjustments to an accounting adapter."""
    __tablename__ = "accounting_export"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("site.id"))
    period_id: Mapped[int] = mapped_column(ForeignKey("billing_period.id"))
    adapter: Mapped[str] = mapped_column(String(40))
    item_ids: Mapped[list] = mapped_column(JSONT)
    file_name: Mapped[str | None] = mapped_column(String(255))
    content: Mapped[bytes | None] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(20))
    created_by: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[dt.datetime] = _ts()


# ---------------------------------------------------------------- 6.9 audit
class AuditLog(TenantMixin, Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[dt.datetime] = _ts()
    user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"))
    action: Mapped[str] = mapped_column(String(60))
    entity: Mapped[str] = mapped_column(String(60))
    entity_id: Mapped[str | None] = mapped_column(String(60))
    before: Mapped[dict | None] = mapped_column(JSONT)
    after: Mapped[dict | None] = mapped_column(JSONT)
    reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (Index("ix_audit_entity", "entity", "entity_id"),)


TENANT_TABLES = [t.name for t in Base.metadata.sorted_tables if "tenant_id" in t.c]
