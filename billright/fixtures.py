"""Load Fixture A (GiNi synthetic billing dataset) and Fixture B (BSP worked example, illustrative) through the
normal application paths: import centre, rate-card editor with approval, manual-entry workflow, driver approval.

Tolerances, thresholds and margin ranges set here are FIXTURE / TEST settings (section 12.2), not product defaults.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from sqlalchemy import select

from . import models as m
from .config import ROOT
from .db import make_session
from .ingest.importer import import_file
from .services import admin, contracts
from .services.common import RULES, SETTINGS, Actor, set_setting

FIX = ROOT / "fixtures"
TEST_TOLERANCES = {f"tolerance.{r}.{k}": "0" for r in RULES for k in ("amount", "pct")}


def _owner_session():
    from .config import Config
    return make_session(None, Config.DATABASE_OWNER_URL)


def bootstrap_tenant(name: str, users: list[tuple[str, str, list[str]]], site_args: dict):
    """Create tenant (owner connection), site and users. Returns (tenant_id, site_id, {email: user_id})."""
    with _owner_session() as own:
        t = admin.create_tenant(own, name)
        own.commit()
        tid = t.id
    s = make_session(tid)
    system = Actor(tid, None)
    site = admin.create_site(s, system, **site_args)
    ids = {}
    for email, uname, roles in users:
        u = admin.create_user(s, system, email, uname, roles, [(site.id, None)])
        ids[email] = u.id
    s.commit()
    s.close()
    return tid, site.id, ids


def _imp(s, actor, site, source, path: Path, **kw):
    b = import_file(s, actor, site, source, path.name, path.read_bytes(), **kw)
    s.flush()
    if b.status != "accepted":
        raise RuntimeError(f"Fixture import failed for {path.name}: {b.report}")
    return b


# ------------------------------------------------------------------ Fixture A
A_USERS = [
    ("admin@gini-synthetic.example", "Ada Admin", ["administrator"]),
    ("analyst@gini-synthetic.example", "Bea Analyst", ["billing_analyst"]),
    ("controller@gini-synthetic.example", "Cal Controller", ["finance_controller"]),
    ("controller2@gini-synthetic.example", "Cleo Controller", ["finance_controller"]),
    ("manager@gini-synthetic.example", "Max Manager", ["account_manager"]),
    ("supervisor@gini-synthetic.example", "Sam Supervisor", ["operations_supervisor"]),
]
A_CHARGES = [
    ("LINE_RX", "Rx pick line", "picking", "line", "pick_line"), ("LINE_OTC", "OTC pick line", "picking", "line", "pick_line"),
    ("LINE_CASES", "Case pick line", "picking", "line", "pick_line"), ("LINE_COOLER", "Cooler pick line", "picking", "line", "pick_line"),
    ("LINE_CAGE", "Cage pick line", "picking", "line", "pick_line"), ("LINE_VAULT", "Vault pick line", "picking", "line", "pick_line"),
    ("COLD_CHAIN_SURCHARGE", "Cold-chain surcharge", "surcharge", "line", "pick_line"), ("ORDER_FEE", "Order fee", "fee", "order", "order_completed"),
]
A_JOIN_PATH = {
    "pick_line": [{"from": "task_ref"}, {"table": "TASK_ORDER_LINE", "match": "TASK_ID", "take": "ORDER_LINE_ID"},
                  {"table": "ORDER_LINE", "match": "ORDER_LINE_ID", "take": "ORDER_ID"},
                  {"table": "ORDER_HEADER", "match": "ORDER_ID", "take": "CUSTOMER_CODE"}, {"identity_map": "customer_code"}],
    "order_completed": [{"from": "order_ref"}, {"table": "ORDER_HEADER", "match": "ORDER_ID", "take": "CUSTOMER_CODE"},
                        {"identity_map": "customer_code"}],
}
# Fixture configuration (not product defaults): makes the demo usable end to end.
A_SITE_SETTINGS = {"leakage.approval_threshold": "100", "leakage.back_billing_window_days": "90", "close.margin_range_low": "0",
                   "close.margin_range_high": "100", "drift.renewal_warning_days": "60"}
A_WORKTYPES = {"1": "PICK_RX", "2": "PICK_OTC", "3": "PICK_VAULT", "4": "PICK_CAGE", "5": "PICK_COOLER", "6": "PICK_CASES"}


def load_fixture_a(run=True, tenant_name="GiNi Synthetic Demo (Fixture A)"):
    tid, site_id, users = bootstrap_tenant(tenant_name, A_USERS, dict(
        code="DC100", name="Synthetic DC100", time_zone="America/Los_Angeles", currency_code="USD", data_label="Synthetic"))
    adm = Actor(tid, users["admin@gini-synthetic.example"])
    analyst = Actor(tid, users["analyst@gini-synthetic.example"])
    ctl = Actor(tid, users["controller@gini-synthetic.example"])
    s = make_session(tid)
    site = s.get(m.Site, site_id)
    admin.create_period(s, adm, site_id, "2026-W37", dt.date(2026, 9, 7), dt.date(2026, 9, 13), "week")
    admin.create_period(s, adm, site_id, "2026-W38", dt.date(2026, 9, 14), dt.date(2026, 9, 20), "week")
    for code, name, cat, uom, ev in A_CHARGES:
        admin.create_charge_code(s, adm, code, name, cat, uom, ev)
    admin.create_charge_code(s, adm, "VAS_RELABEL", "Relabeling (value-added service)", "vas", "unit", "vas_units", manual_capture=True)
    set_setting(s, adm, "customer_resolution.join_path", A_JOIN_PATH)
    set_setting(s, adm, "gini.worktype_activity", A_WORKTYPES)
    for k, v in TEST_TOLERANCES.items():
        set_setting(s, adm, k, v, site_id)
    for k, v in A_SITE_SETTINGS.items():
        set_setting(s, adm, k, v, site_id)
    set_setting(s, adm, "accounting.adapter", "file")
    s.commit()
    d = FIX / "fixture_a"
    order = [("gini_customer_master", "01_customer_master_M140.csv"), ("gini_material", "02_material.csv.gz"),
             ("gini_uom_conversion", "03_uom_conversion.csv"), ("gini_order_header", "04_order_header.csv.gz"),
             ("gini_order_line", "05_order_line.csv.gz"), ("gini_task_order_line", "06_task_order_line.csv.gz"),
             ("gini_ti102c", "07_TI102C_completed_picking.csv.gz"), ("gini_ti102", "08_TI102_open_picking.csv.gz"),
             ("gini_shipment", "09_shipment.csv"), ("gini_shipment_order", "10_shipment_order.csv.gz"),
             ("labor_order_time", "11_labor_order_time.csv.gz"), ("rate_card", "12_rate_card.csv"),
             ("invoice_lines", "13_invoice_lines_2026-W37_W38.csv")]
    for source, fname in order:
        _imp(s, adm if source != "invoice_lines" else analyst, site, source, d / fname, data_label="Synthetic")
        s.commit()
    # rate cards: author = administrator, approver = controller (segregation of duties)
    for v in s.execute(select(m.RateCardVersion).where(m.RateCardVersion.status == "draft")).scalars().all():
        contracts.submit(s, adm, v.id)
        contracts.approve(s, ctl, v.id)
    s.commit()
    _load_sla(s, adm, d / "14_sla_rules.json")
    s.commit()
    if run:
        from .services.run import run_period
        for p in s.execute(select(m.BillingPeriod).where(m.BillingPeriod.site_id == site_id).order_by(m.BillingPeriod.start_date)).scalars().all():
            run_period(s, ctl, p.id)
            s.commit()
    s.close()
    return tid, site_id, users


def _load_sla(s, actor, path: Path):
    customers = {c.code: c for c in s.execute(select(m.Customer)).scalars()}
    for x in json.loads(path.read_text()):
        c = customers.get(x["customer_id"].replace("cust-", ""))
        if c is None:
            continue
        s.add(m.SlaRule(tenant_id=actor.tenant_id, customer_id=c.id, milestone=x["milestone"], allowed_late_seconds=x["allowed_late_seconds"],
                        penalty_amount=str(x["penalty_amount"]), penalty_basis=x["penalty_basis"], cap_amount=str(x["cap_amount"]),
                        penalty_charge_code="SLA_PENALTY", valid_from=dt.date.fromisoformat(x["valid_from"]),
                        valid_to=dt.date.fromisoformat(x["valid_to"]) if x.get("valid_to") else None, contract_reference=x["contract_reference"]))


# ------------------------------------------------------------------ Fixture B
B_USERS = [
    ("admin@bsp-illustrative.example", "Ali Admin", ["administrator"]),
    ("analyst@bsp-illustrative.example", "Bo Analyst", ["billing_analyst"]),
    ("controller@bsp-illustrative.example", "Chen Controller", ["finance_controller"]),
    ("controller2@bsp-illustrative.example", "Cora Controller", ["finance_controller"]),
    ("manager@bsp-illustrative.example", "Mia Manager", ["account_manager"]),
    ("supervisor@bsp-illustrative.example", "Sol Supervisor", ["operations_supervisor"]),
]


def load_fixture_b(run=True, tenant_name="BSP Worked Example (Fixture B, illustrative)"):
    from .services import work

    tid, site_id, users = bootstrap_tenant(tenant_name, B_USERS, dict(
        code="BSP-1", name="BSP warehouse (illustrative)", time_zone="America/Chicago", currency_code="USD", data_label="Illustrative"))
    adm = Actor(tid, users["admin@bsp-illustrative.example"])
    analyst = Actor(tid, users["analyst@bsp-illustrative.example"])
    ctl = Actor(tid, users["controller@bsp-illustrative.example"])
    ctl2 = Actor(tid, users["controller2@bsp-illustrative.example"])
    sup = Actor(tid, users["supervisor@bsp-illustrative.example"])
    s = make_session(tid)
    site = s.get(m.Site, site_id)
    spec = json.loads((FIX / "fixture_b" / "setup.json").read_text())
    for p in spec["periods"]:
        admin.create_period(s, adm, site_id, p["label"], dt.date.fromisoformat(p["start"]), dt.date.fromisoformat(p["end"]), p["type"])
    for c in spec["charge_codes"]:
        admin.create_charge_code(s, adm, c["code"], c["name"], c["category"], c.get("uom"), c.get("event_type"), manual_capture=c.get("manual", False))
    for k, v in spec["settings"].items():
        set_setting(s, adm, k, v, None if SETTINGS[k]["scope"] == "tenant" else site_id)
    for k, v in TEST_TOLERANCES.items():
        set_setting(s, adm, k, v, site_id)
    s.commit()
    d = FIX / "fixture_b"
    _imp(s, adm, site, "gini_customer_master", d / "01_customer_master.csv", data_label="Illustrative")
    s.commit()
    for drv in spec["drivers"]:
        dd = admin.create_driver(s, adm, site_id, drv["category"], drv["driver_type"], drv["description"],
                                 dt.date.fromisoformat(spec["periods"][0]["start"]), data_label="Illustrative")
        admin.approve_driver(s, ctl, dd.id)
    s.commit()
    for source, fname in spec["imports"]:
        _imp(s, analyst if source == "invoice_lines" else adm, site, source, d / fname, data_label="Illustrative")
        s.commit()
    for v in s.execute(select(m.RateCardVersion).where(m.RateCardVersion.status == "draft")).scalars().all():
        contracts.submit(s, adm, v.id)
        contracts.approve(s, ctl, v.id)
    s.commit()
    period = s.execute(select(m.BillingPeriod).where(m.BillingPeriod.site_id == site_id)).scalars().first()
    customers = {c.code: c for c in s.execute(select(m.Customer)).scalars()}
    charges = {c.code: c for c in s.execute(select(m.ChargeCode)).scalars()}
    for e in spec["manual_entries"]:
        entry = work.submit_entry(s, sup, site_id, customers[e["customer"]].id, charges[e["charge"]].id, e["quantity"], e["uom"],
                                  dt.date.fromisoformat(e["date"]), None, None, e["note"])
        work.review_entry(s, analyst, entry.id, accept=True)
    s.commit()
    if run:
        from .services.run import run_period
        run_period(s, ctl, period.id)
        s.commit()
    s.close()
    return tid, site_id, users
