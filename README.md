# GiNi BillRight

**Bill for all the work you do, at the right rate, and know what every customer really earns you.**

BillRight prices every billable 3PL warehouse activity against each customer's contract, compares what should have
been billed with what was invoiced, puts every gap in front of a person with its evidence for a recorded decision,
allocates the 3PL's operating expenses to customers with approved drivers, and shows each customer's revenue, cost,
site profit and margin. All money is calculated by deterministic Python with `decimal.Decimal`; nothing is invoiced,
credited or written off without a recorded human decision.

Release 1 (F1–F13) and the GA features (G1–G8) are implemented. See `docs/TRACEABILITY.md` for every requirement,
its code and its test, and `docs/TEST_REPORT.md` for measured results.

## Stack

Python 3.12+ · Flask/Jinja2 (server-rendered, small JS) · PostgreSQL 16 with row-level security · SQLAlchemy 2 ·
Alembic · openpyxl · ReportLab · Plotly.js bundled locally (`billright/web/static/plotly.min.js`, no CDN) · OIDC SSO.

```
billright/engine/    pure calculation (no Flask): money, resolution, rating, billable, leakage, allocation, labor, pnl, storage
billright/ingest/    source definitions, import centre with validation, bulk loading
billright/metrics/   the single metrics service used by pages, API and exports (P8)
billright/services/  workflows: contracts and rates, runs, leakage review, work capture, close, admin, exports
billright/ga/        G1–G8
billright/web/       Flask app, auth (OIDC + dev login), pages, charts, templates
billright/api/       JSON API /api/v1/...
billright/models/    SQLAlchemy models (section 6)
migrations/          Alembic: schema + RLS, append-only triggers, login lookup function
fixtures/            Fixture A (GiNi synthetic) and Fixture B (BSP worked example, illustrative) as import files
scripts/             fixture builders, data-contract generator, performance test
tests/               unit, acceptance, consistency, security, UI smoke, GA
```

## Set up

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# PostgreSQL: an owner role and an application role WITHOUT superuser/BYPASSRLS
createuser -s billright            # owner (or any role that owns the database)
psql -c "CREATE ROLE billright_app LOGIN PASSWORD 'billright_app' NOSUPERUSER NOBYPASSRLS"
createdb -O billright billright

cp .env.example .env               # set DATABASE_URL (app role), DATABASE_OWNER_URL (owner), SECRET_KEY, OIDC_*
python -m billright.cli migrate
```

## Run

```bash
# demo data: Fixture A (12 synthetic customers, 2 weeks) and Fixture B (BSP worked example, illustrative)
python -m billright.cli load-fixtures all          # ~1.5 minutes; loads through the normal import path and runs every period

APP_ENV=development python -m billright.cli serve --port 8000     # development login by e-mail
python -m billright.cli scheduler                                # G3 daily run (per site at alerts.daily_run_hour)
```

Demo sign-ins (development login only): `analyst@`, `controller@`, `controller2@`, `manager@`, `supervisor@`,
`admin@` followed by `gini-synthetic.example` (Fixture A) or `bsp-illustrative.example` (Fixture B).

Container: `docker compose up` (web + scheduler + PostgreSQL; run `docker compose run web python -m billright.cli migrate` once).

## Test

```bash
pytest                       # creates a fresh database 'billright_test', migrates, loads both fixtures, runs everything (~3.5 min)
BILLRIGHT_REUSE_DB=1 pytest  # reuse an already loaded test database while iterating
python scripts/perf_pilot.py # 3 months of pilot history: measured time written to docs/perf_result.json
```

Test database URLs can be changed with `BILLRIGHT_TEST_OWNER`, `BILLRIGHT_TEST_APP` and `BILLRIGHT_TEST_DB`.

## Rebuild the fixtures

```bash
python scripts/build_fixture_a.py /path/to/GiNi_Labor_Economics/data   # from the prototype's SQLite snapshots
python scripts/build_fixture_b.py
python scripts/gen_data_contract.py                                    # regenerate docs/DATA_CONTRACT.md
```

The reference sheet `tests/truth/out_billing_leakage.csv` is test truth only; a test asserts that no application
code references it.

## Documentation

`docs/DATA_CONTRACT.md` · `docs/RULE_CATALOG.md` · `docs/METRICS.md` · `docs/USER_GUIDE.md` · `docs/ADMIN_GUIDE.md` ·
`docs/TRACEABILITY.md` · `docs/TEST_REPORT.md` · `docs/FINAL_REPORT.md`
