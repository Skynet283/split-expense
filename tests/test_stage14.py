from test_factory import settlement_form_data
import unittest
from decimal import Decimal

from app import create_app
from config import Config
from extensions import db
from helpers import compute_global_user_balances, compute_pairwise_user_obligations
from models import (ActivityEvent, ConfirmedSettlement, Expense, ExpenseRecord, ExpenseRevision,
                    ExpenseSplit, Group, SettlementRequest, User)
from test_factory import create_user


class Stage14Config(Config):
    TESTING = True
    CSRF_ENABLED = True
    SECRET_KEY = "stage-14-test-secret"
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    INITIAL_ADMIN_ID = None
    ADMIN_USERNAME = "backend-admin"
    ADMIN_PASSWORD = "admin-secret"


class Stage14Tests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(Stage14Config)
        with self.app.app_context():
            db.drop_all()
            db.create_all()
        self.client = self.app.test_client()
        self.ids = {login_id: create_user(self.app, name, login_id) for name, login_id in (
            ("Asha", "0123456789"), ("Bala", "1234567890"), ("Chitra", "2345678901"))}
        self.login()
        self.post("/groups", {"name": "Trip", "user_ids": ["1234567890", "2345678901"]})
        with self.app.app_context():
            self.group_id = Group.query.filter_by(name="Trip").one().id

    def token(self, path="/"):
        self.client.get(path)
        with self.client.session_transaction() as session:
            return session["_csrf_token"]

    def post(self, path, data=None, token_path="/"):
        payload = dict(data or {})
        payload["csrf_token"] = self.token(token_path)
        return self.client.post(path, data=payload)

    def login(self, login_id="0123456789", password="secret"):
        return self.post("/login", {"login_id": login_id, "password": password}, "/login")

    def logout(self):
        return self.post("/logout")

    def admin_login(self):
        return self.post("/admin/login", {"username": "backend-admin", "password": "admin-secret"},
                         "/admin/login")

    def share_form(self, amount="100.00", weights=("1", "1", "8"), payer="0123456789"):
        data = {"description": "Shared bill", "paid_by": self.ids[payer],
                "expense_type": "shares", "share_total": amount}
        data.update({f"share_{self.ids[login_id]}": weight for login_id, weight in
                     zip(("0123456789", "1234567890", "2345678901"), weights)})
        return data

    def add_share_expense(self, amount="100.00", weights=("1", "1", "8"), payer="0123456789"):
        return self.post(f"/groups/{self.group_id}/expenses/new", self.share_form(amount, weights, payer),
                         f"/groups/{self.group_id}/expenses/new")

    def current_splits(self):
        with self.app.app_context():
            expense = ExpenseRecord.query.order_by(ExpenseRecord.id.desc()).first().current_expense
            return {split.user_id: (split.amount_owed, split.share_weight) for split in expense.splits}

    def test_basic_shares_payer_balance_and_pairwise_obligations(self):
        response = self.add_share_expense()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.current_splits(), {
            self.ids["0123456789"]: (Decimal("10.00"), "1"),
            self.ids["1234567890"]: (Decimal("10.00"), "1"),
            self.ids["2345678901"]: (Decimal("80.00"), "8"),
        })
        with self.app.app_context():
            expense = ExpenseRecord.query.one().current_expense
            self.assertEqual(expense.expense_type, "shares")
            self.assertEqual(sum((split.amount_owed for split in expense.splits), Decimal(0)), expense.amount)
            balances, unresolved = compute_global_user_balances()
            self.assertEqual(unresolved, [])
            self.assertEqual(balances[self.ids["0123456789"]], Decimal("90.00"))
            self.assertEqual(balances[self.ids["1234567890"]], Decimal("-10.00"))
            self.assertEqual(balances[self.ids["2345678901"]], Decimal("-80.00"))
            self.assertEqual(compute_pairwise_user_obligations()[0], {
                (self.ids["1234567890"], self.ids["0123456789"]): Decimal("10.00"),
                (self.ids["2345678901"], self.ids["0123456789"]): Decimal("80.00"),
            })
            self.assertEqual(ActivityEvent.query.count(), 1)

    def test_zero_shares_keep_a_zero_row_and_history_input(self):
        self.add_share_expense(weights=("1", "1", "0"))
        self.assertEqual(self.current_splits()[self.ids["2345678901"]], (Decimal("0.00"), "0"))
        self.assertEqual(self.current_splits()[self.ids["0123456789"]][0], Decimal("50.00"))
        self.assertEqual(self.current_splits()[self.ids["1234567890"]][0], Decimal("50.00"))
        with self.app.app_context():
            record_id = ExpenseRecord.query.one().id
            obligations, unresolved = compute_pairwise_user_obligations()
            self.assertEqual(unresolved, [])
            self.assertNotIn(self.ids["2345678901"], {user_id for pair in obligations for user_id in pair})
        edit = self.client.get(f"/groups/{self.group_id}/expenses/{record_id}/edit").data
        self.assertIn(f'name="share_{self.ids["2345678901"]}"'.encode(), edit)
        self.assertIn(b'value="0"', edit)

    def test_blank_share_excludes_a_member_without_changing_zero_share_semantics(self):
        self.add_share_expense(weights=("1", "1", ""))
        self.assertEqual(self.current_splits(), {
            self.ids["0123456789"]: (Decimal("50.00"), "1"),
            self.ids["1234567890"]: (Decimal("50.00"), "1"),
        })

    def test_equal_decimal_and_repeating_share_rounding(self):
        cases = (
            (("1", "1", "1"), "100.00", ("33.34", "33.33", "33.33")),
            (("0.5", "0.5", "4"), "100.00", ("10.00", "10.00", "80.00")),
            (("1", "1", "1"), "0.01", ("0.01", "0.00", "0.00")),
            (("1", "2", "3"), "100.00", ("16.67", "33.33", "50.00")),
        )
        for weights, total, expected in cases:
            with self.subTest(weights=weights, total=total):
                self.add_share_expense(amount=total, weights=weights)
                splits = self.current_splits()
                actual = tuple(splits[self.ids[login_id]][0] for login_id in
                               ("0123456789", "1234567890", "2345678901"))
                self.assertEqual(actual, tuple(Decimal(value) for value in expected))
                self.assertEqual(sum(actual), Decimal(total))

    def test_invalid_shares_leave_no_partial_expense_or_activity(self):
        invalid_weights = (("0", "0", "0"), ("-1", "1", "1"), ("oops", "1", "1"),
                           ("NaN", "1", "1"), ("Infinity", "1", "1"),
                           ("-Infinity", "1", "1"), ("", "", ""))
        for weights in invalid_weights:
            with self.subTest(weights=weights):
                response = self.add_share_expense(weights=weights)
                self.assertEqual(response.status_code, 302)
                with self.app.app_context():
                    self.assertEqual((Expense.query.count(), ExpenseSplit.query.count(), ExpenseRecord.query.count(),
                                      ExpenseRevision.query.count(), ActivityEvent.query.count()), (0, 0, 0, 0, 0))
        self.assertIn(b"share", self.client.get(f"/groups/{self.group_id}").data.lower())

    def test_inactive_share_field_and_payer_are_rejected_without_partial_state(self):
        self.logout()
        self.admin_login()
        inactive_id = self.ids["2345678901"]
        self.post(f"/admin/users/{inactive_id}/deactivate", token_path="/admin")
        with self.app.app_context():
            self.assertFalse(db.session.get(User, inactive_id).is_active)
        self.post("/admin/logout", token_path="/admin")
        self.login()
        page = self.client.get(f"/groups/{self.group_id}/expenses/new").data
        self.assertIn(f'name="share_{self.ids["1234567890"]}"'.encode(), page)
        self.assertNotIn(f'name="share_{inactive_id}"'.encode(), page)
        for form in (self.share_form(weights=("1", "1", "0")),
                     self.share_form(weights=("1", "1", ""), payer="2345678901")):
            with self.subTest(form=form):
                response = self.post(f"/groups/{self.group_id}/expenses/new", form,
                                     f"/groups/{self.group_id}/expenses/new")
                self.assertEqual(response.status_code, 302)
                with self.app.app_context():
                    self.assertEqual((Expense.query.count(), ExpenseSplit.query.count(),
                                      ActivityEvent.query.count()), (0, 0, 0))

    def test_share_revision_restores_inputs_and_preserves_old_snapshot(self):
        self.add_share_expense()
        with self.app.app_context():
            record = ExpenseRecord.query.one()
            record_id, original_id = record.id, record.current_expense_id
            old_splits = {split.user_id: (split.amount_owed, split.share_weight)
                          for split in record.current_expense.splits}
        edit_page = self.client.get(f"/groups/{self.group_id}/expenses/{record_id}/edit").data
        self.assertIn(b'id="type_shares" value="shares"', edit_page)
        self.assertIn(f'name="share_{self.ids["2345678901"]}"'.encode(), edit_page)
        self.assertIn(b'value="8"', edit_page)
        revised = self.share_form("120.00", ("1", "2", "3"))
        revised["description"] = "Corrected bill"
        response = self.post(f"/groups/{self.group_id}/expenses/{record_id}/edit", revised,
                             f"/groups/{self.group_id}/expenses/{record_id}/edit")
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            record = db.session.get(ExpenseRecord, record_id)
            self.assertEqual(len(record.revisions), 2)
            self.assertEqual(record.revisions[0].expense_id, original_id)
            self.assertEqual({split.user_id: (split.amount_owed, split.share_weight)
                              for split in record.revisions[0].expense.splits}, old_splits)
            self.assertEqual({split.user_id: split.amount_owed for split in record.current_expense.splits}, {
                self.ids["0123456789"]: Decimal("20.00"),
                self.ids["1234567890"]: Decimal("40.00"),
                self.ids["2345678901"]: Decimal("60.00"),
            })
            self.assertEqual(ActivityEvent.query.count(), 2)
        history = self.client.get(f"/groups/{self.group_id}/expenses/{record_id}/history").data
        self.assertIn(b"8 shares", history)
        self.assertIn(b"3 shares", history)

    def test_changed_share_weights_create_a_revision_even_if_cents_match(self):
        self.add_share_expense()
        with self.app.app_context():
            record_id = ExpenseRecord.query.one().id
        edit_path = f"/groups/{self.group_id}/expenses/{record_id}/edit"
        self.post(edit_path, self.share_form(weights=("2", "2", "16")), edit_path)
        with self.app.app_context():
            record = db.session.get(ExpenseRecord, record_id)
            self.assertEqual(len(record.revisions), 2)
            original = {split.user_id: split.share_weight for split in record.revisions[0].expense.splits}
            current = {split.user_id: (split.share_weight, split.amount_owed)
                       for split in record.current_expense.splits}
            self.assertEqual(original, {self.ids["0123456789"]: "1", self.ids["1234567890"]: "1",
                                        self.ids["2345678901"]: "8"})
            self.assertEqual(current, {self.ids["0123456789"]: ("2", Decimal("10.00")),
                                       self.ids["1234567890"]: ("2", Decimal("10.00")),
                                       self.ids["2345678901"]: ("16", Decimal("80.00"))})
        self.post(edit_path, self.share_form(weights=("2.0", "2.0", "16.0")), edit_path)
        with self.app.app_context():
            self.assertEqual(ExpenseRevision.query.count(), 2)
            self.assertEqual(ActivityEvent.query.count(), 2)

    def test_deactivating_zero_share_participant_preserves_history_and_blocks_revision(self):
        self.add_share_expense(weights=("1", "1", "0"))
        with self.app.app_context():
            record = ExpenseRecord.query.one()
            record_id = record.id
            before = [(split.user_id, split.amount_owed, split.share_weight)
                      for split in record.current_expense.splits]
            balances_before = dict(compute_global_user_balances()[0])
        self.logout()
        self.admin_login()
        inactive_id = self.ids["2345678901"]
        self.post(f"/admin/users/{inactive_id}/deactivate", token_path="/admin")
        with self.app.app_context():
            self.assertFalse(db.session.get(User, inactive_id).is_active)
            self.assertEqual([(split.user_id, split.amount_owed, split.share_weight)
                              for split in db.session.get(ExpenseRecord, record_id).current_expense.splits], before)
            self.assertEqual(dict(compute_global_user_balances()[0]), balances_before)
        self.post("/admin/logout", token_path="/admin")
        self.login()
        edit_path = f"/groups/{self.group_id}/expenses/{record_id}/edit"
        self.assertEqual(self.client.get(edit_path).status_code, 302)
        self.logout()
        self.admin_login()
        self.post(f"/admin/users/{inactive_id}/reactivate", token_path="/admin")
        self.post("/admin/logout", token_path="/admin")
        self.login()
        self.assertEqual(self.client.get(edit_path).status_code, 200)

    def test_settlements_and_deactivation_use_existing_accounting(self):
        self.add_share_expense(weights=("0", "1", "0"), amount="10.00")
        with self.app.app_context():
            record_id = ExpenseRecord.query.one().id
        self.logout()
        self.admin_login()
        debtor_id = self.ids["1234567890"]
        self.post(f"/admin/users/{debtor_id}/deactivate", token_path="/admin")
        with self.app.app_context():
            self.assertTrue(db.session.get(User, debtor_id).is_active)
        self.post("/admin/logout", token_path="/admin")
        self.login("1234567890")
        self.post("/settlement-requests", settlement_form_data(self.client, {"recipient_user_id": self.ids["0123456789"],
                                             "amount": "10.00"}))
        with self.app.app_context():
            request_id = SettlementRequest.query.one().id
            self.assertEqual(compute_pairwise_user_obligations()[0], {})
        self.logout()
        self.login()
        self.post(f"/settlement-requests/{request_id}/confirm")
        with self.app.app_context():
            self.assertEqual(compute_pairwise_user_obligations()[0], {})
            self.assertEqual(compute_global_user_balances()[0][debtor_id], Decimal("0.00"))
            self.assertEqual(ConfirmedSettlement.query.one().amount, Decimal("10.00"))
        self.logout()
        self.admin_login()
        self.post(f"/admin/users/{debtor_id}/deactivate", token_path="/admin")
        with self.app.app_context():
            self.assertFalse(db.session.get(User, debtor_id).is_active)
            self.assertEqual(ExpenseRecord.query.one().id, record_id)
            self.assertEqual(ConfirmedSettlement.query.one().amount, Decimal("10.00"))

    def test_existing_equal_and_custom_splits_still_work(self):
        group_path = f"/groups/{self.group_id}/expenses/new"
        self.post(group_path, {"paid_by": self.ids["0123456789"], "expense_type": "equal",
                               "amount": "100.00", "shared_by": list(self.ids.values())}, group_path)
        self.post(group_path, {"paid_by": self.ids["0123456789"], "expense_type": "custom",
                               "custom_total": "25.00", f"custom_{self.ids['1234567890']}": "25.00"}, group_path)
        with self.app.app_context():
            self.assertEqual(ExpenseRecord.query.count(), 2)
            self.assertEqual({expense.expense_type for expense in Expense.query.all()}, {"equal", "custom"})
            self.assertTrue(all(split.share_weight is None for split in ExpenseSplit.query.all()))


if __name__ == "__main__":
    unittest.main()
