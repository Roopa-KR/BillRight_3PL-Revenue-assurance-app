"""Configuration from environment variables (and an optional .env file). No secrets in code."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE_VERSION = "1.0.0"

try:  # python-dotenv is optional; real deployments set environment variables directly
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
except ImportError:  # pragma: no cover
    pass


class Config:
    APP_ENV = os.environ.get("APP_ENV", "production")
    # Application role (no superuser, no BYPASSRLS): row-level security applies to every query.
    DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg://billright_app:billright_app@localhost/billright")
    # Owner role: migrations, tenant creation and fixture loading only.
    DATABASE_OWNER_URL = os.environ.get("DATABASE_OWNER_URL", "postgresql+psycopg://billright:billright@localhost/billright")
    SECRET_KEY = os.environ.get("SECRET_KEY")
    UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", str(ROOT / "var" / "uploads")))
    OIDC_ISSUER = os.environ.get("OIDC_ISSUER")
    OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID")
    OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET")
    OIDC_REDIRECT_URI = os.environ.get("OIDC_REDIRECT_URI")
    OIDC_SCOPES = os.environ.get("OIDC_SCOPES", "openid email profile")
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "1") == "1"

    @classmethod
    def is_development(cls) -> bool:
        return cls.APP_ENV == "development"
