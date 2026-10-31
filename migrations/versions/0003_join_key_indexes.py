"""Partial expression indexes on the GiNi join keys used by customer resolution and evidence drill-down,
so a run's lookups do not scale with the size of the snapshot history.

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

KEYS = [("TASK_ORDER_LINE", "TASK_ID"), ("TASK_ORDER_LINE", "ORDER_LINE_ID"), ("ORDER_LINE", "ORDER_LINE_ID"), ("ORDER_LINE", "ORDER_ID"),
        ("ORDER_HEADER", "ORDER_ID"), ("SHIPMENT_ORDER", "ORDER_ID"), ("LABOR_ORDER_TIME", "TI102X3ID"), ("MATERIAL", "MATERIAL_ID")]


def upgrade():
    for table, key in KEYS:
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_snap_{table.lower()}_{key.lower()} ON source_snapshot ((payload->>'{key}'), import_batch_id) "
                   f"WHERE source_table = '{table}'")
    op.execute("CREATE INDEX IF NOT EXISTS ix_event_task_ref ON activity_event (task_ref)")


def downgrade():
    for table, key in KEYS:
        op.execute(f"DROP INDEX IF EXISTS ix_snap_{table.lower()}_{key.lower()}")
    op.execute("DROP INDEX IF EXISTS ix_event_task_ref")
