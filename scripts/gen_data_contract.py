"""Generate docs/DATA_CONTRACT.md from the source definitions the importer validates against (single source)."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from billright.ingest.sources import SOURCE_TYPES  # noqa: E402
from billright.services.common import SETTINGS  # noqa: E402
from billright.ga.features import FileAdapter  # noqa: E402

REFRESH = {"gini": "Scheduled read-only extract from GiNi (MySQL), daily before the G3 run; or upload", "billing": "Per invoice run (CSV/Excel upload; API adapter later)",
           "cost": "Monthly / per period, before close (CSV/Excel upload)", "contracts": "On contract change (loads a draft version for approval)",
           "storage": "Daily snapshot (G1)"}
out = ["# Data contract", "",
       "Generated from `billright/ingest/sources.py` (the definitions the importer validates against) by `scripts/gen_data_contract.py`.", "",
       "## Rules for every source", "",
       "* Formats: CSV (UTF-8, header row), CSV.GZ or XLSX (first sheet). Column names match the canonical names below; a saved column mapping per source can rename file columns.",
       "* Every file becomes an immutable import batch (SHA-256 file hash, row count, user, time, source type). An identical file for the same source and site is a no-op.",
       "* A changed file with the same *scope* supersedes the earlier batch; the old rows stay stored. Default scope: the periods in the file (period-scoped sources) or the file name stem.",
       "* Validation per row: required fields, types (decimal, integer, ISO date, ISO 8601 date-time, true/false, JSON), duplicate keys in the file, unknown customer codes "
       "(customer register / identity map), unknown charge codes, unknown or closed periods, negative quantities. Rejected rows are listed with their reason and can be downloaded.",
       "* Event sources create billable events only for rows whose service date lies in an open billing period; other rows are kept as reference snapshots and counted in the report.",
       "* Timestamps without an offset are read in the site time zone; service dates are local site dates.",
       "* **CLIENTID** in GiNi extracts identifies the GiNi tenant (e.g. BSP / SYN). It is stored but never used to identify the 3PL customer. "
       "Customers are resolved through the join path in setting `customer_resolution.join_path` (open decision 10: verify it against the deployed GiNi schema).", ""]
for code, st in SOURCE_TYPES.items():
    out += [f"## {st.label}", "", f"Source type `{code}` · stored as `{st.source_table}` snapshots · refresh: {REFRESH.get(st.group, '')}", ""]
    if st.description:
        out += [st.description, ""]
    for n in st.notes:
        out += [f"> {n}", ""]
    extra = []
    if st.period_field:
        extra.append(f"period-scoped by `{st.period_field}` (must be an open period label)")
    if st.events:
        extra.append(f"creates `{st.events}` events dated by `{st.date_field}`")
    if st.customer_field:
        extra.append(f"`{st.customer_field}` must resolve through the identity map")
    if st.charge_field:
        extra.append(f"`{st.charge_field}` must be a defined charge code")
    out += [f"Record key: `{', '.join(st.key)}`" + ("; " + "; ".join(extra) if extra else "") + ".", "",
            "| Field | Type | Required | Validation |", "|---|---|---|---|"]
    for f in st.fields:
        out.append(f"| `{f.name}` | {f.type} | {'yes' if f.required else ''} | {'not negative' if f.non_negative else ''} {f.description} |")
    out.append("")
out += ["## Accounting export file (G8, `file` adapter)", "", "One CSV line per approved adjustment or credit note:", "",
        "| Column | Meaning |", "|---|---|"]
desc = {"DOCUMENT_TYPE": "invoice_adjustment or credit_note", "CUSTOMER_CODE": "3PL customer", "ORIGINAL_INVOICE": "invoice the gap refers to, or 'New invoice line'",
        "CHARGE_CODE": "charge code", "QUANTITY": "billable − billed quantity", "UNIT_PRICE": "contract unit price", "AMOUNT": "gap (negative = credit)",
        "CURRENCY": "site currency", "PERIOD": "period label", "LEAKAGE_ITEM_ID": "LKG-<id>; put it in ADJUSTMENT_REF of the invoice line that bills it",
        "REASON": "rule and the deterministic sentence", "APPROVER": "approver, or 'Below threshold; decided by …'"}
out += [f"| `{c}` | {desc[c]} |" for c in FileAdapter.COLUMNS]
out += ["", "## Settings (no built-in defaults)", "", "| Setting | Type | Required for | Description |", "|---|---|---|---|"]
for k, v in SETTINGS.items():
    out.append(f"| `{k}` | {v['type']} | {v['required'] or 'optional'} | {v['desc']} |")
out.append("| `margin_target.<CUSTOMER_CODE>` | decimal | optional | Margin target % per customer (open decision 9) |")
Path(__file__).resolve().parents[1].joinpath("docs", "DATA_CONTRACT.md").write_text("\n".join(out) + "\n")
print("written")
