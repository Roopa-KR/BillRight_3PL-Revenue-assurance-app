"""Bulk insert through the session's own connection (same transaction, same RLS tenant).

PostgreSQL does not allow COPY FROM into a table with row-level security, so rows are COPYed into a
temporary staging table and moved with INSERT ... SELECT, which the RLS ``WITH CHECK`` policy still verifies.
"""
from __future__ import annotations

import itertools
import json

_seq = itertools.count()


def copy_rows(s, table: str, columns, rows, json_cols=frozenset()):
    if not rows:
        return 0
    conn = s.connection().connection.driver_connection
    cols = ", ".join(columns)
    tmp = f"_bulk_{table}_{next(_seq)}"
    with conn.cursor() as cur:
        cur.execute(f"CREATE TEMP TABLE {tmp} (LIKE {table} INCLUDING DEFAULTS) ON COMMIT DROP")
        with cur.copy(f"COPY {tmp} ({cols}) FROM STDIN") as cp:
            for r in rows:
                if json_cols:
                    r = tuple(json.dumps(v, default=str) if (i in json_cols and not isinstance(v, str) and v is not None) else v
                              for i, v in enumerate(r))
                cp.write_row(r)
        cur.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM {tmp}")
        cur.execute(f"DROP TABLE {tmp}")
    return len(rows)
