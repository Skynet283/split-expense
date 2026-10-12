import secrets
import hmac

from flask import Flask, abort, request, session
from sqlalchemy import inspect, text
from werkzeug.security import generate_password_hash

from config import Config
from extensions import db, login_manager
from models import Expense, ExpenseRecord, ExpenseRevision, Group, GroupMembership, User


def initialize_database(app):
    """Create new tables and apply only additive, safe SQLite compatibility changes."""
    with app.app_context():
        db.create_all()
        group_columns = {column["name"] for column in inspect(db.engine).get_columns("group")}
        if "owner_id" not in group_columns:
            db.session.execute(text('ALTER TABLE "group" ADD COLUMN owner_id INTEGER'))
            db.session.commit()
        for column_name, column_type in {"archived_at": "DATETIME", "archived_by_user_id": "INTEGER"}.items():
            if column_name not in group_columns:
                db.session.execute(text(f'ALTER TABLE "group" ADD COLUMN {column_name} {column_type}'))
                db.session.commit()

        user_columns = {column["name"] for column in inspect(db.engine).get_columns("users")}
        if "name" not in user_columns:
            # Existing accounts have no reliable display name, so do not guess one.
            db.session.execute(text("ALTER TABLE users ADD COLUMN name VARCHAR(100)"))
            db.session.commit()
        if "is_admin" not in user_columns:
            db.session.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"))
            db.session.commit()
        if "is_active" not in user_columns:
            db.session.execute(text("ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1"))
            db.session.commit()
        # Covers a manually created nullable legacy column as well as making
        # the migration repeatable. No existing account receives elevation.
        db.session.execute(text("UPDATE users SET is_admin = 0 WHERE is_admin IS NULL"))
        db.session.execute(text("UPDATE users SET is_active = 1 WHERE is_active IS NULL"))
        db.session.commit()

        # Stage 2 deliberately adds nullable links only.  No existing Member is
        # matched to a User by name, so historical financial data is unchanged.
        additive_columns = {
            "member": {"user_id": "INTEGER"},
            "expense": {"paid_by_user_id": "INTEGER"},
            "expense_split": {"user_id": "INTEGER", "share_weight": "VARCHAR(100)"},
            "confirmed_settlement": {"recorded_by_user_id": "INTEGER", "submission_id": "VARCHAR(36)"},
        }
        inspector = inspect(db.engine)
        for table_name, columns in additive_columns.items():
            existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, column_type in columns.items():
                if column_name not in existing_columns:
                    db.session.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"))
                    db.session.commit()

        # Nullable metadata leaves every legacy payment snapshot unchanged.
        # create_all does not add indexes to existing tables, so upgrade it here.
        db.session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_confirmed_settlement_submission "
            "ON confirmed_settlement (submission_id)"
        ))
        db.session.commit()

        # Seed the supplied initial account only once. It has no admin role
        # unless an operator explicitly assigns is_admin in the database.
        initial_id = app.config.get("INITIAL_ADMIN_ID")
        admin = User.query.filter_by(login_id=initial_id).first() if initial_id else None
        if initial_id and admin is None:
            if not (len(initial_id) == 10 and initial_id.isascii() and initial_id.isdigit()):
                raise ValueError("INITIAL_ADMIN_ID must contain exactly ten ASCII digits.")
            if not app.config.get("INITIAL_ADMIN_PASSWORD"):
                raise ValueError("Creating a bootstrap account requires INITIAL_ADMIN_PASSWORD.")
            # Flask-SQLAlchemy creates the model constructor dynamically.  Setting
            # fields explicitly keeps static type checkers as well as SQLAlchemy
            # happy.
            admin = User()
            admin.login_id = app.config["INITIAL_ADMIN_ID"]
            admin.name = app.config["INITIAL_ADMIN_NAME"]
            admin.password_hash = generate_password_hash(app.config["INITIAL_ADMIN_PASSWORD"])
            db.session.add(admin)
            db.session.commit()

        # Only an explicitly configured account can receive ownerless legacy groups.
        if Group.query.filter_by(owner_id=None).first() is not None:
            if admin is None:
                raise ValueError("Ownerless legacy groups require an explicitly configured INITIAL_ADMIN_ID.")
            Group.query.filter_by(owner_id=None).update({Group.owner_id: admin.id})
        # An existing group owner is the only legacy identity that can be mapped
        # safely and unambiguously to a global user.
        for group in Group.query.filter(Group.owner_id.isnot(None)).all():
            exists = GroupMembership.query.filter_by(group_id=group.id, user_id=group.owner_id).first()
            if exists is None:
                db.session.add(GroupMembership(group_id=group.id, user_id=group.owner_id))
        db.session.commit()

        # Every pre-Stage-6 expense is preserved byte-for-byte as revision one.
        # The mapping is structural only and never infers a global User identity.
        try:
            # Serialize the read-and-wrap phase against SQLite expense writers
            # and another initializer. The preceding upgrade commits are additive.
            if db.engine.dialect.name == "sqlite":
                db.session.execute(text("BEGIN IMMEDIATE"))
            revised_expense_ids = {row[0] for row in db.session.query(ExpenseRevision.expense_id).all()}
            for expense in Expense.query.order_by(Expense.id).all():
                if expense.id in revised_expense_ids:
                    continue
                root_id = expense.id if db.session.get(ExpenseRecord, expense.id) is None else None
                record = ExpenseRecord(id=root_id, group_id=expense.group_id, current_expense_id=expense.id,
                                       created_at=expense.created_at)
                db.session.add(record)
                db.session.flush()
                db.session.add(ExpenseRevision(expense_record_id=record.id, expense_id=expense.id, version_number=1,
                                               recorded_at=expense.created_at))
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    if app.config.get("PRODUCTION"):
        app.config.update(
            SESSION_COOKIE_SECURE=True,
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SAMESITE="Lax",
            PREFERRED_URL_SCHEME="https",
        )
    if not app.config.get("SECRET_KEY"):
        if app.config.get("PRODUCTION"):
            raise RuntimeError("SECRET_KEY must be configured in production.")
        app.config["SECRET_KEY"] = secrets.token_hex(32)
        if not app.testing:
            app.logger.warning("SECRET_KEY is unset; using an ephemeral key. Configure a stable secret before deployment.")

    def csrf_token():
        token = session.get("_csrf_token")
        if token is None:
            token = secrets.token_urlsafe(32)
            session["_csrf_token"] = token
        return token

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def protect_csrf():
        if not app.config.get("CSRF_ENABLED") or request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        expected = session.get("_csrf_token")
        supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if not isinstance(expected, str) or not isinstance(supplied, str) or not hmac.compare_digest(expected, supplied):
            abort(400, description="Invalid CSRF token.")
        return None

    db.init_app(app)
    login_manager.init_app(app)
    # Flask-Login's runtime attribute is not annotated in older releases.
    login_manager.login_view = "main.login"  # type: ignore[reportAttributeAccessIssue]
    login_manager.login_message_category = "info"

    from routes import admin_authenticated, main

    app.jinja_env.globals["admin_authenticated"] = admin_authenticated

    app.register_blueprint(main)

    initialize_database(app)

    return app


app = create_app()


if __name__ == "__main__":
    app.run()
