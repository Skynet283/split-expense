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


def settlement_form_data(client, data):
    """Supply a signed submission ID for a deliberate test payment submission."""
    from helpers import settlement_submission_token

    payload = dict(data)
    with client.session_transaction() as session:
        actor = session.get("_user_id")
    if actor is not None and "submission_token" not in payload:
        actor = int(actor)
        payer = int(payload.get("payer_user_id", actor))
        recipient = int(payload["recipient_user_id"])
        with client.application.app_context():
            payload["submission_token"] = settlement_submission_token(actor, payer, recipient)
    return payload
