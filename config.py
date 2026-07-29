import os
from pathlib import Path


class Config:
    """Application configuration, overridable through environment variables."""

    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-before-deployment")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        f"sqlite:///{(Path(__file__).parent / 'instance' / 'expenses.db').as_posix()}",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_EXPENSE_AMOUNT = 1_000_000
    # Used only to create the first account when the users table is empty.
    INITIAL_ADMIN_ID = os.environ.get("INITIAL_ADMIN_ID", "9561662180")
    INITIAL_ADMIN_PASSWORD = os.environ.get("INITIAL_ADMIN_PASSWORD", "sachin")
