# Administrator guide

## Install and run

See `README.md` for the commands. Summary:

1. PostgreSQL 16 with two roles: an **owner** (runs migrations) and an **application role without SUPERUSER or
   BYPASSRLS** (`scripts/init_app_role.sql`). Row-level security only protects tenants when the app uses that role.
2. `python -m billright.cli migrate` (owner connection, `DATABASE_OWNER_URL`).
3. Configure SSO (`OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_REDIRECT_URI`); users sign in with
   the e-mail claim, which must match a BillRight user. `APP_ENV=development` enables an e-mail-only login for local
   use; never set it in production. Set `SECRET_KEY`.
4. `python -m billright.cli serve` (web) and `python -m billright.cli scheduler` (daily G3 run per site at
   `alerts.daily_run_hour`). Both run in the provided container (`Dockerfile`, `docker-compose.yml`) or as plain
   Python processes (hosting is open decision 1).

## Set up a tenant

`python -m billright.cli create-tenant "<Tenant>" <SITE_CODE> "<Site name>" <IANA time zone> <CURRENCY> <admin e-mail> "<Admin name>"`

Then, in **Admin**:

* **Periods** — weekly or monthly billing periods (no overlaps).
* **Charge codes** — code, name, category, default unit and the *event type* it prices (`pick_line`,
  `order_completed`, `receipt`, `storage`, or a manual type such as `vas_hours`). Tick *capturable on Work capture*
  for off-system services. Cost-based (cost plus / pass-through) and fixed-fee charges have no event type.
* **Users and scopes** — roles (billing analyst, finance controller, account manager, operations supervisor,
  administrator) and scope (whole site or selected customers). The administrator role configures only; it cannot
  approve rates or decide leakage.
* **Settings** — nothing has a built-in default. Required settings block the step that needs them and say which
  one to fill:
  * `customer_resolution.join_path` — **verify against the deployed GiNi schema before go-live (open decision 10).**
    Per event type, the chain from the operational record to the customer master, e.g.
    `{"pick_line":[{"from":"task_ref"},{"table":"TASK_ORDER_LINE","match":"TASK_ID","take":"ORDER_LINE_ID"},
    {"table":"ORDER_LINE","match":"ORDER_LINE_ID","take":"ORDER_ID"},{"table":"ORDER_HEADER","match":"ORDER_ID","take":"CUSTOMER_CODE"},
    {"identity_map":"customer_code"}]}`. CLIENTID is never used.
  * `gini.worktype_activity` — GiNi WORKTYPE → activity code.
  * `tolerance.L1..L7.amount|pct`, `leakage.approval_threshold`, `leakage.back_billing_window_days`,
    `labor.cost_basis`, `close.margin_range_low|high`, `drift.renewal_warning_days`, `storage.policy`,
    `accounting.adapter`, optional `margin_target.<CUSTOMER>`, `pilot.subscription_cost`, `invoice.source_system`.
* **Allocation drivers** — define one driver per expense category (type and how it is measured); a finance
  controller who did not define it approves it. Driver quantities are imported (`driver_quantities`) or entered on
  the drivers page; a blank customer is unassigned activity (idle, shared, training).
* **Audit log** — every create/update/approve/decide with before, after, who, when and reason; append-only (the
  database rejects updates and deletes).

## Data feeds

The **Data** page imports every source (see `docs/DATA_CONTRACT.md`): GiNi extracts (customer master M140,
TI102C, TI102, TI202, orders, order lines, task links, materials, unit conversions, shipments), invoices, employee
labor cost, labor time attribution, vendor and agency invoices, expense ledger lines, driver quantities, rate cards
(load as a draft version for approval) and daily inventory snapshots. Column mappings are saved per source. Each
import shows accepted and rejected rows with reasons; rejected rows download as CSV. Identical files are ignored;
a changed file for the same scope supersedes the earlier one (history kept).

## Operations

* **Runs**: *Run period* in the header, the CLI (`run`), or the scheduler. One run per site-period at a time
  (advisory lock). Runs log inputs, counts and duration as structured JSON.
* **Close**: closed periods are frozen. `python -m billright.cli verify <tenant> <period>` re-runs a closed period
  read-only and compares the result hash with the close run.
* **Backups**: standard PostgreSQL backups; uploaded photos live in `UPLOAD_DIR`.
* **Retention**: each run stores its rated events; keep close runs, prune superseded nightly runs if storage
  matters (not automated).
* **Adding a client portal (Year 2)**: add a role to `ROLE_PERMISSIONS` in `billright/security.py` with customer
  scope; every query already filters by scope.
