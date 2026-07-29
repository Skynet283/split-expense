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
