"""Database engine and sessions.

Tenant isolation uses ONE mechanism: PostgreSQL row-level security. Every transaction opened through
``session_scope`` / the Flask request session runs ``set_config('app.tenant_id', <id>, true)`` first,
and the RLS policies created by the migration compare each row's ``tenant_id`` with that setting.
A session without a tenant sets ``-1`` and therefore sees no tenant data.
"""
from __future__ import annotations

import contextlib
import contextvars

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .config import Config

current_tenant: contextvars.ContextVar[int | None] = contextvars.ContextVar("current_tenant", default=None)

_engines: dict[str, object] = {}


def get_engine(url: str | None = None):
    url = url or Config.DATABASE_URL
    if url not in _engines:
        _engines[url] = create_engine(url, pool_pre_ping=True, future=True)
    return _engines[url]


def _set_tenant(session, transaction, connection):
    tid = session.info.get("tenant_id", current_tenant.get())
    connection.execute(text("select set_config('app.tenant_id', :t, true)"), {"t": str(tid if tid is not None else -1)})


def make_session(tenant_id: int | None = None, url: str | None = None) -> Session:
    s = sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)()
    s.info["tenant_id"] = tenant_id
    event.listen(s, "after_begin", _set_tenant)
    return s


@contextlib.contextmanager
def session_scope(tenant_id: int | None = None, url: str | None = None):
    s = make_session(tenant_id, url)
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def find_login(email: str):
    """Sign-in lookup before the tenant is known: the security-definer function returns only
    (tenant_id, user_id) for an active e-mail address."""
    with get_engine().connect() as c:
        rows = c.execute(text("select tenant_id, user_id from billright_find_user(:e)"), {"e": email.lower()}).all()
    return rows
