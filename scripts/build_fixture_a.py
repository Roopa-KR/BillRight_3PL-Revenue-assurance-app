"""Build Fixture A (GiNi synthetic billing dataset) as import files from the GiNi Labor Economics prototype.

Usage: python scripts/build_fixture_a.py <path to GiNi_Labor_Economics/data>

Reads the prototype's SQLite snapshots (namespace ``intelligence``, ``operations`` and ``finance``) and writes
GiNi-style extracts to fixtures/fixture_a/. The reference sheet ``out_billing_leakage`` is written to
tests/truth/ for the acceptance tests only; application code never reads it.
All records are synthetic (fictional customers).
"""
import csv
import gzip
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "fixtures" / "fixture_a"
TRUTH = ROOT / "tests" / "truth"


def sheet(c, ns, name):
    q = ("select payload from raw r join active_file a on a.batch_id=r.batch_id and a.namespace=r.namespace "
         "where r.namespace=? and r.sheet=? order by r.row_no")
    return [json.loads(p) for (p,) in c.execute(q, (ns, name))]


def evidence(e, ns, name):
    return [json.loads(p) for (p,) in e.execute("select payload from raw where namespace=? and sheet=? order by row_no", (ns, name))]


def write(name, header, rows, gz=False):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / (name + (".csv.gz" if gz else ".csv"))
    opener = (lambda p: gzip.open(p, "wt", newline="", encoding="utf-8", compresslevel=6)) if gz else (lambda p: open(p, "w", newline="", encoding="utf-8"))
    with opener(path) as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])
    print(f"{path.name}: {len(rows):,} rows")


def strip(v, prefix):
    return v[len(prefix):] if isinstance(v, str) and v.startswith(prefix) else v


def main(data_dir: Path):
    c = sqlite3.connect(data_dir / "gini.sqlite")
    e = sqlite3.connect(data_dir / "gini_evidence.sqlite")
    customers = sheet(c, "intelligence", "customer")
    write("01_customer_master_M140", ["CUSTOMER_CODE", "CUSTOMER_NAME", "SEGMENT", "ACTIVE", "SOURCE_SYSTEM"],
          [(x["customer_code"], x["customer_name"], x["segment"], "true" if x["active"] else "false", "GiNi M140 (synthetic)") for x in customers])
    mats = evidence(e, "intelligence", "material")
    write("02_material", ["MATERIAL_ID", "BASE_UOM", "TEMPERATURE_CLASS", "HANDLING_CLASS", "WEIGHT_KG", "VOLUME_M3"],
          [(x["material_code"], x["base_uom"], x["temperature_class"], x["handling_class"], x["weight_kg"], x["volume_m3"]) for x in mats], gz=True)
    uoms = evidence(e, "intelligence", "uom_conversion")
    write("03_uom_conversion", ["MATERIAL_ID", "FROM_UOM", "TO_UOM", "MULTIPLIER", "VALID_FROM", "VALID_TO"],
          [(strip(x["material_id"], "mat-"), x["from_uom"], x["to_uom"], x["multiplier"], x["valid_from"], x["valid_to"]) for x in uoms])
    heads = sheet(c, "intelligence", "order_header")
    write("04_order_header", ["ORDER_ID", "ORDER_CODE", "CUSTOMER_CODE", "ORDER_TYPE", "RELEASED_AT", "COMPLETED_AT", "CUTOFF_AT", "LINE_COUNT", "STATUS"],
          [(strip(x["id"], "oh-"), x["order_code"], strip(x["customer_id"], "cust-"), x["order_type"], x["released_at"], x["completed_at"],
            x["cutoff_at"], x["line_count"], x["status"]) for x in heads], gz=True)
    lines = sheet(c, "intelligence", "order_line")
    write("05_order_line", ["ORDER_LINE_ID", "ORDER_ID", "LINE_NUMBER", "MATERIAL_ID", "ORDERED_QTY", "UOM"],
          [(strip(x["id"], "ol-"), strip(x["order_id"], "oh-"), x["line_number"], x["material_id"], x["ordered_qty"], x["uom"]) for x in lines], gz=True)
    links = sheet(c, "intelligence", "task_order_line")
    write("06_task_order_line", ["TASK_ID", "ORDER_LINE_ID", "ALLOCATED_QTY"],
          [(strip(x["task_id"], "wt-"), strip(x["order_line_id"], "ol-"), x["allocated_qty"]) for x in links], gz=True)
    tasks = sheet(c, "operations", "warehouse_task")
    done = [t for t in tasks if t["status"] == "completed" and t.get("completed_at")]
    open_ = [t for t in tasks if not (t["status"] == "completed" and t.get("completed_at"))]
    write("07_TI102C_completed_picking", ["TI102ID", "TI102X3ID", "CLIENTID", "WHID", "WORKTYPE", "MATERIALID", "SRCLOC", "RESOURCENAME", "WORKER_ID",
                                          "PROCESSEDQTY", "UOM", "CREATEDATETIME", "COMPLETED_AT"],
          [(t["source_record_key"], t["warehouse_order_code"], "SYN", "100", t["source_worktype_code"], t["material_id"], t["source_location"],
            t["source_resource_name"], t["worker_id"], t["completed_qty"], t["uom"], t["created_at"], t["completed_at"]) for t in done], gz=True)
    write("08_TI102_open_picking", ["TI102ID", "TI102X3ID", "CLIENTID", "WORKTYPE", "MATERIALID", "RESOURCENAME", "ORDERQTY", "OPENQTY", "CREATEDATETIME"],
          [(t["source_record_key"], t["warehouse_order_code"], "SYN", t["source_worktype_code"], t["material_id"], t["source_resource_name"],
            t["ordered_qty"], t["open_qty"], t["created_at"]) for t in open_], gz=True)
    ships = evidence(e, "intelligence", "shipment")
    write("09_shipment", ["SHIPMENT_ID", "CUSTOMER_CODE", "CARRIER", "SCHEDULED_DEPARTURE_AT", "CUTOFF_AT", "DISPATCHED_AT", "ORDER_COUNT"],
          [(strip(x["id"], "shp-"), strip(x["customer_id"], "cust-"), x["carrier"], x["scheduled_departure_at"], x["cutoff_at"], x["dispatched_at"],
            x["order_count"]) for x in ships])
    so = evidence(e, "intelligence", "shipment_order")
    write("10_shipment_order", ["SHIPMENT_ID", "ORDER_ID", "STAGED_AT", "ORDER_CUTOFF_AT", "LATE_SECONDS"],
          [(strip(x["shipment_id"], "shp-"), strip(x["order_id"], "oh-"), x["staged_at"], x["order_cutoff_at"], x["late_seconds"]) for x in so], gz=True)
    oc = c.execute("select worker, day, activity, order_code, seconds, lines from order_cost order by worker, day, activity, order_code").fetchall()
    write("11_labor_order_time", ["WORKER_ID", "WORK_DATE", "ACTIVITY", "TI102X3ID", "SECONDS", "LINES"],
          [(w, d, strip(a, "act-"), o, f"{s:.0f}", n) for w, d, a, o, s, n in oc], gz=True)
    rates = sheet(c, "intelligence", "contract_rate")
    write("12_rate_card", ["CUSTOMER_CODE", "CONTRACT_REFERENCE", "CHARGE_CODE", "ACTIVITY_CODE", "PRICING_METHOD", "UNIT_PRICE", "UOM", "MARKUP_PCT",
                           "ELIGIBLE_COST_CATEGORIES", "MINIMUM_CHARGE", "BILLING_PERIOD", "TIER_METHOD", "TIERS", "CONDITION", "VALID_FROM", "VALID_TO",
                           "CURRENCY_CODE", "SOURCE_REF"],
          [(strip(r["customer_id"], "cust-"), r["contract_reference"], r["charge_code"], strip(r["activity_id"], "act-") if r["activity_id"] else None,
            "per_unit", repr(r["unit_price"]), r["uom"], None, None, r["minimum_charge"] or None, r["billing_period"], "none", None, None, r["valid_from"],
            r["valid_to"], r["currency_code"], r["id"]) for r in rates])
    bills = sheet(c, "intelligence", "billing_line")
    write("13_invoice_lines_2026-W37_W38", ["INVOICE_NUMBER", "LINE_NUMBER", "CUSTOMER_CODE", "CHARGE_CODE", "PERIOD", "QUANTITY", "UOM", "UNIT_PRICE",
                                            "AMOUNT", "POSTED_ON", "INVOICE_DATE", "ADJUSTMENT_REF"],
          [(b["invoice_number"], b["line_number"], strip(b["customer_id"], "cust-"), b["charge_code"], strip(b["period_id"], "prd-"), b["quantity"],
            b["uom"], repr(b["unit_price"]), f"{b['amount']:.2f}", b["posted_on"], b["posted_on"], None) for b in bills])
    sla = evidence(e, "intelligence", "sla_rule")
    (OUT / "14_sla_rules.json").write_text(json.dumps([{k: x[k] for k in ("customer_id", "milestone", "allowed_late_seconds", "penalty_amount",
                                                                          "penalty_basis", "cap_amount", "valid_from", "valid_to", "contract_reference")}
                                                       for x in sla], indent=1))
    TRUTH.mkdir(parents=True, exist_ok=True)
    truth = evidence(e, "intelligence", "out_billing_leakage")
    with open(TRUTH / "out_billing_leakage.csv", "w", newline="") as f:
        w = csv.writer(f)
        keys = ["customer_id", "period_id", "charge_code", "contract_rate_id", "unit_price", "billable_qty", "billed_qty", "billable_amount",
                "billed_amount", "leakage_amount", "leakage_note"]
        w.writerow(keys)
        for t in truth:
            w.writerow([repr(t[k]) if isinstance(t[k], float) else t[k] for k in keys])
    print(f"tests/truth/out_billing_leakage.csv: {len(truth)} rows (test truth only)")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
