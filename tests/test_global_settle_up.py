from test_factory import settlement_form_data
import re
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from expense_history import create_expense_record
from helpers import compute_global_user_balances, compute_pairwise_user_obligations, get_user_settlement_summary
from models import (ConfirmedSettlement, Expense, ExpenseSplit, Group, GroupMembership,
                    Member, SettlementRequest, User)
import test_security


class GlobalSettleUpConfig(test_security.SecurityConfig):
    SECRET_KEY = "global-settle-up-test-secret"


class GlobalSettleUpTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(GlobalSettleUpConfig)
        with self.app.app_context():
            db.drop_all()
            db.create_all()
        self.client = self.app.test_client()

    def token(self, path="/login"):
        self.client.get(path)
        with self.client.session_transaction() as session:
            return session["_csrf_token"]

    def post(self, path, data=None, *, token_path="/login"):
        payload = dict(data or {})
        payload["csrf_token"] = self.token(token_path)
        return self.client.post(path, data=payload)

    def add_users(self):
        with self.app.app_context():
            users = {}
            for name, login_id in (("Asha", "0123456789"), ("Bala", "1234567890"), ("Chitra", "2345678901")):
                user = User(name=name, login_id=login_id, password_hash=generate_password_hash("secret"))
                db.session.add(user)
                db.session.flush()
                users[name] = user
            db.session.commit()
            return {name: user.id for name, user in users.items()}

    def add_obligation(self, users, debtor_name, creditor_name, amount, group_name):
        """Add one direct debtor-to-creditor expense snapshot in its own group."""
        with self.app.app_context():
            debtor = db.session.get(User, users[debtor_name])
            creditor = db.session.get(User, users[creditor_name])
            group = Group(name=group_name, owner_id=creditor.id)
            db.session.add(group)
            db.session.flush()
            members = {}
            for user in (debtor, creditor):
                db.session.add(GroupMembership(group_id=group.id, user_id=user.id))
                member = Member(group_id=group.id, user_id=user.id, name=user.name)
                db.session.add(member)
                db.session.flush()
                members[user.id] = member
            expense = Expense(group_id=group.id, description=group_name, paid_by_id=members[creditor.id].id,
                              paid_by_user_id=creditor.id, amount=Decimal(amount), expense_type="custom")
            db.session.add(expense)
            db.session.flush()
            create_expense_record(expense, creditor.id)
            db.session.add(ExpenseSplit(expense_id=expense.id, member_id=members[debtor.id].id,
                                        user_id=debtor.id, amount_owed=Decimal(amount)))
            db.session.commit()
            return group.id

    def login(self, login_id):
        return self.post("/login", {"login_id": login_id, "password": "secret"})

    def logout(self):
        return self.post("/logout", token_path="/")

    def section(self):
        page = self.client.get("/").get_data(as_text=True)
        return re.search(r'<section id="global-settle-up".*?</section>', page, re.S).group(0)

    def create_request(self, recipient_id, amount):
        return self.post("/settlement-requests", settlement_form_data(self.client, {"recipient_user_id": recipient_id, "amount": amount}), token_path="/")

    def pairwise(self):
        with self.app.app_context():
            return compute_pairwise_user_obligations()[0]

    def global_balances(self):
        with self.app.app_context():
            return compute_global_user_balances()[0]

    def test_obligations_aggregate_across_multiple_groups(self):
        users = self.add_users()
        self.add_obligation(users, "Asha", "Bala", "40.00", "Trip")
        self.add_obligation(users, "Asha", "Bala", "60.00", "Dinner")
        self.login("0123456789")
        section = self.section()
        self.assertIn("Bala", section)
        self.assertIn("₹100.00", section)
        self.assertEqual(self.pairwise(),
                         {(users["Asha"], users["Bala"]): Decimal("100.00")})

    def test_reciprocal_obligations_net_correctly_in_dashboard(self):
        users = self.add_users()
        self.add_obligation(users, "Asha", "Bala", "100.00", "Trip")
        self.add_obligation(users, "Bala", "Asha", "25.00", "Refunded meal")
        self.login("0123456789")
        section = self.section()
        self.assertIn("₹75.00", section)
        self.assertNotIn("₹100.00", section)
        self.assertEqual(self.pairwise(),
                         {(users["Asha"], users["Bala"]): Decimal("75.00")})

    def test_single_settlement_request_can_cover_multiple_groups(self):
        users = self.add_users()
        self.add_obligation(users, "Asha", "Bala", "40.00", "Trip")
        self.add_obligation(users, "Asha", "Bala", "60.00", "Dinner")
        self.login("0123456789")
        self.assertEqual(self.create_request(users["Bala"], "100.00").status_code, 302)
        with self.app.app_context():
            request = SettlementRequest.query.one()
            self.assertEqual((request.requester_user_id, request.recipient_user_id, request.amount),
                             (users["Asha"], users["Bala"], Decimal("100.00")))
        self.logout()
        self.login("1234567890")
        with self.app.app_context():
            request_id = SettlementRequest.query.one().id
        self.assertEqual(self.post(f"/settlement-requests/{request_id}/confirm", token_path="/").status_code, 302)
        with self.app.app_context():
            self.assertEqual(ConfirmedSettlement.query.count(), 1)
            self.assertEqual(compute_pairwise_user_obligations()[0], {})

    def test_completed_payment_is_shown_to_both_participants(self):
        users = self.add_users()
        self.add_obligation(users, 'Asha', 'Bala', '100.00', 'Trip')
        self.login('0123456789')
        self.create_request(users['Bala'], '30.00')
        for login_id in ('0123456789', '1234567890'):
            self.logout()
            self.login(login_id)
            self.assertIn('Completed', self.client.get('/').get_data(as_text=True))
            self.assertIn('70.00', self.section())
            self.assertNotIn('Awaiting confirmation', self.section())

    def test_full_payment_removes_outstanding_form(self):
        users = self.add_users()
        self.add_obligation(users, 'Asha', 'Bala', '10.00', 'Trip')
        self.login('0123456789')
        self.create_request(users['Bala'], '10.00')
        self.assertNotIn('settlement-form', self.section())
        self.assertNotIn('Awaiting confirmation', self.section())
        self.assertEqual(self.pairwise(), {})

    def test_partial_payment_limits_form_and_backend_to_remaining_obligation(self):
        users = self.add_users()
        self.add_obligation(users, 'Asha', 'Bala', '100.00', 'Trip')
        self.login('0123456789')
        self.create_request(users['Bala'], '40.00')
        self.assertRegex(self.section(), r'max="60\.00" value="60\.00"')
        self.create_request(users['Bala'], '60.01')
        with self.app.app_context():
            self.assertEqual(ConfirmedSettlement.query.count(), 1)

    def test_partial_payments_and_reversal_restore_available_amount(self):
        users = self.add_users()
        self.add_obligation(users, 'Asha', 'Bala', '100.00', 'Trip')
        self.login('0123456789')
        self.create_request(users['Bala'], '40.00')
        self.create_request(users['Bala'], '30.00')
        with self.app.app_context():
            outgoing, _ = get_user_settlement_summary(users['Asha'])
            self.assertEqual(outgoing[0]['available_amount'], Decimal('30.00'))
            identifier = ConfirmedSettlement.query.order_by(ConfirmedSettlement.id).first().id
        self.logout()
        self.login('1234567890')
        self.post(f'/settlements/{identifier}/reverse', token_path='/')
        with self.app.app_context():
            outgoing, _ = get_user_settlement_summary(users['Asha'])
            self.assertEqual(outgoing[0]['available_amount'], Decimal('70.00'))

    def test_confirmed_settlement_automatically_reduces_displayed_obligation(self):
        users = self.add_users()
        self.add_obligation(users, "Asha", "Bala", "100.00", "Trip")
        self.login("0123456789")
        self.create_request(users["Bala"], "30.00")
        self.logout()
        self.login("1234567890")
        with self.app.app_context():
            request_id = SettlementRequest.query.one().id
        self.post(f"/settlement-requests/{request_id}/confirm", token_path="/")
        self.logout()
        self.login("0123456789")
        self.assertIn("₹70.00", self.section())
        self.assertEqual(self.pairwise(),
                         {(users["Asha"], users["Bala"]): Decimal("70.00")})

    def test_archived_groups_continue_to_contribute_to_global_settle_up(self):
        users = self.add_users()
        group_id = self.add_obligation(users, "Asha", "Bala", "100.00", "Archived trip")
        with self.app.app_context():
            group = db.session.get(Group, group_id)
            group.archived_at = datetime.now(timezone.utc)
            group.archived_by_user_id = users["Bala"]
            db.session.commit()
        self.login("0123456789")
        self.assertIn("₹100.00", self.section())
        self.assertEqual(self.global_balances()[users["Asha"]], Decimal("-100.00"))

    def test_dashboard_does_not_infer_a_false_partner_from_net_global_balances(self):
        users = self.add_users()
        self.add_obligation(users, "Asha", "Bala", "50.00", "Asha owes Bala")
        self.add_obligation(users, "Chitra", "Asha", "50.00", "Chitra owes Asha")
        self.login("0123456789")
        section = self.section()
        self.assertEqual(self.global_balances()[users["Asha"]], Decimal("0.00"))
        self.assertIn(f'value="{users["Bala"]}"', section)
        self.assertIn(f'name="payer_user_id" value="{users["Chitra"]}"', section)
        self.assertIn("Chitra", section)
        self.assertEqual(self.pairwise(), {
            (users["Asha"], users["Bala"]): Decimal("50.00"),
            (users["Chitra"], users["Asha"]): Decimal("50.00"),
        })

    def test_global_settle_up_form_remains_csrf_protected(self):
        users = self.add_users()
        self.add_obligation(users, "Asha", "Bala", "100.00", "Trip")
        self.login("0123456789")
        response = self.client.post("/settlement-requests", data=settlement_form_data(self.client, {
            "recipient_user_id": users["Bala"], "amount": "30.00"}))
        self.assertEqual(response.status_code, 400)
        with self.app.app_context():
            self.assertEqual(SettlementRequest.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
