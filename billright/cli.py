"""Command line: python -m billright.cli <command>

  migrate                      apply database migrations (owner connection)
  load-fixtures [a|b|all]      load Fixture A (synthetic) and/or B (illustrative) through the normal import path
  create-tenant NAME SITE_CODE SITE_NAME TZ CURRENCY ADMIN_EMAIL ADMIN_NAME
  run TENANT_ID PERIOD_ID      on-demand calculation run
  nightly                      G3 daily rating and rule run for every site whose alerts.daily_run_hour is set
  scheduler                    loop: fire 'nightly' per site at its configured local hour (container-friendly)
  verify TENANT_ID PERIOD_ID   re-run a closed period read-only and compare result hashes
  serve [--port 8000]          production WSGI server (waitress if installed, else Flask's)
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import models as m
from .config import ROOT, Config
from .db import make_session
from .services.common import Actor, get_setting


def _migrate():
    from alembic import command
    from alembic.config import Config as AC
    cfg = AC(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")


def _owner():
    return make_session(None, Config.DATABASE_OWNER_URL)


def _system_actor(tenant_id):
    return Actor(tenant_id, None)


def nightly(today: dt.datetime | None = None, force=False):
    from .ga.features import daily_run
    with _owner() as own:
        tenants = [t.id for t in own.execute(select(m.Tenant)).scalars()]
    for tid in tenants:
        s = make_session(tid)
        for site in s.execute(select(m.Site)).scalars().all():
            hour = get_setting(s, tid, "alerts.daily_run_hour", site.id)
            now = (today or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(site.time_zone))
            if hour is None or (not force and now.hour != hour):
                continue
            n = daily_run(s, _system_actor(tid), site.id, now.date())
            s.commit()
            logging.info("nightly tenant=%s site=%s new_alerts=%s", tid, site.code, n)
        s.close()


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format='{"t":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}')
    ap = argparse.ArgumentParser(prog="billright")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    lf = sub.add_parser("load-fixtures")
    lf.add_argument("which", nargs="?", default="all")
    ct = sub.add_parser("create-tenant")
    for a in ("name", "site_code", "site_name", "tz", "currency", "admin_email", "admin_name"):
        ct.add_argument(a)
    r = sub.add_parser("run")
    r.add_argument("tenant", type=int)
    r.add_argument("period", type=int)
    v = sub.add_parser("verify")
    v.add_argument("tenant", type=int)
    v.add_argument("period", type=int)
    sub.add_parser("nightly").add_argument("--force", action="store_true")
    sub.add_parser("scheduler")
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args(argv)

    if args.cmd == "migrate":
        _migrate()
    elif args.cmd == "load-fixtures":
        from .fixtures import load_fixture_a, load_fixture_b
        if args.which in ("a", "all"):
            print("Fixture A:", load_fixture_a())
        if args.which in ("b", "all"):
            print("Fixture B:", load_fixture_b())
    elif args.cmd == "create-tenant":
        from .fixtures import bootstrap_tenant
        print(bootstrap_tenant(args.name, [(args.admin_email, args.admin_name, ["administrator"])],
                               dict(code=args.site_code, name=args.site_name, time_zone=args.tz, currency_code=args.currency)))
    elif args.cmd in ("run", "verify"):
        from .services.run import run_period
        s = make_session(args.tenant)
        run = run_period(s, _system_actor(args.tenant), args.period, "verify" if args.cmd == "verify" else "on_demand")
        s.commit()
        print(run.id, run.status, run.message or "", run.counts)
        sys.exit(0 if run.status == "succeeded" else 1)
    elif args.cmd == "nightly":
        nightly(force=args.force)
    elif args.cmd == "scheduler":
        while True:
            nightly()
            time.sleep(3600 - time.time() % 3600 + 5)
    elif args.cmd == "serve":
        from .web import create_app
        app = create_app()
        try:
            from waitress import serve
            serve(app, host=args.host, port=args.port, threads=8)
        except ImportError:
            app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
