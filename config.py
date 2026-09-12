import os
from pathlib import Path


class Config:
    """Application configuration, overridable through environment variables."""

    SECRET_KEY = os.environ.get("SECRET_KEY")
    PRODUCTION = os.environ.get("FLASK_ENV", "development").lower() == "production"
    CSRF_ENABLED = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = PRODUCTION
    SESSION_COOKIE_NAME = "expense_splitter_session"
    PREFERRED_URL_SCHEME = "https" if PRODUCTION else "http"
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        f"sqlite:///{(Path(__file__).parent / 'instance' / 'expenses.db').as_posix()}",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_EXPENSE_AMOUNT = 1_000_000
    # Optional explicit bootstrap account, never automatically an administrator.
    INITIAL_ADMIN_ID = os.environ.get("INITIAL_ADMIN_ID")
    INITIAL_ADMIN_NAME = os.environ.get("INITIAL_ADMIN_NAME", "Administrator")
    INITIAL_ADMIN_PASSWORD = os.environ.get("INITIAL_ADMIN_PASSWORD")
