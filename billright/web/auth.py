"""Authentication: OIDC single sign-on (authorization-code flow with PKCE); a local development login only
when APP_ENV=development. The signed-in user's tenant, roles, permissions and scope are loaded per request."""
from __future__ import annotations

import base64
import functools
import hashlib
import secrets
import time

import jwt
import requests
from flask import Blueprint, abort, current_app, g, redirect, render_template, request, session, url_for

from .. import models as m
from ..config import Config
from ..db import find_login, make_session
from ..security import ROLE_LABELS, load_scope, permissions_for, roles_of

bp = Blueprint("auth", __name__)
_discovery_cache: dict = {}


def _discovery():
    if not Config.OIDC_ISSUER:
        return None
    if "doc" not in _discovery_cache or _discovery_cache["t"] < time.time() - 3600:
        doc = requests.get(Config.OIDC_ISSUER.rstrip("/") + "/.well-known/openid-configuration", timeout=10).json()
        _discovery_cache.update(doc=doc, t=time.time(), jwks=jwt.PyJWKClient(doc["jwks_uri"]))
    return _discovery_cache


def _start_session(tenant_id: int, user_id: int):
    session.clear()
    session["tenant_id"] = tenant_id
    session["user_id"] = user_id
    session["csrf"] = secrets.token_urlsafe(32)
    session.permanent = True


@bp.route("/login", methods=["GET", "POST"])
def login():
    dev = Config.is_development() or current_app.config.get("TESTING")
    error = None
    if request.method == "POST":
        if not dev:
            abort(404)
        email = (request.form.get("email") or "").strip().lower()
        found = find_login(email)
        if len(found) == 1:
            _start_session(found[0][0], found[0][1])
            return redirect(url_for("pages.home"))
        error = "No active user with that e-mail." if not found else "That e-mail exists in more than one tenant; use SSO."
    return render_template("login.html", dev=dev, sso=bool(Config.OIDC_ISSUER), error=error)


@bp.route("/login/sso")
def sso_start():
    disc = _discovery()
    if disc is None:
        abort(404)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    session.update(oidc_state=state, oidc_nonce=nonce, oidc_verifier=verifier)
    params = dict(response_type="code", client_id=Config.OIDC_CLIENT_ID, redirect_uri=Config.OIDC_REDIRECT_URI, scope=Config.OIDC_SCOPES,
                  state=state, nonce=nonce, code_challenge=challenge, code_challenge_method="S256")
    return redirect(requests.Request("GET", disc["doc"]["authorization_endpoint"], params=params).prepare().url)


@bp.route("/login/callback")
def sso_callback():
    disc = _discovery()
    if disc is None or request.args.get("state") != session.get("oidc_state"):
        abort(400, "Sign-in state mismatch; start again.")
    tok = requests.post(disc["doc"]["token_endpoint"], timeout=10, data=dict(
        grant_type="authorization_code", code=request.args.get("code"), redirect_uri=Config.OIDC_REDIRECT_URI,
        client_id=Config.OIDC_CLIENT_ID, client_secret=Config.OIDC_CLIENT_SECRET, code_verifier=session.get("oidc_verifier"))).json()
    key = disc["jwks"].get_signing_key_from_jwt(tok["id_token"]).key
    claims = jwt.decode(tok["id_token"], key, algorithms=["RS256", "ES256"], audience=Config.OIDC_CLIENT_ID, issuer=disc["doc"]["issuer"])
    if claims.get("nonce") != session.get("oidc_nonce"):
        abort(400, "Sign-in nonce mismatch.")
    if claims.get("email_verified") is False:
        abort(403)
    found = find_login(claims.get("email", ""))
    if len(found) != 1:
        abort(403)
    _start_session(found[0][0], found[0][1])
    return redirect(url_for("pages.home"))


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


class Current:
    def __init__(self, s, tenant_id, user_id):
        self.s = s
        self.tenant_id = tenant_id
        self.user = s.get(m.AppUser, user_id)
        if self.user is None or not self.user.active:
            raise PermissionError
        self.roles = roles_of(s, user_id)
        self.perms = permissions_for(self.roles)
        self.scope = load_scope(s, tenant_id, user_id)
        self.role_labels = [ROLE_LABELS[r] for r in self.roles]

    def can(self, perm: str) -> bool:
        return perm in self.perms

    @property
    def actor(self):
        from ..services.common import Actor
        return Actor(self.tenant_id, self.user.id, frozenset(self.roles))


def load_current():
    if "user_id" not in session:
        return None
    s = make_session(session["tenant_id"])
    g.s = s
    try:
        g.cur = Current(s, session["tenant_id"], session["user_id"])
    except PermissionError:
        session.clear()
        return None
    return g.cur


def login_required(perm: str | None = None):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            cur = load_current()
            if cur is None:
                if request.path.startswith("/api/"):
                    return {"error": "Not signed in"}, 401
                return redirect(url_for("auth.login"))
            if perm and not any(cur.can(p) for p in perm.split("|")):
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco
