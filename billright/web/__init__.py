"""Flask application factory."""
from __future__ import annotations

import datetime as dt
import logging
import secrets
from decimal import ROUND_HALF_UP, Decimal

from flask import Flask, abort, g, redirect, render_template, request, session, url_for

from ..config import Config
from ..services.common import BusinessError


def fmt_money(v, places=2, dash="Not available"):
    if v is None:
        return dash
    d = Decimal(str(v)).quantize(Decimal(1) if places == 0 else Decimal("0.01"), rounding=ROUND_HALF_UP)
    s = f"{abs(d):,.{places}f}"
    return f"−${s}" if d < 0 else f"${s}"


def fmt_pct(v, na="Not available"):
    if v is None:
        return na
    d = Decimal(str(v)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"−{abs(d)}%" if d < 0 else f"{d}%"


def fmt_ratio_pct(v, na="Not available"):
    return na if v is None else fmt_pct(Decimal(str(v)) * 100)


def fmt_date(v):
    if v is None:
        return "—"
    if isinstance(v, str):
        try:
            v = dt.datetime.fromisoformat(v)
        except ValueError:
            return v
    if isinstance(v, dt.datetime):
        return f"{v.day} {v:%b %Y %H:%M}"
    return f"{v.day} {v:%b %Y}"


def fmt_qty(v):
    if v is None:
        return "—"
    d = Decimal(str(v))
    if d == d.to_integral_value():
        return f"{int(d):,}"
    return f"{d.normalize():,f}"


def create_app(testing: bool = False) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    secret = Config.SECRET_KEY
    if not secret:
        if not (Config.is_development() or testing):
            raise RuntimeError("SECRET_KEY must be set outside development")
        secret = secrets.token_hex(32)
    app.config.update(SECRET_KEY=secret, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=Config.SESSION_COOKIE_SECURE and not Config.is_development() and not testing,
                      MAX_CONTENT_LENGTH=200 * 1024 * 1024, TESTING=testing)
    logging.basicConfig(level=logging.INFO, format='{"t":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}')
    app.jinja_env.filters.update(money=fmt_money, money0=lambda v: fmt_money(v, 0), pct=fmt_pct, ratio_pct=fmt_ratio_pct, date=fmt_date, qty=fmt_qty)
    app.jinja_env.globals.update(csrf_token=csrf_token)

    from . import auth, pages
    from ..api import v1

    app.register_blueprint(auth.bp)
    app.register_blueprint(pages.bp)
    app.register_blueprint(v1.bp)

    @app.before_request
    def _csrf():
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
                abort(400, "Missing or invalid CSRF token. Reload the page and try again.")

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline' 'unsafe-eval'")
        return resp

    @app.teardown_request
    def _close(exc):
        s = g.pop("s", None)
        if s is not None:
            if exc is not None:
                s.rollback()
            s.close()

    from ..metrics.service import NotAllowed

    @app.errorhandler(NotAllowed)
    def _not_allowed(e):
        if request.path.startswith("/api/"):
            return {"error": "Not found"}, 404
        return render_template("error.html", message="Not found or not in your scope.", code=404), 404

    @app.errorhandler(BusinessError)
    def _business(e):
        if request.path.startswith("/api/"):
            return {"error": str(e)}, 409
        return render_template("error.html", message=str(e), code=409), 409

    @app.errorhandler(403)
    def _forbidden(e):
        if request.path.startswith("/api/"):
            return {"error": "Forbidden"}, 403
        return render_template("error.html", message="You do not have access to this page or record.", code=403), 403

    @app.errorhandler(404)
    def _nf(e):
        if request.path.startswith("/api/"):
            return {"error": "Not found"}, 404
        return render_template("error.html", message="Not found.", code=404), 404

    @app.errorhandler(400)
    def _bad(e):
        if request.path.startswith("/api/"):
            return {"error": getattr(e, "description", "Bad request")}, 400
        return render_template("error.html", message=getattr(e, "description", "Bad request"), code=400), 400

    @app.route("/healthz")
    def health():
        return {"ok": True}

    return app


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]
