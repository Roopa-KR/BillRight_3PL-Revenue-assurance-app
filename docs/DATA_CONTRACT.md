# Data contract

Generated from `billright/ingest/sources.py` (the definitions the importer validates against) by `scripts/gen_data_contract.py`.

## Rules for every source

* Formats: CSV (UTF-8, header row), CSV.GZ or XLSX (first sheet). Column names match the canonical names below; a saved column mapping per source can rename file columns.
* Every file becomes an immutable import batch (SHA-256 file hash, row count, user, time, source type). An identical file for the same source and site is a no-op.
* A changed file with the same *scope* supersedes the earlier batch; the old rows stay stored. Default scope: the periods in the file (period-scoped sources) or the file name stem.
* Validation per row: required fields, types (decimal, integer, ISO date, ISO 8601 date-time, true/false, JSON), duplicate keys in the file, unknown customer codes (customer register / identity map), unknown charge codes, unknown or closed periods, negative quantities. Rejected rows are listed with their reason and can be downloaded.
* Event sources create billable events only for rows whose service date lies in an open billing period; other rows are kept as reference snapshots and counted in the report.
* Timestamps without an offset are read in the site time zone; service dates are local site dates.
* **CLIENTID** in GiNi extracts identifies the GiNi tenant (e.g. BSP / SYN). It is stored but never used to identify the 3PL customer. Customers are resolved through the join path in setting `customer_resolution.join_path` (open decision 10: verify it against the deployed GiNi schema).

## GiNi customer master (M140 family)

Source type `gini_customer_master` · stored as `M140` snapshots · refresh: Scheduled read-only extract from GiNi (MySQL), daily before the G3 run; or upload

Bill-to customer / inventory owner master. Creates or updates customers and their identity-map key.

Record key: `CUSTOMER_CODE`.

| Field | Type | Required | Validation |
|---|---|---|---|
| `CUSTOMER_CODE` | str | yes |   |
| `CUSTOMER_NAME` | str | yes |   |
| `SEGMENT` | str |  |   |
| `ACTIVE` | bool |  |   |
| `SOURCE_SYSTEM` | str |  |   |

## GiNi TI102C completed picking

Source type `gini_ti102c` · stored as `TI102C` snapshots · refresh: Scheduled read-only extract from GiNi (MySQL), daily before the G3 run; or upload

One row per completed pick task. Each row is one billable pick line (unit 'line').

> CLIENTID identifies the GiNi tenant, not the 3PL customer; it is stored but never used for billing.

Record key: `TI102ID`; creates `pick_line` events dated by `COMPLETED_AT`.

| Field | Type | Required | Validation |
|---|---|---|---|
| `TI102ID` | str | yes |   |
| `TI102X3ID` | str | yes |  warehouse order |
| `CLIENTID` | str |  |   |
| `WHID` | str |  |   |
| `WORKTYPE` | str | yes |   |
| `MATERIALID` | str |  |   |
| `SRCLOC` | str |  |   |
| `RESOURCENAME` | str |  |   |
| `WORKER_ID` | str |  |   |
| `PROCESSEDQTY` | decimal | yes | not negative  |
| `UOM` | str |  |   |
| `CREATEDATETIME` | datetime |  |   |
| `COMPLETED_AT` | datetime | yes |   |

## GiNi TI102 open / assigned picking

Source type `gini_ti102` · stored as `TI102` snapshots · refresh: Scheduled read-only extract from GiNi (MySQL), daily before the G3 run; or upload

Open work: evidence and workload only; never billed.

Record key: `TI102ID`.

| Field | Type | Required | Validation |
|---|---|---|---|
| `TI102ID` | str | yes |   |
| `TI102X3ID` | str |  |   |
| `CLIENTID` | str |  |   |
| `WORKTYPE` | str |  |   |
| `MATERIALID` | str |  |   |
| `RESOURCENAME` | str |  |   |
| `ORDERQTY` | decimal |  | not negative  |
| `OPENQTY` | decimal |  | not negative  |
| `CREATEDATETIME` | datetime |  |   |

## GiNi TI202 receiving

Source type `gini_ti202` · stored as `TI202` snapshots · refresh: Scheduled read-only extract from GiNi (MySQL), daily before the G3 run; or upload

Record key: `TI202ID`; creates `receipt` events dated by `RECEIVED_AT`.

| Field | Type | Required | Validation |
|---|---|---|---|
| `TI202ID` | str | yes |   |
| `RECEIPT_REF` | str |  |   |
| `OWNER_CODE` | str | yes |   |
| `MATERIALID` | str |  |   |
| `QUANTITY` | decimal | yes | not negative  |
| `UOM` | str | yes |   |
| `RECEIVED_AT` | datetime | yes |   |

## GiNi order headers

Source type `gini_order_header` · stored as `ORDER_HEADER` snapshots · refresh: Scheduled read-only extract from GiNi (MySQL), daily before the G3 run; or upload

Orders. A completed order (COMPLETED_AT set) is one 'order_completed' event (unit 'order').

Record key: `ORDER_ID`; creates `order_completed` events dated by `COMPLETED_AT`.

| Field | Type | Required | Validation |
|---|---|---|---|
| `ORDER_ID` | str | yes |   |
| `ORDER_CODE` | str |  |   |
| `CUSTOMER_CODE` | str | yes |   |
| `ORDER_TYPE` | str |  |   |
| `RELEASED_AT` | datetime |  |   |
| `COMPLETED_AT` | datetime |  |   |
| `CUTOFF_AT` | datetime |  |   |
| `LINE_COUNT` | int |  | not negative  |
| `STATUS` | str |  |   |

## GiNi order lines

Source type `gini_order_line` · stored as `ORDER_LINE` snapshots · refresh: Scheduled read-only extract from GiNi (MySQL), daily before the G3 run; or upload

Record key: `ORDER_LINE_ID`.

| Field | Type | Required | Validation |
|---|---|---|---|
| `ORDER_LINE_ID` | str | yes |   |
| `ORDER_ID` | str | yes |   |
| `LINE_NUMBER` | int |  |   |
| `MATERIAL_ID` | str |  |   |
| `ORDERED_QTY` | decimal |  | not negative  |
| `UOM` | str |  |   |

## GiNi task to order-line links

Source type `gini_task_order_line` · stored as `TASK_ORDER_LINE` snapshots · refresh: Scheduled read-only extract from GiNi (MySQL), daily before the G3 run; or upload

Record key: `TASK_ID, ORDER_LINE_ID`.

| Field | Type | Required | Validation |
|---|---|---|---|
| `TASK_ID` | str | yes |   |
| `ORDER_LINE_ID` | str | yes |   |
| `ALLOCATED_QTY` | decimal |  | not negative  |

## GiNi materials

Source type `gini_material` · stored as `MATERIAL` snapshots · refresh: Scheduled read-only extract from GiNi (MySQL), daily before the G3 run; or upload

Record key: `MATERIAL_ID`.

| Field | Type | Required | Validation |
|---|---|---|---|
| `MATERIAL_ID` | str | yes |   |
| `BASE_UOM` | str |  |   |
| `TEMPERATURE_CLASS` | str |  |   |
| `HANDLING_CLASS` | str |  |   |
| `WEIGHT_KG` | decimal |  |   |
| `VOLUME_M3` | decimal |  |   |

## GiNi unit-of-measure conversions

Source type `gini_uom_conversion` · stored as `UOM_CONVERSION` snapshots · refresh: Scheduled read-only extract from GiNi (MySQL), daily before the G3 run; or upload

Record key: `MATERIAL_ID, FROM_UOM, TO_UOM, VALID_FROM`.

| Field | Type | Required | Validation |
|---|---|---|---|
| `MATERIAL_ID` | str |  |   |
| `FROM_UOM` | str | yes |   |
| `TO_UOM` | str | yes |   |
| `MULTIPLIER` | decimal | yes | not negative  |
| `VALID_FROM` | date | yes |   |
| `VALID_TO` | date |  |   |

## GiNi / TMS shipments

Source type `gini_shipment` · stored as `SHIPMENT` snapshots · refresh: Scheduled read-only extract from GiNi (MySQL), daily before the G3 run; or upload

Record key: `SHIPMENT_ID`.

| Field | Type | Required | Validation |
|---|---|---|---|
| `SHIPMENT_ID` | str | yes |   |
| `CUSTOMER_CODE` | str | yes |   |
| `CARRIER` | str |  |   |
| `SCHEDULED_DEPARTURE_AT` | datetime |  |   |
| `CUTOFF_AT` | datetime |  |   |
| `DISPATCHED_AT` | datetime |  |   |
| `ORDER_COUNT` | int |  | not negative  |

## GiNi shipment to order links

Source type `gini_shipment_order` · stored as `SHIPMENT_ORDER` snapshots · refresh: Scheduled read-only extract from GiNi (MySQL), daily before the G3 run; or upload

Record key: `SHIPMENT_ID, ORDER_ID`.

| Field | Type | Required | Validation |
|---|---|---|---|
| `SHIPMENT_ID` | str | yes |   |
| `ORDER_ID` | str | yes |   |
| `STAGED_AT` | datetime |  |   |
| `ORDER_CUTOFF_AT` | datetime |  |   |
| `LATE_SECONDS` | decimal |  |   |

## Labor minutes per warehouse order (GiNi Labor Economics)

Source type `labor_order_time` · stored as `LABOR_ORDER_TIME` snapshots · refresh: Scheduled read-only extract from GiNi (MySQL), daily before the G3 run; or upload

Evidence only: labor minutes shown in drill-downs.

Record key: `WORKER_ID, WORK_DATE, ACTIVITY, TI102X3ID`.

| Field | Type | Required | Validation |
|---|---|---|---|
| `WORKER_ID` | str | yes |   |
| `WORK_DATE` | date | yes |   |
| `ACTIVITY` | str |  |   |
| `TI102X3ID` | str | yes |   |
| `SECONDS` | decimal | yes | not negative  |
| `LINES` | int |  | not negative  |

## Daily inventory snapshots (storage)

Source type `inventory_snapshot` · stored as `INVENTORY_SNAPSHOT` snapshots · refresh: Daily snapshot (G1)

Record key: `SNAPSHOT_DATE, CUSTOMER_CODE, UNIT_TYPE, LOCATION, RECEIVED_DATE`; `CUSTOMER_CODE` must resolve through the identity map.

| Field | Type | Required | Validation |
|---|---|---|---|
| `SNAPSHOT_DATE` | date | yes |   |
| `CUSTOMER_CODE` | str | yes |   |
| `UNIT_TYPE` | str | yes |   |
| `QUANTITY` | decimal | yes | not negative  |
| `RECEIVED_DATE` | date |  |   |
| `LOCATION` | str |  |   |

## Storage billing quantities

Source type `storage_activity` · stored as `STORAGE_ACTIVITY` snapshots · refresh: Daily snapshot (G1)

Contract storage units already measured (e.g. pallet-months). G1 derives these from daily snapshots.

Record key: `STORAGE_REF`; creates `storage` events dated by `SERVICE_DATE`; `CUSTOMER_CODE` must resolve through the identity map.

| Field | Type | Required | Validation |
|---|---|---|---|
| `STORAGE_REF` | str | yes |   |
| `CUSTOMER_CODE` | str | yes |   |
| `SERVICE_DATE` | date | yes |   |
| `QUANTITY` | decimal | yes | not negative  |
| `UOM` | str | yes |   |

## Customer invoice lines

Source type `invoice_lines` · stored as `INVOICE_LINE` snapshots · refresh: Per invoice run (CSV/Excel upload; API adapter later)

What was actually invoiced. ADJUSTMENT_REF (e.g. LKG-42) marks a line that settles a leakage item.

Record key: `INVOICE_NUMBER, LINE_NUMBER`; period-scoped by `PERIOD` (must be an open period label); `CUSTOMER_CODE` must resolve through the identity map; `CHARGE_CODE` must be a defined charge code.

| Field | Type | Required | Validation |
|---|---|---|---|
| `INVOICE_NUMBER` | str | yes |   |
| `LINE_NUMBER` | int | yes |   |
| `CUSTOMER_CODE` | str | yes |   |
| `CHARGE_CODE` | str | yes |   |
| `PERIOD` | str | yes |   |
| `QUANTITY` | decimal |  |   |
| `UOM` | str |  |   |
| `UNIT_PRICE` | decimal |  |   |
| `AMOUNT` | decimal | yes |   |
| `POSTED_ON` | date |  |   |
| `INVOICE_DATE` | date |  |   |
| `ADJUSTMENT_REF` | str |  |   |

## Employee labor cost (EMPLOYEE_LABOR_COST)

Source type `employee_labor_cost` · stored as `EMPLOYEE_LABOR_COST` snapshots · refresh: Monthly / per period, before close (CSV/Excel upload)

Record key: `WORKER_ID, PERIOD`; period-scoped by `PERIOD` (must be an open period label).

| Field | Type | Required | Validation |
|---|---|---|---|
| `WORKER_ID` | str | yes |   |
| `EMPLOYER_TYPE` | str | yes |   |
| `ROLE` | str | yes |   |
| `PERIOD` | str | yes |   |
| `PAID_HOURS` | decimal | yes | not negative  |
| `LOADED_COST` | decimal | yes | not negative  |
| `OVERTIME_HOURS` | decimal |  | not negative  |

## Labor time attribution

Source type `labor_time_attribution` · stored as `LABOR_TIME_ATTRIBUTION` snapshots · refresh: Monthly / per period, before close (CSV/Excel upload)

Blank CUSTOMER_CODE = unassigned time (training, shared work, idle, meetings); UNASSIGNED_REASON required then.

Record key: `WORKER_ID, PERIOD, CUSTOMER_CODE, UNASSIGNED_REASON`; period-scoped by `PERIOD` (must be an open period label); `CUSTOMER_CODE` must resolve through the identity map.

| Field | Type | Required | Validation |
|---|---|---|---|
| `WORKER_ID` | str | yes |   |
| `PERIOD` | str | yes |   |
| `CUSTOMER_CODE` | str |  |   |
| `HOURS` | decimal | yes | not negative  |
| `BASIS` | str | yes |   |
| `UNASSIGNED_REASON` | str |  |   |

## Vendor and agency invoices (VENDOR_INVOICE_HEADER / LINES)

Source type `vendor_invoices` · stored as `VENDOR_INVOICE_LINE` snapshots · refresh: Monthly / per period, before close (CSV/Excel upload)

Record key: `VENDOR, INVOICE_NUMBER, LINE_NUMBER`; period-scoped by `PERIOD` (must be an open period label); `CUSTOMER_CODE` must resolve through the identity map.

| Field | Type | Required | Validation |
|---|---|---|---|
| `VENDOR` | str | yes |   |
| `VENDOR_TYPE` | str | yes |   |
| `INVOICE_NUMBER` | str | yes |   |
| `LINE_NUMBER` | int | yes |   |
| `INVOICE_DATE` | date |  |   |
| `PERIOD` | str | yes |   |
| `PO_REF` | str |  |   |
| `SERVICE` | str | yes |   |
| `EXPENSE_CATEGORY` | str | yes |   |
| `EXPENSE_ACCOUNT` | str |  |   |
| `CUSTOMER_CODE` | str |  |   |
| `SHIPMENT_REF` | str |  |   |
| `AMOUNT` | decimal | yes |   |
| `CUSTOMER_PAYS_CARRIER` | bool |  |   |

## Expense ledger lines (payroll journal, facility, manual)

Source type `expense_ledger` · stored as `EXPENSE_LEDGER` snapshots · refresh: Monthly / per period, before close (CSV/Excel upload)

Record key: `REF`; period-scoped by `PERIOD` (must be an open period label); `CUSTOMER_CODE` must resolve through the identity map.

| Field | Type | Required | Validation |
|---|---|---|---|
| `REF` | str | yes |   |
| `PERIOD` | str | yes |   |
| `EXPENSE_CATEGORY` | str | yes |   |
| `DESCRIPTION` | str | yes |   |
| `AMOUNT` | decimal | yes |   |
| `SOURCE_TYPE` | str | yes |   |
| `PAYMENT_RECIPIENT_TYPE` | str | yes |   |
| `CUSTOMER_CODE` | str |  |   |

## Allocation driver quantities

Source type `driver_quantities` · stored as `DRIVER_QUANTITY` snapshots · refresh: Monthly / per period, before close (CSV/Excel upload)

Blank CUSTOMER_CODE = unassigned driver quantity (idle/shared).

Record key: `EXPENSE_CATEGORY, PERIOD, CUSTOMER_CODE`; period-scoped by `PERIOD` (must be an open period label); `CUSTOMER_CODE` must resolve through the identity map.

| Field | Type | Required | Validation |
|---|---|---|---|
| `EXPENSE_CATEGORY` | str | yes |   |
| `PERIOD` | str | yes |   |
| `CUSTOMER_CODE` | str |  |   |
| `QUANTITY` | decimal | yes | not negative  |
| `SOURCE_REF` | str | yes |   |

## Rate card (loads a draft version for approval)

Source type `rate_card` · stored as `RATE_CARD` snapshots · refresh: On contract change (loads a draft version for approval)

Record key: `CUSTOMER_CODE, CHARGE_CODE, VALID_FROM, CONDITION`; `CUSTOMER_CODE` must resolve through the identity map; `CHARGE_CODE` must be a defined charge code.

| Field | Type | Required | Validation |
|---|---|---|---|
| `CUSTOMER_CODE` | str | yes |   |
| `CONTRACT_REFERENCE` | str | yes |   |
| `CHARGE_CODE` | str | yes |   |
| `ACTIVITY_CODE` | str |  |   |
| `PRICING_METHOD` | str | yes |   |
| `UNIT_PRICE` | decimal |  | not negative  |
| `UOM` | str |  |   |
| `MARKUP_PCT` | decimal |  |   |
| `ELIGIBLE_COST_CATEGORIES` | str |  |   |
| `MINIMUM_CHARGE` | decimal |  | not negative  |
| `BILLING_PERIOD` | str | yes |   |
| `TIER_METHOD` | str |  |   |
| `TIERS` | json |  |   |
| `CONDITION` | json |  |   |
| `VALID_FROM` | date | yes |   |
| `VALID_TO` | date |  |   |
| `CURRENCY_CODE` | str | yes |   |
| `SOURCE_REF` | str |  |   |

## Accounting export file (G8, `file` adapter)

One CSV line per approved adjustment or credit note:

| Column | Meaning |
|---|---|
| `DOCUMENT_TYPE` | invoice_adjustment or credit_note |
| `CUSTOMER_CODE` | 3PL customer |
| `ORIGINAL_INVOICE` | invoice the gap refers to, or 'New invoice line' |
| `CHARGE_CODE` | charge code |
| `QUANTITY` | billable − billed quantity |
| `UNIT_PRICE` | contract unit price |
| `AMOUNT` | gap (negative = credit) |
| `CURRENCY` | site currency |
| `PERIOD` | period label |
| `LEAKAGE_ITEM_ID` | LKG-<id>; put it in ADJUSTMENT_REF of the invoice line that bills it |
| `REASON` | rule and the deterministic sentence |
| `APPROVER` | approver, or 'Below threshold; decided by …' |

## Settings (no built-in defaults)

| Setting | Type | Required for | Description |
|---|---|---|---|
| `tolerance.L1.amount` | decimal | run | L1: gap amount (USD) that must be exceeded to raise an item |
| `tolerance.L1.pct` | decimal | run | L1: gap as % of billable that must be exceeded to raise an item |
| `tolerance.L2.amount` | decimal | run | L2: gap amount (USD) that must be exceeded to raise an item |
| `tolerance.L2.pct` | decimal | run | L2: gap as % of billable that must be exceeded to raise an item |
| `tolerance.L3.amount` | decimal | run | L3: gap amount (USD) that must be exceeded to raise an item |
| `tolerance.L3.pct` | decimal | run | L3: gap as % of billable that must be exceeded to raise an item |
| `tolerance.L4.amount` | decimal | run | L4: gap amount (USD) that must be exceeded to raise an item |
| `tolerance.L4.pct` | decimal | run | L4: gap as % of billable that must be exceeded to raise an item |
| `tolerance.L5.amount` | decimal | run | L5: gap amount (USD) that must be exceeded to raise an item |
| `tolerance.L5.pct` | decimal | run | L5: gap as % of billable that must be exceeded to raise an item |
| `tolerance.L6.amount` | decimal | run | L6: gap amount (USD) that must be exceeded to raise an item |
| `tolerance.L6.pct` | decimal | run | L6: gap as % of billable that must be exceeded to raise an item |
| `tolerance.L7.amount` | decimal | run | L7: gap amount (USD) that must be exceeded to raise an item |
| `tolerance.L7.pct` | decimal | run | L7: gap as % of billable that must be exceeded to raise an item |
| `leakage.approval_threshold` | decimal | decide | Leakage decisions with |gap| above this amount (USD) need a second person's approval |
| `leakage.back_billing_window_days` | int | decide | 'Bill now' is not allowed for periods that ended more than this many days ago |
| `labor.cost_basis` | choice:employee_labor_cost,standard_rate | labor | Labor cost basis: employee_labor_cost (imported paid hours and loaded cost) or standard_rate |
| `labor.standard_rates` | json | optional | Standard loaded rate per activity (JSON {activity: rate}); used only when the basis is standard_rate |
| `customer_resolution.join_path` | json | run | Join path per event type from each operational record to the 3PL customer (JSON). Must be verified against the deployed GiNi schema. |
| `gini.worktype_activity` | json | import:gini_ti102c | GiNi WORKTYPE -> activity code map for TI102/TI102C (JSON) |
| `close.margin_range_low` | decimal | close | Close check 6: customer margins below this % need acknowledgement |
| `close.margin_range_high` | decimal | close | Close check 6: customer margins above this % need acknowledgement |
| `invoice.source_system` | str | optional | Invoice source system and format for the pilot (open decision 7) |
| `pilot.subscription_cost` | decimal | optional | Subscription cost (USD) for the pilot results export (return on subscription) |
| `storage.daily_snapshots_available` | choice:yes,no | optional | Whether daily inventory snapshots per customer exist in GiNi (open decision 8; needed for G1) |
| `storage.policy` | json | storage | G1 storage policy JSON: {"unit_type","method":"average_daily|unit_days|peak","output_uom","grace_days","split_month":"prorate|full_month"} |
| `alerts.daily_run_hour` | int | optional | Local hour (0-23) for the daily rating and rule run (G3); empty = off |
| `drift.renewal_warning_days` | int | drift | G6: warn when a contract ends within this many days |
| `accounting.adapter` | choice:file,quickbooks,netsuite,sap,dynamics | export | G8: accounting adapter (file is the documented starting format) |
| `margin_target.<CUSTOMER_CODE>` | decimal | optional | Margin target % per customer (open decision 9) |
