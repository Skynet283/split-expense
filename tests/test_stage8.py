import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app import create_app
from config import Config
from extensions import db
from helpers import compute_global_user_balances
from models import Group, User
import test_stage6
from test_factory import create_user


class Stage8Tests(unittest.TestCase):
    setUp = test_stage6.Stage6Tests.setUp
    register = test_stage6.Stage6Tests.register
    login = test_stage6.Stage6Tests.login
    seed = test_stage6.Stage6Tests.seed

    def promote(self, login_id):
        with self.app.app_context():
            user = User.query.filter_by(login_id=login_id).one()
            user.is_admin = True
            db.session.commit()
            return user.id

    def test_existing_users_receive_false_admin_flag_during_upgrade(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.db"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, login_id VARCHAR(10) UNIQUE NOT NULL, password_hash VARCHAR(255) NOT NULL)")
            connection.execute("INSERT INTO users (id, login_id, password_hash) VALUES (1, '0123456789', 'legacy-hash')")
            connection.commit()
            connection.close()

            class LegacyConfig(Config):
                TESTING = True
                SQLALCHEMY_DATABASE_URI = "sqlite:///" + database.as_posix()
                INITIAL_ADMIN_ID = "9999999999"
                INITIAL_ADMIN_PASSWORD = "test-admin-password"
                INITIAL_ADMIN_NAME = "Test Administrator"

            app = create_app(LegacyConfig)
            try:
                with app.app_context():
                    legacy = User.query.filter_by(login_id="0123456789").one()
                    seeded = User.query.filter_by(login_id="9999999999").one()
                    self.assertFalse(legacy.is_admin)
                    self.assertFalse(seeded.is_admin)
            finally:
                with app.app_context():
                    db.session.remove()
                    db.engine.dispose()

    def test_public_registration_cannot_inject_role_or_create_user(self):
        response = self.client.post("/register", data={"name": "Asha", "login_id": "0123456789",
                                                       "password": "secret", "is_admin": "true"})
        self.assertEqual(response.status_code, 403)
        with self.app.app_context():
            self.assertIsNone(User.query.filter_by(login_id="0123456789").first())

    def test_admin_page_rejects_anonymous_and_authenticated_non_admin(self):
        self.assertEqual(self.client.get("/admin").status_code, 302)
        self.register("Asha", "0123456789")
        self.login("0123456789")
        self.promote("0123456789")
        self.assertEqual(self.client.get("/admin").status_code, 403)

    def test_admin_page_shows_safe_operational_information(self):
        group_id, _, users = self.seed()
        self.promote("0123456789")
        self.client.post(f"/groups/{group_id}/delete")
        self.client.post("/groups", data={"name": "Active", "user_ids": ["1234567890"]})
        with self.app.app_context():
            owner = User.query.filter_by(login_id="0123456789").one()
            owner.password_hash = "private-admin-hash"
            db.session.commit()
            balances, unresolved = compute_global_user_balances()

        self.client.post("/logout")
        self.client.post("/admin/login", data={"username": "test-admin", "password": "test-admin-password"})

        page = self.client.get("/admin")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Administration", page.data)
        self.assertIn(b"0123456789", page.data)
        self.assertIn(b"1234567890", page.data)
        self.assertIn(b"Archived", page.data)
        self.assertIn(b"Active", page.data)
        self.assertIn(b"User Management", page.data)
        self.assertNotIn(b"private-admin-hash", page.data)
        self.assertNotIn(b"password_hash", page.data)
        self.assertIn(f"{balances[users['0123456789']]:.2f}".encode(), page.data)
        self.assertIn(str(len(unresolved)).encode(), page.data)
        self.assertIn(b"Administration", page.data)

    def test_admin_does_not_bypass_group_owner_or_immutable_history(self):
        group_id, record_id, users = self.seed()
        self.client.post("/logout")
        self.login("1234567890")
        self.promote("1234567890")
        edit_data = {"description": "Unauthorized", "paid_by": str(users["0123456789"]), "expense_type": "custom",
                     "custom_total": "500.00", f"custom_{users['1234567890']}": "500.00"}
        self.assertEqual(self.client.post(f"/groups/{group_id}/expenses/{record_id}/edit", data=edit_data).status_code, 403)
        self.assertEqual(self.client.post(f"/groups/{group_id}/expenses/{record_id}/delete").status_code, 403)
        self.assertEqual(self.client.post(f"/groups/{group_id}/delete").status_code, 403)
        self.assertEqual(self.client.get("/admin").status_code, 403)
        self.client.post("/logout")
        self.client.post("/admin/login", data={"username": "test-admin", "password": "test-admin-password"})
        self.assertEqual(self.client.get("/admin").status_code, 200)
        self.assertEqual(self.client.get(f"/groups/{group_id}").status_code, 302)
        with self.app.app_context():
            self.assertEqual(db.session.get(Group, group_id).expense_records[0].revisions[0].expense.description, "Dinner")

    def test_admin_overview_stays_read_only_and_mutations_are_user_only(self):
        admin_rules = [rule for rule in self.app.url_map.iter_rules() if rule.endpoint == "main.admin_overview"]
        self.assertEqual(len(admin_rules), 1)
        self.assertEqual(admin_rules[0].methods, {"GET", "HEAD", "OPTIONS"})


if __name__ == "__main__":
    unittest.main()
