import sqlite3
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from werkzeug.security import check_password_hash, generate_password_hash

from app import create_app
from config import Config
from extensions import db
from helpers import compute_global_user_balances, compute_pairwise_user_obligations
from models import (ActivityEvent, ConfirmedSettlement, Expense, ExpenseRecord, ExpenseRevision,
                    Group, GroupMembership, Member, SettlementRequest, User)
from test_factory import create_user


class Stage13Config(Config):
    TESTING = True
    CSRF_ENABLED = True
    SECRET_KEY = "stage-13-test-secret"
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    INITIAL_ADMIN_ID = None
    ADMIN_USERNAME = "backend-admin"
    ADMIN_PASSWORD_HASH = generate_password_hash("admin-secret")


class Stage13Tests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(Stage13Config)
        with self.app.app_context():
            db.drop_all()
            db.create_all()
        self.client = self.app.test_client()
        self.ids = {login_id: create_user(self.app, name, login_id) for name, login_id in (
            ("Asha", "0123456789"), ("Bala", "1234567890"), ("Chitra", "2345678901"))}

    def token(self, path="/login"):
        self.client.get(path)
        with self.client.session_transaction() as session:
            return session["_csrf_token"]

    def post(self, path, data=None, token_path="/login"):
        payload = dict(data or {})
        payload["csrf_token"] = self.token(token_path)
        return self.client.post(path, data=payload)

    def user_login(self, login_id="0123456789", password="secret"):
        return self.post("/login", {"login_id": login_id, "password": password})

    def user_logout(self):
        return self.post("/logout", token_path="/")

    def admin_login(self):
        return self.post("/admin/login", {"username": "backend-admin", "password": "admin-secret"},
                         token_path="/admin/login")

    def admin_logout(self):
        return self.post("/admin/logout", token_path="/admin")

    def make_group(self, members=("1234567890",)):
        self.user_login()
        self.post("/groups", {"name": "Trip", "user_ids": list(members)}, token_path="/")
        with self.app.app_context():
            return Group.query.filter_by(name="Trip").one().id

    def add_expense(self, group_id, payer, debtor, amount="10.00"):
        return self.post(f"/groups/{group_id}/expenses/new", {
            "description": "Dinner", "paid_by": self.ids[payer], "expense_type": "custom",
            "custom_total": amount, f"custom_{self.ids[debtor]}": amount,
        }, token_path=f"/groups/{group_id}/expenses/new")

    def test_public_registration_and_database_admin_flag_cannot_manage_users(self):
        self.assertEqual(self.client.get("/register").status_code, 403)
        self.assertEqual(self.post("/register", {"name": "Dara", "login_id": "3456789012",
                                                 "password": "secret"}).status_code, 403)
        self.assertNotIn(b"Create new user", self.client.get("/login").data)
        self.assertEqual(self.client.get("/admin").status_code, 302)
        self.user_login()
        with self.app.app_context():
            User.query.filter_by(login_id="0123456789").one().is_admin = True
            db.session.commit()
        self.assertEqual(self.client.get("/admin").status_code, 403)
        self.assertEqual(self.post("/register", {"name": "Dara"}, token_path="/").status_code, 403)
        for path in ("/admin/users", f"/admin/users/{self.ids['1234567890']}/deactivate",
                     f"/admin/users/{self.ids['1234567890']}/reactivate",
                     f"/admin/users/{self.ids['1234567890']}/reset-password"):
            self.assertEqual(self.post(path, token_path="/").status_code, 403)
        with self.app.app_context():
            self.assertIsNone(User.query.filter_by(login_id="3456789012").first())

    def test_admin_creation_validation_hash_and_login(self):
        self.assertEqual(self.admin_login().status_code, 302)
        page = self.client.get("/admin")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"User Management", page.data)
        self.assertIn(b"0123456789", page.data)
        self.assertNotIn(b"password_hash", page.data)
        for invalid in ({"name": "Dara", "login_id": "1234", "password": "new-secret",
                         "confirm_password": "new-secret"},
                        {"name": "Duplicate", "login_id": "0123456789", "password": "new-secret",
                         "confirm_password": "new-secret"}):
            response = self.post("/admin/users", invalid, token_path="/admin")
            self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(User.query.filter_by(login_id="1234").first())
            self.assertEqual(User.query.count(), 3)
        response = self.post("/admin/users", {"name": "Dara", "login_id": "3456789012",
                                                    "password": "new-secret", "confirm_password": "new-secret"},
                             token_path="/admin")
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            user = User.query.filter_by(login_id="3456789012").one()
            self.assertTrue(user.is_active)
            self.assertNotEqual(user.password_hash, "new-secret")
            self.assertTrue(check_password_hash(user.password_hash, "new-secret"))
        self.admin_logout()
        self.assertEqual(self.user_login("3456789012", "new-secret").status_code, 302)
        self.assertIn(b"Your Groups", self.client.get("/").data)

    def test_admin_password_reset_replaces_old_password(self):
        self.admin_login()
        target = self.ids["1234567890"]
        self.assertEqual(self.post(f"/admin/users/{target}/reset-password", {
            "password": "replacement", "confirm_password": "replacement"
        }, token_path="/admin").status_code, 302)
        self.admin_logout()
        self.user_login("1234567890", "secret")
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.user_login("1234567890", "replacement").status_code, 302)
        self.assertIn(b"Your Groups", self.client.get("/").data)

    def test_settled_user_deactivation_preserves_history_and_reactivation(self):
        group_id = self.make_group()
        self.add_expense(group_id, "0123456789", "1234567890")
        with self.app.app_context():
            record_id = ExpenseRecord.query.one().id
        self.user_logout()
        self.user_login("1234567890")
        self.post("/settlement-requests", {"recipient_user_id": self.ids["0123456789"],
                                           "amount": "10.00"}, token_path="/")
        with self.app.app_context():
            request_id = SettlementRequest.query.one().id
        self.user_logout()
        self.user_login()
        self.post(f"/settlement-requests/{request_id}/confirm", token_path="/")
        with self.app.app_context():
            self.assertEqual(compute_global_user_balances()[0][self.ids["1234567890"]], Decimal("0.00"))
            self.assertFalse(any(self.ids["1234567890"] in pair for pair in compute_pairwise_user_obligations()[0]))
            before = (User.query.count(), GroupMembership.query.count(), Member.query.count(),
                      Expense.query.count(), ExpenseRecord.query.count(), ExpenseRevision.query.count(),
                      ActivityEvent.query.count(), SettlementRequest.query.count(), ConfirmedSettlement.query.count())
        self.user_logout()
        self.admin_login()
        target = self.ids["1234567890"]
        self.post(f"/admin/users/{target}/deactivate", token_path="/admin")
        with self.app.app_context():
            self.assertFalse(db.session.get(User, target).is_active)
            after = (User.query.count(), GroupMembership.query.count(), Member.query.count(),
                     Expense.query.count(), ExpenseRecord.query.count(), ExpenseRevision.query.count(),
                     ActivityEvent.query.count(), SettlementRequest.query.count(), ConfirmedSettlement.query.count())
            self.assertEqual(before, after)
            self.assertIsNotNone(db.session.get(ExpenseRecord, record_id))
        self.admin_logout()
        self.user_login("1234567890")
        self.assertEqual(self.client.get("/").status_code, 302)
        self.admin_login()
        self.post(f"/admin/users/{target}/reactivate", token_path="/admin")
        self.admin_logout()
        self.user_login("1234567890")
        self.assertIn(b"Your Groups", self.client.get("/").data)

    def test_inactive_member_cannot_join_new_snapshot_and_history_stays_accounted(self):
        group_id = self.make_group()
        active_id = self.ids["0123456789"]
        inactive_id = self.ids["1234567890"]
        initial_page = self.client.get(f"/groups/{group_id}/expenses/new").data
        self.assertIn(f'<option value="{active_id}"'.encode(), initial_page)
        self.assertIn(f'<option value="{inactive_id}"'.encode(), initial_page)
        self.add_expense(group_id, "0123456789", "1234567890")
        with self.app.app_context():
            record = ExpenseRecord.query.one()
            record_id = record.id
            original_expense_id = record.current_expense_id
            original_split = [(split.user_id, split.amount_owed) for split in record.current_expense.splits]
        self.user_logout()
        self.user_login("1234567890")
        self.post("/settlement-requests", {"recipient_user_id": active_id, "amount": "10.00"}, token_path="/")
        with self.app.app_context():
            request_id = SettlementRequest.query.one().id
        self.user_logout()
        self.user_login()
        self.post(f"/settlement-requests/{request_id}/confirm", token_path="/")
        with self.app.app_context():
            balances_before = dict(compute_global_user_balances()[0])
            obligations_before = dict(compute_pairwise_user_obligations()[0])
            history_before = (Expense.query.count(), ExpenseRevision.query.count(), ActivityEvent.query.count(),
                              ConfirmedSettlement.query.count(), GroupMembership.query.count(), Member.query.count())
        self.user_logout()
        self.admin_login()
        self.post(f"/admin/users/{inactive_id}/deactivate", token_path="/admin")
        with self.app.app_context():
            self.assertFalse(db.session.get(User, inactive_id).is_active)
        self.admin_logout()
        self.user_login()

        page = self.client.get(f"/groups/{group_id}/expenses/new").data
        self.assertIn(f'<option value="{active_id}"'.encode(), page)
        self.assertNotIn(f'<option value="{inactive_id}"'.encode(), page)
        self.assertNotIn(f'name="shared_by" value="{inactive_id}"'.encode(), page)
        self.assertNotIn(f'name="custom_{inactive_id}"'.encode(), page)

        forged_forms = (
            {"description": "Forged equal", "paid_by": active_id, "expense_type": "equal",
             "amount": "10.00", "shared_by": [active_id, inactive_id]},
            {"description": "Forged payer", "paid_by": inactive_id, "expense_type": "custom",
             "custom_total": "10.00", f"custom_{active_id}": "10.00"},
            {"description": "Forged custom", "paid_by": active_id, "expense_type": "custom",
             "custom_total": "10.00", f"custom_{inactive_id}": "10.00"},
        )
        for form in forged_forms:
            with self.subTest(description=form["description"]):
                response = self.post(f"/groups/{group_id}/expenses/new", form,
                                     token_path=f"/groups/{group_id}/expenses/new")
                self.assertEqual(response.status_code, 302)
                with self.app.app_context():
                    self.assertEqual((Expense.query.count(), ExpenseRevision.query.count(), ActivityEvent.query.count(),
                                      ConfirmedSettlement.query.count(), GroupMembership.query.count(), Member.query.count()),
                                     history_before)
                    self.assertEqual(dict(compute_global_user_balances()[0]), balances_before)
                    self.assertEqual(dict(compute_pairwise_user_obligations()[0]), obligations_before)

        # A revision is also a new financial snapshot. The old one stays intact.
        self.assertEqual(self.client.get(f"/groups/{group_id}/expenses/{record_id}/edit").status_code, 302)
        edit = self.post(f"/groups/{group_id}/expenses/{record_id}/edit", {
            "description": "Forged revision", "paid_by": active_id, "expense_type": "custom",
            "custom_total": "10.00", f"custom_{active_id}": "10.00",
        }, token_path=f"/groups/{group_id}")
        self.assertEqual(edit.status_code, 302)
        with self.app.app_context():
            record = db.session.get(ExpenseRecord, record_id)
            self.assertEqual(record.current_expense_id, original_expense_id)
            self.assertEqual([(split.user_id, split.amount_owed) for split in record.current_expense.splits],
                             original_split)
            self.assertEqual((Expense.query.count(), ExpenseRevision.query.count(), ActivityEvent.query.count(),
                              ConfirmedSettlement.query.count(), GroupMembership.query.count(), Member.query.count()),
                             history_before)

        self.user_logout()
        self.admin_login()
        self.post(f"/admin/users/{inactive_id}/reactivate", token_path="/admin")
        self.admin_logout()
        self.user_login()
        restored_page = self.client.get(f"/groups/{group_id}/expenses/new").data
        self.assertIn(f'<option value="{inactive_id}"'.encode(), restored_page)
        self.assertEqual(self.add_expense(group_id, "1234567890", "0123456789").status_code, 302)
        with self.app.app_context():
            self.assertEqual(ExpenseRecord.query.count(), 2)

    def test_zero_net_does_not_hide_direct_pairwise_obligations(self):
        group_id = self.make_group(("1234567890", "2345678901"))
        self.add_expense(group_id, "1234567890", "0123456789")
        self.add_expense(group_id, "0123456789", "2345678901")
        with self.app.app_context():
            self.assertEqual(compute_global_user_balances()[0][self.ids["0123456789"]], Decimal("0.00"))
            self.assertTrue(any(self.ids["0123456789"] in pair for pair in compute_pairwise_user_obligations()[0]))
        self.user_logout()
        self.admin_login()
        response = self.post(f"/admin/users/{self.ids['0123456789']}/deactivate", token_path="/admin")
        self.assertEqual(response.status_code, 302)
        self.assertIn(b"outstanding direct obligation", self.client.get("/admin").data)
        with self.app.app_context():
            self.assertTrue(db.session.get(User, self.ids["0123456789"]).is_active)

    def test_nonzero_balance_and_stale_pending_request_each_block_deactivation(self):
        group_id = self.make_group()
        self.add_expense(group_id, "0123456789", "1234567890")
        self.user_logout()
        self.admin_login()
        target = self.ids["1234567890"]
        self.post(f"/admin/users/{target}/deactivate", token_path="/admin")
        self.assertIn(b"outstanding global balance", self.client.get("/admin").data)
        with self.app.app_context():
            self.assertTrue(db.session.get(User, target).is_active)
        self.admin_logout()
        self.user_login("1234567890")
        self.post("/settlement-requests", {"recipient_user_id": self.ids["0123456789"],
                                           "amount": "10.00"}, token_path="/")
        self.user_logout()
        self.user_login()
        with self.app.app_context():
            record_id = ExpenseRecord.query.one().id
        self.post(f"/groups/{group_id}/expenses/{record_id}/delete", token_path=f"/groups/{group_id}")
        with self.app.app_context():
            self.assertEqual(compute_global_user_balances()[0].get(target, Decimal("0.00")), Decimal("0.00"))
            self.assertFalse(any(target in pair for pair in compute_pairwise_user_obligations()[0]))
            self.assertEqual(SettlementRequest.query.filter_by(status="PENDING").count(), 1)
        self.user_logout()
        self.admin_login()
        self.post(f"/admin/users/{target}/deactivate", token_path="/admin")
        self.assertIn(b"pending settlement request", self.client.get("/admin").data)
        with self.app.app_context():
            self.assertTrue(db.session.get(User, target).is_active)

    def test_inactive_user_is_hidden_and_rejected_by_group_backend(self):
        self.admin_login()
        self.post(f"/admin/users/{self.ids['2345678901']}/deactivate", token_path="/admin")
        self.admin_logout()
        self.user_login()
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-login-id="1234567890"', page)
        self.assertNotIn('data-login-id="2345678901"', page)
        response = self.post("/groups", {"name": "Blocked", "user_ids": ["2345678901"]}, token_path="/")
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertEqual(Group.query.count(), 0)
            self.assertEqual(GroupMembership.query.count(), 0)

    def test_admin_mutations_require_csrf_and_admin_cannot_edit_accounting(self):
        self.admin_login()
        target = self.ids["1234567890"]
        for path, data in (("/admin/users", {"name": "Dara", "login_id": "3456789012",
                                               "password": "secret", "confirm_password": "secret"}),
                           (f"/admin/users/{target}/deactivate", {}),
                           (f"/admin/users/{target}/reactivate", {}),
                           (f"/admin/users/{target}/reset-password", {"password": "changed",
                                                                          "confirm_password": "changed"})):
            self.assertEqual(self.client.post(path, data=data).status_code, 400)
        self.assertEqual(self.client.post("/groups", data={"name": "Admin group",
                                                             "csrf_token": self.token("/admin")}).status_code, 302)
        with self.app.app_context():
            self.assertEqual(Group.query.count(), 0)
            self.assertTrue(db.session.get(User, target).is_active)
            self.assertIsNone(User.query.filter_by(login_id="3456789012").first())

    def test_existing_database_users_migrate_active(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.db"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, login_id VARCHAR(10) UNIQUE NOT NULL, password_hash VARCHAR(255) NOT NULL)")
                connection.execute("INSERT INTO users VALUES (1, '0123456789', 'legacy-hash')")
                connection.commit()

            class LegacyConfig(Stage13Config):
                SQLALCHEMY_DATABASE_URI = "sqlite:///" + database.as_posix()

            app = create_app(LegacyConfig)
            try:
                with app.app_context():
                    self.assertTrue(User.query.filter_by(login_id="0123456789").one().is_active)
            finally:
                with app.app_context():
                    db.session.remove()
                    db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
