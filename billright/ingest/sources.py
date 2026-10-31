"""Source definitions for the import centre (5.1, docs/DATA_CONTRACT.md).

Each source type lists its canonical fields (the names a column mapping maps file columns to), which are
required, their types, the record key, and whether the source is period-scoped. Validation in
``billright.ingest.importer`` is driven entirely by these definitions.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class F:
    name: str
    type: str = "str"  # str | int | decimal | date | datetime | bool | json
    required: bool = False
    non_negative: bool = False
    description: str = ""


@dataclass(frozen=True)
class SourceType:
    code: str
    label: str
    source_table: str
    fields: tuple
    key: tuple
    period_field: str | None = None  # period label column (period-scoped sources)
    date_field: str | None = None  # service timestamp/date column (event sources)
    customer_field: str | None = None  # customer code column validated against the identity map
    charge_field: str | None = None
    description: str = ""
    events: str | None = None  # event type created from each row, if any
    group: str = "gini"
    notes: tuple = field(default_factory=tuple)


S = {}


def _add(st: SourceType):
    S[st.code] = st


_add(SourceType("gini_customer_master", "GiNi customer master (M140 family)", "M140",
    (F("CUSTOMER_CODE", required=True), F("CUSTOMER_NAME", required=True), F("SEGMENT"), F("ACTIVE", "bool"),
     F("SOURCE_SYSTEM")), ("CUSTOMER_CODE",),
    description="Bill-to customer / inventory owner master. Creates or updates customers and their identity-map key."))
_add(SourceType("gini_ti102c", "GiNi TI102C completed picking", "TI102C",
    (F("TI102ID", required=True), F("TI102X3ID", required=True, description="warehouse order"), F("CLIENTID"), F("WHID"),
     F("WORKTYPE", required=True), F("MATERIALID"), F("SRCLOC"), F("RESOURCENAME"), F("WORKER_ID"),
     F("PROCESSEDQTY", "decimal", required=True, non_negative=True), F("UOM"), F("CREATEDATETIME", "datetime"),
     F("COMPLETED_AT", "datetime", required=True)), ("TI102ID",), date_field="COMPLETED_AT", events="pick_line",
    description="One row per completed pick task. Each row is one billable pick line (unit 'line').",
    notes=("CLIENTID identifies the GiNi tenant, not the 3PL customer; it is stored but never used for billing.",)))
_add(SourceType("gini_ti102", "GiNi TI102 open / assigned picking", "TI102",
    (F("TI102ID", required=True), F("TI102X3ID"), F("CLIENTID"), F("WORKTYPE"), F("MATERIALID"), F("RESOURCENAME"),
     F("ORDERQTY", "decimal", non_negative=True), F("OPENQTY", "decimal", non_negative=True), F("CREATEDATETIME", "datetime")),
    ("TI102ID",), description="Open work: evidence and workload only; never billed."))
_add(SourceType("gini_ti202", "GiNi TI202 receiving", "TI202",
    (F("TI202ID", required=True), F("RECEIPT_REF"), F("OWNER_CODE", required=True), F("MATERIALID"),
     F("QUANTITY", "decimal", required=True, non_negative=True), F("UOM", required=True), F("RECEIVED_AT", "datetime", required=True)),
    ("TI202ID",), date_field="RECEIVED_AT", events="receipt"))
_add(SourceType("gini_order_header", "GiNi order headers", "ORDER_HEADER",
    (F("ORDER_ID", required=True), F("ORDER_CODE"), F("CUSTOMER_CODE", required=True), F("ORDER_TYPE"), F("RELEASED_AT", "datetime"),
     F("COMPLETED_AT", "datetime"), F("CUTOFF_AT", "datetime"), F("LINE_COUNT", "int", non_negative=True), F("STATUS")),
    ("ORDER_ID",), date_field="COMPLETED_AT", events="order_completed",
    description="Orders. A completed order (COMPLETED_AT set) is one 'order_completed' event (unit 'order')."))
_add(SourceType("gini_order_line", "GiNi order lines", "ORDER_LINE",
    (F("ORDER_LINE_ID", required=True), F("ORDER_ID", required=True), F("LINE_NUMBER", "int"), F("MATERIAL_ID"),
     F("ORDERED_QTY", "decimal", non_negative=True), F("UOM")), ("ORDER_LINE_ID",)))
_add(SourceType("gini_task_order_line", "GiNi task to order-line links", "TASK_ORDER_LINE",
    (F("TASK_ID", required=True), F("ORDER_LINE_ID", required=True), F("ALLOCATED_QTY", "decimal", non_negative=True)),
    ("TASK_ID", "ORDER_LINE_ID")))
_add(SourceType("gini_material", "GiNi materials", "MATERIAL",
    (F("MATERIAL_ID", required=True), F("BASE_UOM"), F("TEMPERATURE_CLASS"), F("HANDLING_CLASS"), F("WEIGHT_KG", "decimal"),
     F("VOLUME_M3", "decimal")), ("MATERIAL_ID",)))
_add(SourceType("gini_uom_conversion", "GiNi unit-of-measure conversions", "UOM_CONVERSION",
    (F("MATERIAL_ID"), F("FROM_UOM", required=True), F("TO_UOM", required=True), F("MULTIPLIER", "decimal", required=True, non_negative=True),
     F("VALID_FROM", "date", required=True), F("VALID_TO", "date")), ("MATERIAL_ID", "FROM_UOM", "TO_UOM", "VALID_FROM")))
_add(SourceType("gini_shipment", "GiNi / TMS shipments", "SHIPMENT",
    (F("SHIPMENT_ID", required=True), F("CUSTOMER_CODE", required=True), F("CARRIER"), F("SCHEDULED_DEPARTURE_AT", "datetime"),
     F("CUTOFF_AT", "datetime"), F("DISPATCHED_AT", "datetime"), F("ORDER_COUNT", "int", non_negative=True)), ("SHIPMENT_ID",)))
_add(SourceType("gini_shipment_order", "GiNi shipment to order links", "SHIPMENT_ORDER",
    (F("SHIPMENT_ID", required=True), F("ORDER_ID", required=True), F("STAGED_AT", "datetime"), F("ORDER_CUTOFF_AT", "datetime"),
     F("LATE_SECONDS", "decimal")), ("SHIPMENT_ID", "ORDER_ID")))
_add(SourceType("labor_order_time", "Labor minutes per warehouse order (GiNi Labor Economics)", "LABOR_ORDER_TIME",
    (F("WORKER_ID", required=True), F("WORK_DATE", "date", required=True), F("ACTIVITY"), F("TI102X3ID", required=True),
     F("SECONDS", "decimal", required=True, non_negative=True), F("LINES", "int", non_negative=True)),
    ("WORKER_ID", "WORK_DATE", "ACTIVITY", "TI102X3ID"), description="Evidence only: labor minutes shown in drill-downs."))
_add(SourceType("inventory_snapshot", "Daily inventory snapshots (storage)", "INVENTORY_SNAPSHOT",
    (F("SNAPSHOT_DATE", "date", required=True), F("CUSTOMER_CODE", required=True), F("UNIT_TYPE", required=True),
     F("QUANTITY", "decimal", required=True, non_negative=True), F("RECEIVED_DATE", "date"), F("LOCATION")),
    ("SNAPSHOT_DATE", "CUSTOMER_CODE", "UNIT_TYPE", "LOCATION", "RECEIVED_DATE"), customer_field="CUSTOMER_CODE", group="storage"))
_add(SourceType("storage_activity", "Storage billing quantities", "STORAGE_ACTIVITY",
    (F("STORAGE_REF", required=True), F("CUSTOMER_CODE", required=True), F("SERVICE_DATE", "date", required=True),
     F("QUANTITY", "decimal", required=True, non_negative=True), F("UOM", required=True)), ("STORAGE_REF",),
    date_field="SERVICE_DATE", customer_field="CUSTOMER_CODE", events="storage", group="storage",
    description="Contract storage units already measured (e.g. pallet-months). G1 derives these from daily snapshots."))
_add(SourceType("invoice_lines", "Customer invoice lines", "INVOICE_LINE",
    (F("INVOICE_NUMBER", required=True), F("LINE_NUMBER", "int", required=True), F("CUSTOMER_CODE", required=True),
     F("CHARGE_CODE", required=True), F("PERIOD", required=True), F("QUANTITY", "decimal"), F("UOM"), F("UNIT_PRICE", "decimal"),
     F("AMOUNT", "decimal", required=True), F("POSTED_ON", "date"), F("INVOICE_DATE", "date"), F("ADJUSTMENT_REF")),
    ("INVOICE_NUMBER", "LINE_NUMBER"), period_field="PERIOD", customer_field="CUSTOMER_CODE", charge_field="CHARGE_CODE",
    group="billing", description="What was actually invoiced. ADJUSTMENT_REF (e.g. LKG-42) marks a line that settles a leakage item."))
_add(SourceType("employee_labor_cost", "Employee labor cost (EMPLOYEE_LABOR_COST)", "EMPLOYEE_LABOR_COST",
    (F("WORKER_ID", required=True), F("EMPLOYER_TYPE", required=True), F("ROLE", required=True), F("PERIOD", required=True),
     F("PAID_HOURS", "decimal", required=True, non_negative=True), F("LOADED_COST", "decimal", required=True, non_negative=True),
     F("OVERTIME_HOURS", "decimal", non_negative=True)), ("WORKER_ID", "PERIOD"), period_field="PERIOD", group="cost"))
_add(SourceType("labor_time_attribution", "Labor time attribution", "LABOR_TIME_ATTRIBUTION",
    (F("WORKER_ID", required=True), F("PERIOD", required=True), F("CUSTOMER_CODE"), F("HOURS", "decimal", required=True, non_negative=True),
     F("BASIS", required=True), F("UNASSIGNED_REASON")), ("WORKER_ID", "PERIOD", "CUSTOMER_CODE", "UNASSIGNED_REASON"),
    period_field="PERIOD", customer_field="CUSTOMER_CODE", group="cost",
    description="Blank CUSTOMER_CODE = unassigned time (training, shared work, idle, meetings); UNASSIGNED_REASON required then."))
_add(SourceType("vendor_invoices", "Vendor and agency invoices (VENDOR_INVOICE_HEADER / LINES)", "VENDOR_INVOICE_LINE",
    (F("VENDOR", required=True), F("VENDOR_TYPE", required=True), F("INVOICE_NUMBER", required=True), F("LINE_NUMBER", "int", required=True),
     F("INVOICE_DATE", "date"), F("PERIOD", required=True), F("PO_REF"), F("SERVICE", required=True),
     F("EXPENSE_CATEGORY", required=True), F("EXPENSE_ACCOUNT"), F("CUSTOMER_CODE"), F("SHIPMENT_REF"),
     F("AMOUNT", "decimal", required=True), F("CUSTOMER_PAYS_CARRIER", "bool")),
    ("VENDOR", "INVOICE_NUMBER", "LINE_NUMBER"), period_field="PERIOD", customer_field="CUSTOMER_CODE", group="cost"))
_add(SourceType("expense_ledger", "Expense ledger lines (payroll journal, facility, manual)", "EXPENSE_LEDGER",
    (F("REF", required=True), F("PERIOD", required=True), F("EXPENSE_CATEGORY", required=True), F("DESCRIPTION", required=True),
     F("AMOUNT", "decimal", required=True), F("SOURCE_TYPE", required=True), F("PAYMENT_RECIPIENT_TYPE", required=True),
     F("CUSTOMER_CODE")), ("REF",), period_field="PERIOD", customer_field="CUSTOMER_CODE", group="cost"))
_add(SourceType("driver_quantities", "Allocation driver quantities", "DRIVER_QUANTITY",
    (F("EXPENSE_CATEGORY", required=True), F("PERIOD", required=True), F("CUSTOMER_CODE"), F("QUANTITY", "decimal", required=True, non_negative=True),
     F("SOURCE_REF", required=True)), ("EXPENSE_CATEGORY", "PERIOD", "CUSTOMER_CODE"), period_field="PERIOD",
    customer_field="CUSTOMER_CODE", group="cost", description="Blank CUSTOMER_CODE = unassigned driver quantity (idle/shared)."))
_add(SourceType("rate_card", "Rate card (loads a draft version for approval)", "RATE_CARD",
    (F("CUSTOMER_CODE", required=True), F("CONTRACT_REFERENCE", required=True), F("CHARGE_CODE", required=True), F("ACTIVITY_CODE"),
     F("PRICING_METHOD", required=True), F("UNIT_PRICE", "decimal", non_negative=True), F("UOM"), F("MARKUP_PCT", "decimal"),
     F("ELIGIBLE_COST_CATEGORIES"), F("MINIMUM_CHARGE", "decimal", non_negative=True), F("BILLING_PERIOD", required=True),
     F("TIER_METHOD"), F("TIERS", "json"), F("CONDITION", "json"), F("VALID_FROM", "date", required=True), F("VALID_TO", "date"),
     F("CURRENCY_CODE", required=True), F("SOURCE_REF")), ("CUSTOMER_CODE", "CHARGE_CODE", "VALID_FROM", "CONDITION"),
    customer_field="CUSTOMER_CODE", charge_field="CHARGE_CODE", group="contracts"))

SOURCE_TYPES = S
