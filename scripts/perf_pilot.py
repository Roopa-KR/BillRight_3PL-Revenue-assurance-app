"""Performance test (12.2): process 3 months (13 weekly periods) of pilot history.

Builds 13 weeks of GiNi extracts by shifting Fixture A's two weeks back in time (synthetic), loads them through
the normal import path into a fresh tenant, rates and reconciles every period, and reports the measured time.
Usage: python scripts/perf_pilot.py   (uses DATABASE_URL / DATABASE_OWNER_URL)
"""
import csv
import datetime as dt
import gzip
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from billright import models as m  # noqa: E402
from billright.db import make_session  # noqa: E402
from billright.fixtures import A_CHARGES, A_JOIN_PATH, A_SITE_SETTINGS, A_WORKTYPES, TEST_TOLERANCES, bootstrap_tenant  # noqa: E402
from billright.ingest.importer import import_file  # noqa: E402
from billright.services import admin, contracts  # noqa: E402
from billright.services.common import Actor, set_setting  # noqa: E402
from billright.services.run import run_period  # noqa: E402

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "fixture_a"
W37 = dt.date(2026, 9, 7)
WEEKS = 13


def rows(name):
    p = FIX / name
    data = gzip.decompress(p.read_bytes()) if p.suffix == ".gz" else p.read_bytes()
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def out(header, rs):
    b = io.StringIO()
    w = csv.DictWriter(b, fieldnames=header)
    w.writeheader()
    w.writerows(rs)
    return b.getvalue().encode()


def shift_ts(v, days):
    if not v:
        return v
    return (dt.datetime.fromisoformat(v) + dt.timedelta(days=days)).isoformat()


def main():
    t0 = time.monotonic()
    tid, site_id, users = bootstrap_tenant(f"Pilot performance {dt.datetime.now():%H%M%S}", [
        ("perf-admin@perf.example", "Perf Admin", ["administrator"]), ("perf-ctl@perf.example", "Perf Controller", ["finance_controller"])],
        dict(code="PERF", name="Pilot performance (synthetic)", time_zone="America/Los_Angeles", currency_code="USD", data_label="Synthetic"))
    adm, ctl = Actor(tid, users["perf-admin@perf.example"]), Actor(tid, users["perf-ctl@perf.example"])
    s = make_session(tid)
    site = s.get(m.Site, site_id)
    first = W37 - dt.timedelta(days=7 * (WEEKS - 2))
    labels = {}
    for k in range(WEEKS):
        start = first + dt.timedelta(days=7 * k)
        label = f"P{k + 1:02d}"
        labels[start] = label
        admin.create_period(s, adm, site_id, label, start, start + dt.timedelta(days=6), "week")
    for code, name, cat, uom, ev in A_CHARGES:
        admin.create_charge_code(s, adm, code, name, cat, uom, ev)
    set_setting(s, adm, "customer_resolution.join_path", A_JOIN_PATH)
    set_setting(s, adm, "gini.worktype_activity", A_WORKTYPES)
    for k, v in {**TEST_TOLERANCES, **A_SITE_SETTINGS}.items():
        set_setting(s, adm, k, v, site_id)
    s.commit()
    for src, f in [("gini_customer_master", "01_customer_master_M140.csv"), ("gini_material", "02_material.csv.gz"),
                   ("gini_uom_conversion", "03_uom_conversion.csv"), ("rate_card", "12_rate_card.csv")]:
        import_file(s, adm, site, src, f, (FIX / f).read_bytes())
        s.commit()
    for v in s.query(m.RateCardVersion).filter(m.RateCardVersion.status == "draft").all():
        contracts.submit(s, adm, v.id)
        contracts.approve(s, ctl, v.id)
    s.commit()
    oh, ol, tol, ti, inv = rows("04_order_header.csv.gz"), rows("05_order_line.csv.gz"), rows("06_task_order_line.csv.gz"), \
        rows("07_TI102C_completed_picking.csv.gz"), rows("13_invoice_lines_2026-W37_W38.csv")
    t_load = time.monotonic()
    copies = WEEKS // 2 + 1  # each copy is two weeks shifted back by 14 days; the oldest copy's first week falls before P01
    for c in range(copies):
        days = -14 * (copies - 1 - c)
        suf = f"-c{c}"
        sh = lambda v: shift_ts(v, days)  # noqa: E731
        o = [dict(r, ORDER_ID=r["ORDER_ID"] + suf, RELEASED_AT=sh(r["RELEASED_AT"]), COMPLETED_AT=sh(r["COMPLETED_AT"]), CUTOFF_AT=sh(r["CUTOFF_AT"])) for r in oh]
        lines = [dict(r, ORDER_LINE_ID=r["ORDER_LINE_ID"] + suf, ORDER_ID=r["ORDER_ID"] + suf) for r in ol]
        links = [dict(r, TASK_ID=r["TASK_ID"] + suf, ORDER_LINE_ID=r["ORDER_LINE_ID"] + suf) for r in tol]
        tasks = [dict(r, TI102ID=r["TI102ID"] + suf, CREATEDATETIME=sh(r["CREATEDATETIME"]), COMPLETED_AT=sh(r["COMPLETED_AT"])) for r in ti]
        for src, name, data, header in [("gini_order_header", f"oh{suf}.csv", o, list(oh[0])), ("gini_order_line", f"ol{suf}.csv", lines, list(ol[0])),
                                        ("gini_task_order_line", f"tol{suf}.csv", links, list(tol[0])), ("gini_ti102c", f"ti{suf}.csv", tasks, list(ti[0]))]:
            b = import_file(s, adm, site, src, name, out(header, data))
            s.commit()
        inv_rows = []
        for r in inv:
            pstart = (W37 if r["PERIOD"] == "2026-W37" else W37 + dt.timedelta(days=7)) + dt.timedelta(days=days)
            if pstart not in labels:
                continue
            inv_rows.append(dict(r, INVOICE_NUMBER=r["INVOICE_NUMBER"] + suf, PERIOD=labels[pstart]))
        if inv_rows:
            import_file(s, ctl, site, "invoice_lines", f"inv{suf}.csv", out(list(inv[0]), inv_rows))
            s.commit()
    t_runs = time.monotonic()
    per = []
    for p in s.query(m.BillingPeriod).filter(m.BillingPeriod.site_id == site_id).order_by(m.BillingPeriod.start_date).all():
        r0 = time.monotonic()
        run = run_period(s, ctl, p.id)
        s.commit()
        per.append((p.label, run.status, run.counts.get("events"), run.counts.get("findings"), round(time.monotonic() - r0, 1)))
    total = time.monotonic() - t0
    events = s.query(m.ActivityEvent).filter(m.ActivityEvent.site_id == site_id).count()
    result = dict(weeks=WEEKS, activity_events=events, setup_seconds=round(t_load - t0, 1), import_seconds=round(t_runs - t_load, 1),
                  run_seconds=round(time.monotonic() - t_runs, 1), total_seconds=round(total, 1), total_minutes=round(total / 60, 2),
                  target_minutes=15, met=total < 900, periods=per)
    print(json.dumps(result, indent=1))
    (Path(__file__).resolve().parents[1] / "docs" / "perf_result.json").write_text(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
