import unittest

from werkzeug.security import check_password_hash

from app import create_app
from config import Config
from extensions import db
from helpers import compute_balances
from models import Expense, Group, GroupMembership, User


class TestConfig(Config):
    TESTING = True
    CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    INITIAL_ADMIN_ID = "9999999999"
    INITIAL_ADMIN_PASSWORD = "test-admin-password"
    INITIAL_ADMIN_NAME = "Test Administrator"


class Stage1Tests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        with self.app.app_context():
            db.drop_all()
            db.create_all()
        self.client = self.app.test_client()

    def register(self, name, login_id, password="secret"):
        return self.client.post("/register", data={
            "name": name, "login_id": login_id, "password": password,
            "confirm_password": password,
        }, follow_redirects=True)

    def login(self, login_id, password="secret"):
        return self.client.post("/login", data={"login_id": login_id, "password": password}, follow_redirects=True)

    def test_registration_validation_and_hashing(self):
        invalid_ids = ("123456789", "12345678901", "abcdefghij", "12345 6789")
        for login_id in invalid_ids:
            with self.subTest(login_id=login_id):
                response = self.register("Asha", login_id)
                self.assertIn(b"exactly 10 digits", response.data)

        self.register("Asha", "0123456789", "secure-password")
        with self.app.app_context():
            user = User.query.filter_by(login_id="0123456789").one()
            self.assertEqual(user.name, "Asha")
            self.assertNotEqual(user.password_hash, "secure-password")
            self.assertTrue(check_password_hash(user.password_hash, "secure-password"))

        response = self.register("Other Asha", "0123456789")
        self.assertIn(b"already registered", response.data)

    def test_login_accepts_correct_password_and_rejects_wrong_one(self):
        self.register("Asha", "0123456789", "secure-password")
        response = self.login("0123456789", "wrong-password")
        self.assertIn(b"Invalid ID or password", response.data)
        response = self.login("0123456789", "secure-password")
        self.assertIn(b"Your Groups", response.data)

    def test_group_memberships_authorization_and_legacy_expense_compatibility(self):
        self.register("Asha", "0123456789")
        self.register("Bala", "1234567890")
        self.register("Chitra", "2345678901")
        self.login("0123456789")
        response = self.client.post("/groups", data={"name": "Trip", "user_ids": ["1234567890"]}, follow_redirects=True)
        self.assertIn(b"Trip", response.data)
        self.assertIn(b"created!", response.data)

        with self.app.app_context():
            group = Group.query.filter_by(name="Trip").one()
            asha = User.query.filter_by(login_id="0123456789").one()
            bala = User.query.filter_by(login_id="1234567890").one()
            self.assertEqual(GroupMembership.query.filter_by(group_id=group.id).count(), 2)
            self.assertIsNotNone(GroupMembership.query.filter_by(group_id=group.id, user_id=asha.id).first())
            self.assertIsNotNone(GroupMembership.query.filter_by(group_id=group.id, user_id=bala.id).first())
            # The existing expense engine still uses these group-local ledger rows.
            self.assertEqual(sorted(member.name for member in group.members), ["Asha", "Bala"])
            group_id = group.id
            user_ids = [asha.id, bala.id]

        self.client.post(f"/groups/{group_id}/expenses/new", data={
            "description": "Dinner", "paid_by": user_ids[0], "expense_type": "equal",
            "amount": "100.00", "shared_by": [str(user_ids[0]), str(user_ids[1])],
        }, follow_redirects=True)
        with self.app.app_context():
            expense = Expense.query.one()
            self.assertEqual(len(expense.splits), 2)
            self.assertEqual(sum(compute_balances(db.session.get(Group, group_id)).values()), 0)

        self.client.post("/logout")
        self.login("1234567890")
        self.assertEqual(self.client.get(f"/groups/{group_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/groups/{group_id}/expenses/new").status_code, 403)

        self.client.post("/logout")
        self.login("2345678901")
        self.assertEqual(self.client.get(f"/groups/{group_id}").status_code, 403)

        # A user may join another group, but the database rejects a duplicate
        # membership for the same group/user pair.
        with self.app.app_context():
            other_group = Group(name="Dinner", owner_id=asha.id)
            db.session.add(other_group)
            db.session.flush()
            db.session.add(GroupMembership(group_id=other_group.id, user_id=bala.id))
            db.session.commit()
            self.assertEqual(GroupMembership.query.filter_by(user_id=bala.id).count(), 2)
            db.session.add(GroupMembership(group_id=other_group.id, user_id=bala.id))
            with self.assertRaises(Exception):
                db.session.commit()
            db.session.rollback()


if __name__ == "__main__":
    unittest.main()
