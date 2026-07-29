from flask import Flask
from sqlalchemy import inspect, text
from werkzeug.security import generate_password_hash

from config import Config
from extensions import db, login_manager
from models import Group, User


def initialize_database(app):
    """Create new tables and safely add ownership to an existing SQLite database."""
    with app.app_context():
        db.create_all()
        group_columns = {column["name"] for column in inspect(db.engine).get_columns("group")}
        if "owner_id" not in group_columns:
            db.session.execute(text('ALTER TABLE "group" ADD COLUMN owner_id INTEGER'))
            db.session.commit()

        # Seed the supplied administrator only once. Its password is stored as a hash.
        admin = User.query.filter_by(login_id=app.config["INITIAL_ADMIN_ID"]).first()
        if admin is None:
            admin = User(
                login_id=app.config["INITIAL_ADMIN_ID"],
                password_hash=generate_password_hash(app.config["INITIAL_ADMIN_PASSWORD"]),
            )
            db.session.add(admin)
            db.session.commit()

        # Existing groups predate authentication, so make the first account their owner.
        Group.query.filter_by(owner_id=None).update({Group.owner_id: admin.id})
        db.session.commit()


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "main.login"
    login_manager.login_message_category = "info"

    from routes import main

    app.register_blueprint(main)

    initialize_database(app)

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
