"""Alembic environment: migrations run on the OWNER connection (DATABASE_OWNER_URL)."""
from alembic import context
from sqlalchemy import create_engine

from billright.config import Config
from billright.models import Base

target_metadata = Base.metadata


def run_migrations_online():
    url = context.config.get_main_option("sqlalchemy.url") or Config.DATABASE_OWNER_URL
    engine = create_engine(url)
    with engine.begin() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        context.run_migrations()


def run_migrations_offline():
    context.configure(url=Config.DATABASE_OWNER_URL, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
