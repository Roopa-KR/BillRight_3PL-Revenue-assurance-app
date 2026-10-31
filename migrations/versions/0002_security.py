"""Row-level tenant isolation, append-only audit log, immutable snapshots, login lookup.

Revision ID: 0002
Revises: 0001
"""
from alembic import op
from billright.models import Base

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

APP_ROLE = "billright_app"
IMMUTABLE = ["audit_log", "source_snapshot", "activity_event", "import_rejected_row"]


def upgrade():
    op.execute(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{APP_ROLE}') THEN CREATE ROLE {APP_ROLE} LOGIN; END IF; END $$;")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    op.execute(f"REVOKE UPDATE, DELETE ON alembic_version FROM {APP_ROLE}")
    for t in IMMUTABLE:
        op.execute(f"REVOKE UPDATE, DELETE ON {t} FROM {APP_ROLE}")
    op.execute("""
        CREATE OR REPLACE FUNCTION billright_forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END $$;""")
    for t in IMMUTABLE:
        op.execute(f"CREATE TRIGGER {t}_append_only BEFORE UPDATE OR DELETE ON {t} FOR EACH ROW EXECUTE FUNCTION billright_forbid_change()")
    # one policy per tenant table, driven by the app.tenant_id transaction setting
    tenant_tables = [t.name for t in Base.metadata.sorted_tables if "tenant_id" in t.c]
    for t in tenant_tables:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY tenant_isolation ON {t} TO {APP_ROLE}
            USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::int)
            WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::int)""")
    op.execute("ALTER TABLE tenant ENABLE ROW LEVEL SECURITY")
    op.execute(f"""CREATE POLICY tenant_self ON tenant TO {APP_ROLE}
        USING (id = nullif(current_setting('app.tenant_id', true), '')::int)""")
    # The owner keeps full access for migrations and maintenance.
    for t in tenant_tables + ["tenant"]:
        op.execute(f"CREATE POLICY owner_all ON {t} TO CURRENT_USER USING (true) WITH CHECK (true)")
    # Login lookup: the only cross-tenant read; returns ids for an e-mail.
    op.execute("""
        CREATE OR REPLACE FUNCTION billright_find_user(p_email text)
        RETURNS TABLE(tenant_id int, user_id int) LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
          SELECT u.tenant_id, u.id FROM app_user u WHERE lower(u.email) = lower(p_email) AND u.active $$;""")
    op.execute(f"REVOKE ALL ON FUNCTION billright_find_user(text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION billright_find_user(text) TO {APP_ROLE}")


def downgrade():
    tenant_tables = [t.name for t in Base.metadata.sorted_tables if "tenant_id" in t.c]
    for t in tenant_tables + ["tenant"]:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
        op.execute(f"DROP POLICY IF EXISTS owner_all ON {t}")
        op.execute(f"DROP POLICY IF EXISTS tenant_self ON {t}")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
    for t in IMMUTABLE:
        op.execute(f"DROP TRIGGER IF EXISTS {t}_append_only ON {t}")
    op.execute("DROP FUNCTION IF EXISTS billright_find_user(text)")
    op.execute("DROP FUNCTION IF EXISTS billright_forbid_change()")
