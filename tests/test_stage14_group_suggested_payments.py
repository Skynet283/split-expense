from test_factory import settlement_form_data
import unittest
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

import openpyxl

from app import create_app
from config import Config
from extensions import db
from helpers import compute_balances, compute_global_user_balances, compute_pairwise_user_obligations
from models import ConfirmedSettlement, ExpenseRecord, Group, SettlementRequest
from test_factory import create_user


class GroupSuggestionsConfig(Config):
    TESTING = True
    CSRF_ENABLED = True
    SECRET_KEY = "group-suggestions-test-secret"
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    INITIAL_ADMIN_ID = None


class GroupSuggestedPaymentsTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(GroupSuggestionsConfig)
        with self.app.app_context():
            db.drop_all()
            db.create_all()
        self.client = self.app.test_client()
        self.ids = {login_id: create_user(self.app, name, login_id) for name, login_id in (
            ("Asha", "0123456789"), ("Bala", "1234567890"), ("Chitra", "2345678901"))}
        self.login("0123456789")
        self.post("/groups", {"name": "Trip", "user_ids": ["1234567890"]})
        with self.app.app_context():
            self.group_id = Group.query.filter_by(name="Trip").one().id
        group_path = f"/groups/{self.group_id}/expenses/new"
        self.post(group_path, {"description": "Cab", "paid_by": self.ids["0123456789"],
                               "expense_type": "custom", "custom_total": "100.00",
                               f"custom_{self.ids['1234567890']}": "100.00"}, token_path=group_path)
        with self.app.app_context():
            self.assertEqual(ExpenseRecord.query.count(), 1)

    def token(self, path="/"):
        self.client.get(path)
        with self.client.session_transaction() as session:
            return session["_csrf_token"]

    def post(self, path, data=None, token_path="/"):
        payload = dict(data or {})
        payload["csrf_token"] = self.token(token_path)
        return self.client.post(path, data=payload)

    def login(self, login_id):
        return self.post("/login", {"login_id": login_id, "password": "secret"}, token_path="/login")

    def logout(self):
        return self.post("/logout")

    def group_page(self):
        return self.client.get(f"/groups/{self.group_id}")

    def accounting_snapshot(self):
        with self.app.app_context():
            group = db.session.get(Group, self.group_id)
            return (dict(compute_balances(group)), dict(compute_global_user_balances()[0]),
                    dict(compute_pairwise_user_obligations()[0]))

    def test_default_is_false_and_group_card_and_computation_are_absent(self):
        self.assertIs(Config.SHOW_GROUP_SUGGESTED_PAYMENTS, False)
        self.assertIs(self.app.config["SHOW_GROUP_SUGGESTED_PAYMENTS"], False)
        with patch("routes.compute_settlements", side_effect=AssertionError("hidden computation ran")):
            response = self.group_page()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Suggested Expense Payments", response.data)
        self.assertNotIn(b"No expense payments are currently suggested", response.data)
        self.assertIn(b"Group Expense Balances", response.data)

    def test_true_restores_existing_group_suggestions(self):
        self.app.config["SHOW_GROUP_SUGGESTED_PAYMENTS"] = True
        response = self.group_page()
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("Suggested Expense Payments", text)
        suggestion_area = text.split("Suggested Expense Payments", 1)[1].split("Expenses Log", 1)[0]
        self.assertIn("Bala", suggestion_area)
        self.assertIn("Asha", suggestion_area)
        self.assertIn("₹100.00", suggestion_area)

    def test_group_export_keeps_existing_suggestions_for_both_flag_values(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.app.config["SHOW_GROUP_SUGGESTED_PAYMENTS"] = enabled
                response = self.client.get(f"/groups/{self.group_id}/export")
                self.assertEqual(response.status_code, 200)
                workbook = openpyxl.load_workbook(BytesIO(response.data), read_only=True)
                self.assertIn("Suggested Payments", workbook.sheetnames)
                self.assertEqual(list(workbook["Suggested Payments"].values)[1],
                                 ("Bala", "Asha", 100))
                workbook.close()

    def test_global_settle_up_and_balances_match_with_flag_off_and_on(self):
        baseline = self.accounting_snapshot()
        self.assertEqual(baseline[1][self.ids["0123456789"]], Decimal("100.00"))
        self.assertEqual(baseline[2], {
            (self.ids["1234567890"], self.ids["0123456789"]): Decimal("100.00")})
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.app.config["SHOW_GROUP_SUGGESTED_PAYMENTS"] = enabled
                self.assertEqual(self.group_page().status_code, 200)
                dashboard = self.client.get("/").get_data(as_text=True)
                self.assertIn("Global Settle Up", dashboard)
                self.assertIn("₹100.00", dashboard)
                self.assertEqual(self.accounting_snapshot(), baseline)

    def test_settlement_request_and_confirmation_work_across_flag_values(self):
        self.logout()
        self.login("1234567890")
        self.app.config["SHOW_GROUP_SUGGESTED_PAYMENTS"] = False
        response = self.post("/settlement-requests", settlement_form_data(self.client, {
            "recipient_user_id": self.ids["0123456789"], "amount": "100.00"}))
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            settlement_request = SettlementRequest.query.one()
            request_id = settlement_request.id
            self.assertEqual(settlement_request.status, "CONFIRMED")
            self.assertEqual(self.accounting_snapshot()[2], {})
        self.app.config["SHOW_GROUP_SUGGESTED_PAYMENTS"] = True
        self.logout()
        self.login("0123456789")
        self.assertEqual(self.post(f"/settlement-requests/{request_id}/confirm").status_code, 302)
        with self.app.app_context():
            self.assertEqual(SettlementRequest.query.one().status, "CONFIRMED")
            self.assertEqual(ConfirmedSettlement.query.one().amount, Decimal("100.00"))
            self.assertEqual(compute_pairwise_user_obligations()[0], {})
            self.assertEqual(compute_global_user_balances()[0][self.ids["1234567890"]], Decimal("0.00"))

    def test_group_authorization_is_unchanged_by_flag(self):
        self.logout()
        self.login("2345678901")
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.app.config["SHOW_GROUP_SUGGESTED_PAYMENTS"] = enabled
                self.assertEqual(self.group_page().status_code, 403)
        self.logout()
        self.login("1234567890")
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.app.config["SHOW_GROUP_SUGGESTED_PAYMENTS"] = enabled
                self.assertEqual(self.group_page().status_code, 200)


if __name__ == "__main__":
    unittest.main()
