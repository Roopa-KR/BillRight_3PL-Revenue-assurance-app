"""Test set-up: a fresh PostgreSQL test database, migrated, with Fixture A and Fixture B loaded through the normal
import path once per test session."""
from __future__ import annotations

import os
import re
import subprocess

import pytest

TEST_DB = os.environ.get("BILLRIGHT_TEST_DB", "billright_test")
OWNER = os.environ.get("BILLRIGHT_TEST_OWNER", "postgresql+psycopg://billright:billright@localhost")
APP = os.environ.get("BILLRIGHT_TEST_APP", "postgresql+psycopg://billright_app:billright_app@localhost")
os.environ["DATABASE_URL"] = f"{APP}/{TEST_DB}"
os.environ["DATABASE_OWNER_URL"] = f"{OWNER}/{TEST_DB}"
os.environ["APP_ENV"] = "development"
os.environ.setdefault("UPLOAD_DIR", "/tmp/billright_test_uploads")


def _recreate_db():
    import psycopg
    url = OWNER.replace("postgresql+psycopg", "postgresql") + "/postgres"
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {TEST_DB}")


@pytest.fixture(scope="session")
def world():
    """Fresh database with both fixtures. Returns ids. BILLRIGHT_REUSE_DB=1 reuses an already loaded test database."""
    if os.environ.get("BILLRIGHT_REUSE_DB") == "1":
        from billright.config import Config
        from billright.db import make_session
        from sqlalchemy import select
        from billright import models as m
        own = make_session(None, Config.DATABASE_OWNER_URL)
        ts = own.execute(select(m.Tenant).order_by(m.Tenant.id)).scalars().all()
        if len(ts) >= 2:
            out = {}
            for key, t in (("a", ts[0]), ("b", ts[1])):
                site = own.execute(select(m.Site).where(m.Site.tenant_id == t.id)).scalars().first()
                users = {u.email: u.id for u in own.execute(select(m.AppUser).where(m.AppUser.tenant_id == t.id)).scalars()}
                out[key] = dict(tenant=t.id, site=site.id, users=users)
            own.close()
            return out
        own.close()
    _recreate_db()
    from billright.cli import _migrate
    _migrate()
    from billright.fixtures import load_fixture_a, load_fixture_b
    a = load_fixture_a()
    b = load_fixture_b()
    return dict(a=dict(tenant=a[0], site=a[1], users=a[2]), b=dict(tenant=b[0], site=b[1], users=b[2]))


@pytest.fixture()
def session_for():
    from billright.db import make_session
    opened = []

    def make(tenant_id):
        s = make_session(tenant_id)
        opened.append(s)
        return s
    yield make
    for s in opened:
        s.rollback()
        s.close()


@pytest.fixture(scope="session")
def app(world):
    from billright.web import create_app
    a = create_app(testing=True)
    a.config["SYNC_RUNS"] = True
    return a


class Client:
    def __init__(self, app, email):
        self.c = app.test_client()
        r = self.c.get("/login")
        self.token = re.search(rb'name="csrf_token" value="([^"]+)"', r.data).group(1).decode()
        r = self.c.post("/login", data={"email": email, "csrf_token": self.token})
        assert r.status_code == 302, r.data[:300]
        r = self.c.get("/")
        m = re.search(rb'name="csrf_token" value="([^"]+)"', r.data)
        if m:
            self.token = m.group(1).decode()

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def post(self, url, data=None, **kw):
        data = dict(data or {}, csrf_token=self.token)
        return self.c.post(url, data=data, **kw)


@pytest.fixture()
def client(app):
    return lambda email: Client(app, email)


def actor(world, which, email_prefix):
    from billright.services.common import Actor
    w = world[which]
    uid = next(v for k, v in w["users"].items() if k.startswith(email_prefix + "@"))
    return Actor(w["tenant"], uid)
