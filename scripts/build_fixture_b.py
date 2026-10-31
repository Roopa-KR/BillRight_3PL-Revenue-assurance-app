"""Build Fixture B: the BSP worked example (one month, three customers) as import files.

ILLUSTRATIVE DATA. The product document gives the customer split of each expense pool, not the underlying
driver quantities; the driver quantities, operational events, worker rows and FoodCo/PartsCo service mixes below
are constructed to reproduce exactly the document's figures through BillRight's normal calculations.
"""
import csv
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "fixtures" / "fixture_b"
MONTH = ("2026-08", "2026-08-01", "2026-08-31")
CUST = {"RETAILCO": "RetailCo", "FOODCO": "FoodCo", "PARTSCO": "PartsCo"}
# per customer: receiving pallets, storage pallet-months, pick lines, orders (packing), carrier bill, special-project hours
VOLUMES = {
    "RETAILCO": dict(pallets=1000, pallet_months=3000, lines=12000, orders=10000, freight=30000, hours=200),
    "FOODCO": dict(pallets=500, pallet_months=2000, lines=8000, orders=6000, freight=18000, hours=105),
    "PARTSCO": dict(pallets=400, pallet_months=1500, lines=6000, orders=4000, freight=12000, hours=250),
}
RATES = dict(RECEIVING=("12", "pallet"), STORAGE=("12", "pallet_month"), PICKING=("3", "line"), PACKING=("2", "order"), SPECIAL_PROJECTS=("40", "hour"))
SHARES = {  # expense category: (driver type, description, RetailCo, FoodCo, PartsCo quantities)
    "warehouse_operators": ("paid_labor_hours_by_customer", "Paid operator hours attributed to customer tasks (illustrative)", 2000, 1200, 800),
    "forklift_operators": ("equipment_hours_by_customer", "Documented forklift hours by customer (illustrative)", 100, 100, 200),
    "supervisors": ("documented_time_share", "Documented supervision time, % (document: 40/35/25)", 40, 35, 25),
    "managers": ("documented_time_share", "Documented dedicated management time, % (illustrative)", 40, 35, 25),
    "administration": ("transactions_by_customer", "Administrative workload: documents processed (illustrative)", 50, 30, 20),
    "warehouse_rent": ("pallet_days", "Occupied pallet-days (illustrative)", 50, 30, 20),
    "utilities": ("metered_usage", "Sub-metered usage share (illustrative)", 50, 30, 20),
    "equipment": ("equipment_hours_by_customer", "Equipment hours used (illustrative)", 25, 25, 50),
    "packaging": ("materials_consumed", "Packaging materials consumed (illustrative)", 50, 30, 20),
    "software": ("transactions_by_customer", "WMS transactions (illustrative)", 50, 30, 20),
    "other_overhead": ("transactions_by_customer", "Documented allocation rule: transactions (illustrative)", 50, 30, 20),
}


def w(name, header, rows):
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / name, "w", newline="") as f:
        cw = csv.writer(f)
        cw.writerow(header)
        cw.writerows([["" if v is None else v for v in r] for r in rows])
    print(name, len(rows))


def main():
    label, start, end = MONTH
    w("01_customer_master.csv", ["CUSTOMER_CODE", "CUSTOMER_NAME", "SEGMENT", "ACTIVE", "SOURCE_SYSTEM"],
      [(k, v, "illustrative", "true", "Illustrative M140") for k, v in CUST.items()])
    orders, lines, receipts, storage = [], [], [], []
    oid = 900000
    tid = 5000000
    rid = 1
    for code, v in VOLUMES.items():
        ids = []
        for n in range(v["orders"]):
            oid += 1
            day = 1 + n % 31
            ids.append(str(oid))
            orders.append((oid, f"SO-{oid}", code, "outbound", f"2026-08-{day:02d}T08:00:00-05:00", f"2026-08-{day:02d}T14:00:00-05:00", None, None, "shipped"))
        for n in range(v["lines"]):
            tid += 1
            order = ids[n % len(ids)]
            day = 1 + (n % len(ids)) % 31
            lines.append((tid, order, "BSP", "1", "1", f"MAT-{n % 50}", "A-01", "op", "OP001", 1, "each", f"2026-08-{day:02d}T09:00:00-05:00",
                          f"2026-08-{day:02d}T13:00:00-05:00"))
        for n in range(v["pallets"] // 10):
            receipts.append((f"R{rid:05d}", f"ASN-{rid}", code, None, 10, "pallet", f"2026-08-{1 + n % 31:02d}T07:00:00-05:00"))
            rid += 1
        storage.append((f"STO-{code}-{label}", code, end, v["pallet_months"], "pallet_month"))
    w("02_order_header.csv", ["ORDER_ID", "ORDER_CODE", "CUSTOMER_CODE", "ORDER_TYPE", "RELEASED_AT", "COMPLETED_AT", "CUTOFF_AT", "LINE_COUNT", "STATUS"], orders)
    w("03_TI102C_completed_picking.csv", ["TI102ID", "TI102X3ID", "CLIENTID", "WHID", "WORKTYPE", "MATERIALID", "SRCLOC", "RESOURCENAME", "WORKER_ID",
                                          "PROCESSEDQTY", "UOM", "CREATEDATETIME", "COMPLETED_AT"], lines)
    w("04_TI202_receiving.csv", ["TI202ID", "RECEIPT_REF", "OWNER_CODE", "MATERIALID", "QUANTITY", "UOM", "RECEIVED_AT"], receipts)
    w("05_storage_activity.csv", ["STORAGE_REF", "CUSTOMER_CODE", "SERVICE_DATE", "QUANTITY", "UOM"], storage)
    rate_rows = []
    for code in CUST:
        ref = f"MSA-{code}-2026 (illustrative)"
        for charge, (price, uom) in RATES.items():
            method = "hourly" if charge == "SPECIAL_PROJECTS" else "per_unit"
            act = "PICK" if charge == "PICKING" else None
            rate_rows.append((code, ref, charge, act, method, price, uom, None, None, None, "month", "none", None, None, "2026-01-01", None, "USD", None))
        rate_rows.append((code, ref, "TRANSPORT", None, "cost_plus", None, None, "10", "transport_carriers", None, "month", "none", None, None,
                          "2026-01-01", None, "USD", None))
    w("06_rate_card.csv", ["CUSTOMER_CODE", "CONTRACT_REFERENCE", "CHARGE_CODE", "ACTIVITY_CODE", "PRICING_METHOD", "UNIT_PRICE", "UOM", "MARKUP_PCT",
                           "ELIGIBLE_COST_CATEGORIES", "MINIMUM_CHARGE", "BILLING_PERIOD", "TIER_METHOD", "TIERS", "CONDITION", "VALID_FROM", "VALID_TO",
                           "CURRENCY_CODE", "SOURCE_REF"], rate_rows)
    inv = []
    for i, (code, v) in enumerate(VOLUMES.items(), start=1):
        n = f"BSP-INV-{label}-{i}"
        items = [("RECEIVING", v["pallets"], "pallet", 12), ("STORAGE", v["pallet_months"], "pallet_month", 12), ("PICKING", v["lines"], "line", 3),
                 ("PACKING", v["orders"], "order", 2), ("SPECIAL_PROJECTS", v["hours"], "hour", 40)]
        for ln, (charge, q, uom, price) in enumerate(items, start=1):
            inv.append((n, ln, code, charge, label, q, uom, price, f"{q * price:.2f}", end, end, None))
        inv.append((n, 6, code, "TRANSPORT", label, None, None, None, f"{v['freight'] * 1.1:.2f}", end, end, None))
    w("07_invoice_lines.csv", ["INVOICE_NUMBER", "LINE_NUMBER", "CUSTOMER_CODE", "CHARGE_CODE", "PERIOD", "QUANTITY", "UOM", "UNIT_PRICE", "AMOUNT",
                               "POSTED_ON", "INVOICE_DATE", "ADJUSTMENT_REF"], inv)
    ledger = [("PAY-OPS", label, "warehouse_operators", "BSP operator payroll", 56000, "payroll", "direct_payroll", None),
              ("PAY-FORK", label, "forklift_operators", "Forklift operator payroll", 16000, "payroll", "direct_payroll", None),
              ("PAY-SUP", label, "supervisors", "Supervisor payroll", 16000, "payroll", "direct_payroll", None),
              ("PAY-MGR", label, "managers", "Warehouse manager payroll", 10000, "payroll", "direct_payroll", None),
              ("PAY-ADM", label, "administration", "Administration staff payroll", 8000, "payroll", "direct_payroll", None)]
    w("08_expense_ledger_payroll.csv", ["REF", "PERIOD", "EXPENSE_CATEGORY", "DESCRIPTION", "AMOUNT", "SOURCE_TYPE", "PAYMENT_RECIPIENT_TYPE", "CUSTOMER_CODE"], ledger)
    vend = [("XYZ Staffing Agency", "staffing_agency", "XYZ-0826", 1, "2026-08-31", label, None, "Outsourced warehouse operators", "warehouse_operators", None, None, None, 24000, None),
            ("ABC Transport", "carrier", "ABC-0826", 1, "2026-08-31", label, None, "Trucking for RetailCo", "transport_carriers", None, "RETAILCO", None, 30000, None),
            ("ABC Transport", "carrier", "ABC-0826", 2, "2026-08-31", label, None, "Trucking for FoodCo", "transport_carriers", None, "FOODCO", None, 18000, None),
            ("ABC Transport", "carrier", "ABC-0826", 3, "2026-08-31", label, None, "Trucking for PartsCo", "transport_carriers", None, "PARTSCO", None, 12000, None),
            ("Warehouse Landlord LLC", "landlord", "RENT-0826", 1, "2026-08-01", label, None, "Warehouse rent", "warehouse_rent", None, None, None, 30000, None),
            ("City Utilities", "facility_services", "UTIL-0826", 1, "2026-08-31", label, None, "Electricity and water", "utilities", None, None, None, 6000, None),
            ("LiftRent Co", "equipment", "EQ-0826", 1, "2026-08-31", label, None, "Equipment rental and maintenance", "equipment", None, None, None, 7000, None),
            ("BoxSupply Inc", "packaging", "PK-0826", 1, "2026-08-31", label, None, "Packaging materials", "packaging", None, None, None, 9000, None),
            ("WMS Vendor", "it", "IT-0826", 1, "2026-08-31", label, None, "WMS and software", "software", None, None, None, 4000, None),
            ("SafeGuard Services", "facility_services", "SG-0826", 1, "2026-08-31", label, None, "Security, insurance, cleaning and other", "other_overhead", None, None, None, 10000, None)]
    w("09_vendor_invoices.csv", ["VENDOR", "VENDOR_TYPE", "INVOICE_NUMBER", "LINE_NUMBER", "INVOICE_DATE", "PERIOD", "PO_REF", "SERVICE", "EXPENSE_CATEGORY",
                                 "EXPENSE_ACCOUNT", "CUSTOMER_CODE", "SHIPMENT_REF", "AMOUNT", "CUSTOMER_PAYS_CARRIER"], vend)
    labor = [("OP001", "employee", "operator", label, 160, 4800)]
    labor += [(f"OP{n:03d}", "employee", "operator", label, 160, 3200) for n in range(2, 18)]
    labor += [(f"FL{n:03d}", "employee", "forklift", label, 160, 4000) for n in range(1, 5)]
    labor += [(f"SV{n:03d}", "employee", "supervisor", label, 160, 8000) for n in range(1, 3)]
    labor += [("MG001", "employee", "manager", label, 160, 10000)]
    labor += [(f"AD{n:03d}", "employee", "admin", label, 160, 4000) for n in range(1, 3)]
    labor += [(f"AG{n:03d}", "agency", "operator", label, 120, 2400) for n in range(1, 11)]
    w("10_employee_labor_cost.csv", ["WORKER_ID", "EMPLOYER_TYPE", "ROLE", "PERIOD", "PAID_HOURS", "LOADED_COST", "OVERTIME_HOURS"],
      [(*r, None) for r in labor])
    w("11_labor_time_attribution.csv", ["WORKER_ID", "PERIOD", "CUSTOMER_CODE", "HOURS", "BASIS", "UNASSIGNED_REASON"],
      [("OP001", label, "RETAILCO", 90, "task_time", None), ("OP001", label, "FOODCO", 50, "task_time", None), ("OP001", label, "PARTSCO", 20, "task_time", None)])
    w("11b_labor_time_attribution_with_training.csv", ["WORKER_ID", "PERIOD", "CUSTOMER_CODE", "HOURS", "BASIS", "UNASSIGNED_REASON"],
      [("OP001", label, "RETAILCO", 80, "task_time", None), ("OP001", label, "FOODCO", 40, "task_time", None), ("OP001", label, "PARTSCO", 20, "task_time", None),
       ("OP001", label, None, 20, "documented_time", "training")])
    dq = []
    for cat, (dtype, desc, r, f, p) in SHARES.items():
        for code, q in (("RETAILCO", r), ("FOODCO", f), ("PARTSCO", p)):
            dq.append((cat, label, code, q, f"illustrative:{dtype}"))
    w("12_driver_quantities.csv", ["EXPENSE_CATEGORY", "PERIOD", "CUSTOMER_CODE", "QUANTITY", "SOURCE_REF"], dq)
    setup = {
        "periods": [{"label": label, "start": start, "end": end, "type": "month"}],
        "charge_codes": [
            {"code": "RECEIVING", "name": "Receiving", "category": "receiving", "uom": "pallet", "event_type": "receipt"},
            {"code": "STORAGE", "name": "Storage", "category": "storage", "uom": "pallet_month", "event_type": "storage"},
            {"code": "PICKING", "name": "Picking", "category": "picking", "uom": "line", "event_type": "pick_line"},
            {"code": "PACKING", "name": "Packing", "category": "packing", "uom": "order", "event_type": "order_completed"},
            {"code": "TRANSPORT", "name": "Transportation (cost + markup)", "category": "transport", "uom": None, "event_type": None},
            {"code": "SPECIAL_PROJECTS", "name": "Special projects", "category": "special_project", "uom": "hour", "event_type": "special_project_hours", "manual": True},
        ],
        "settings": {
            "customer_resolution.join_path": {
                "pick_line": [{"from": "order_ref"}, {"table": "ORDER_HEADER", "match": "ORDER_ID", "take": "CUSTOMER_CODE"}, {"identity_map": "customer_code"}],
                "order_completed": [{"from": "order_ref"}, {"table": "ORDER_HEADER", "match": "ORDER_ID", "take": "CUSTOMER_CODE"}, {"identity_map": "customer_code"}],
                "receipt": [{"from": "attribute:owner_code"}, {"identity_map": "customer_code"}],
                "storage": [{"from": "attribute:customer_code"}, {"identity_map": "customer_code"}]},
            "gini.worktype_activity": {"1": "PICK"},
            "leakage.approval_threshold": "5000", "leakage.back_billing_window_days": "90", "labor.cost_basis": "employee_labor_cost",
            "close.margin_range_low": "10", "close.margin_range_high": "30", "drift.renewal_warning_days": "60", "accounting.adapter": "file",
        },
        "drivers": [{"category": c, "driver_type": t, "description": d} for c, (t, d, *_q) in SHARES.items()]
                   + [{"category": "transport_carriers", "driver_type": "direct", "description": "Actual freight bill by customer (direct)"}],
        "imports": [["gini_order_header", "02_order_header.csv"], ["gini_ti102c", "03_TI102C_completed_picking.csv"],
                    ["gini_ti202", "04_TI202_receiving.csv"], ["storage_activity", "05_storage_activity.csv"], ["rate_card", "06_rate_card.csv"],
                    ["expense_ledger", "08_expense_ledger_payroll.csv"], ["vendor_invoices", "09_vendor_invoices.csv"],
                    ["employee_labor_cost", "10_employee_labor_cost.csv"], ["labor_time_attribution", "11_labor_time_attribution.csv"],
                    ["driver_quantities", "12_driver_quantities.csv"], ["invoice_lines", "07_invoice_lines.csv"]],
        "manual_entries": [{"customer": c, "charge": "SPECIAL_PROJECTS", "quantity": v["hours"], "uom": "hour", "date": "2026-08-20",
                            "note": f"Special project hours for {CUST[c]} (illustrative)"} for c, v in VOLUMES.items()],
        "label": "Illustrative: BSP worked example; driver quantities constructed to reproduce the document's allocation split.",
    }
    (OUT / "setup.json").write_text(json.dumps(setup, indent=1))


if __name__ == "__main__":
    main()
