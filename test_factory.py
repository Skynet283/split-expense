"""Test-only user fixture; production account creation remains admin-only."""

from werkzeug.security import generate_password_hash

from extensions import db
from models import User


def create_user(app, name, login_id, password="secret"):
    with app.app_context():
        user = User(name=name, login_id=login_id, password_hash=generate_password_hash(password))
        db.session.add(user)
        db.session.commit()
        return user.id
