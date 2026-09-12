import unittest
from decimal import Decimal

from werkzeug.security import generate_password_hash

from app import create_app
from config import Config
from extensions import db
from expense_history import append_expense_revision, claim_active_expense_record, create_expense_record
from helpers import compute_global_user_balances, compute_pairwise_user_obligations
from models import Expense, ExpenseRecord, ExpenseSplit, Group, GroupMembership, Member, SettlementRequest, User


class TestConfig(Config):
    TESTING = True
    CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    INITIAL_ADMIN_ID = "9999999999"
    INITIAL_ADMIN_PASSWORD = "test-admin-password"
    INITIAL_ADMIN_NAME = "Test Administrator"


class Stage4Tests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        with self.app.app_context():
            db.drop_all()
            db.create_all()
        self.client = self.app.test_client()

    def add_user(self, name, login_id):
        user = User(name=name, login_id=login_id, password_hash=generate_password_hash("secret"))
        db.session.add(user)
        db.session.flush()
        return user

    def add_group_with_expense(self, payer, participants, amount, shares, name="Group"):
        group = Group(name=name, owner_id=payer.id)
        db.session.add(group)
        db.session.flush()
        members = {}
        for user in {user.id: user for user in [payer, *participants]}.values():
            db.session.add(GroupMembership(group_id=group.id, user_id=user.id))
            member = Member(name=user.name, group_id=group.id, user_id=user.id)
            db.session.add(member)
            db.session.flush()
            members[user.id] = member
        expense = Expense(group_id=group.id, paid_by_id=members[payer.id].id, paid_by_user_id=payer.id,
                          amount=Decimal(amount), expense_type="custom")
        db.session.add(expense)
        db.session.flush()
        record = create_expense_record(expense, payer.id)
        for user, share in zip(participants, shares):
            db.session.add(ExpenseSplit(expense_id=expense.id, member_id=members[user.id].id,
                                        user_id=user.id, amount_owed=Decimal(share)))
        return group, expense

    def revise_debt(self, expense_id, amount):
        old = db.session.get(Expense, expense_id)
        record = ExpenseRecord.query.filter_by(current_expense_id=old.id).one()
        record = claim_active_expense_record(record.id)
        snapshot = Expense(description=old.description, group_id=old.group_id, paid_by_id=old.paid_by_id,
                           paid_by_user_id=old.paid_by_user_id, amount=Decimal(amount),
                           expense_type=old.expense_type)
        db.session.add(snapshot)
        db.session.flush()
        for split in old.splits:
            db.session.add(ExpenseSplit(expense_id=snapshot.id, member_id=split.member_id, user_id=split.user_id,
                                        amount_owed=Decimal(amount)))
        append_expense_revision(record, snapshot, record.group.owner_id)
        db.session.commit()

    def login(self, login_id):
        self.client.post("/login", data={"login_id": login_id, "password": "secret"})

    def seed_a_owes_b(self, amount="500.00"):
        with self.app.app_context():
            asha = self.add_user("Asha", "0123456789")
            bala = self.add_user("Bala", "1234567890")
            group, expense = self.add_group_with_expense(bala, [asha], amount, [amount])
            db.session.commit()
            return asha.id, bala.id, expense.id

    def create_request(self, recipient_user_id, amount):
        return self.client.post("/settlement-requests", data={"recipient_user_id": str(recipient_user_id), "amount": amount},
                                follow_redirects=True)

    def test_valid_pending_request_and_money_validation_do_not_change_balances(self):
        asha_id, bala_id, _ = self.seed_a_owes_b()
        self.login("0123456789")
        with self.app.app_context():
            before, _ = compute_global_user_balances()
        response = self.create_request(bala_id, "300.00")
        self.assertIn(b"Settlement request created", response.data)
        with self.app.app_context():
            settlement = SettlementRequest.query.one()
            after, _ = compute_global_user_balances()
            self.assertEqual(settlement.status, "PENDING")
            self.assertEqual(settlement.amount, Decimal("300.00"))
            self.assertEqual(dict(after), dict(before))
            self.assertEqual(before[asha_id], Decimal("-500.00"))
            self.assertEqual(before[bala_id], Decimal("500.00"))

        for invalid_amount in ("0", "-1", "1.001", "bad"):
            self.create_request(bala_id, invalid_amount)
        response = self.create_request(asha_id, "1.00")
        self.assertIn(b"yourself", response.data)
        response = self.create_request(999999, "1.00")
        self.assertIn(b"not found", response.data)
        with self.app.app_context():
            self.assertEqual(SettlementRequest.query.count(), 1)

    def test_pairwise_validation_pending_limit_and_no_false_pairwise_relationship(self):
        with self.app.app_context():
            asha = self.add_user("Asha", "0123456789")
            bala = self.add_user("Bala", "1234567890")
            chitra = self.add_user("Chitra", "2345678901")
            self.add_group_with_expense(bala, [asha, chitra], "500.00", ["300.00", "200.00"])
            db.session.commit()
            asha_id, bala_id, chitra_id = asha.id, bala.id, chitra.id
            obligations, unresolved = compute_pairwise_user_obligations()
            self.assertEqual(obligations[(asha_id, bala_id)], Decimal("300.00"))
            self.assertEqual(obligations[(chitra_id, bala_id)], Decimal("200.00"))
            self.assertEqual(unresolved, [])

        self.login("0123456789")
        self.create_request(bala_id, "200.00")
        self.create_request(bala_id, "100.00")
        self.create_request(bala_id, "0.01")
        response = self.create_request(chitra_id, "1.00")
        self.assertIn(b"No valid outstanding obligation", response.data)
        with self.app.app_context():
            self.assertEqual(SettlementRequest.query.filter_by(status="PENDING").count(), 2)

    def test_only_recipient_can_resolve_and_confirmed_payment_affects_balances(self):
        asha_id, bala_id, _ = self.seed_a_owes_b()
        with self.app.app_context():
            chitra = self.add_user("Chitra", "2345678901")
            db.session.commit()
            chitra_id = chitra.id
        self.login("0123456789")
        self.create_request(bala_id, "100.00")
        with self.app.app_context():
            request_id = SettlementRequest.query.one().id
            before, _ = compute_global_user_balances()

        self.assertEqual(self.client.post(f"/settlement-requests/{request_id}/confirm").status_code, 403)
        self.client.post("/logout")
        self.login("2345678901")
        self.assertEqual(self.client.post(f"/settlement-requests/{request_id}/reject").status_code, 403)
        self.client.post("/logout")
        self.login("1234567890")
        self.client.post(f"/settlement-requests/{request_id}/confirm", follow_redirects=True)
        with self.app.app_context():
            request = db.session.get(SettlementRequest, request_id)
            after, _ = compute_global_user_balances()
            self.assertEqual(request.status, "CONFIRMED")
            self.assertIsNotNone(request.resolved_at)
            self.assertEqual(after[asha_id], before[asha_id] + Decimal("100.00"))
            self.assertEqual(after[bala_id], before[bala_id] - Decimal("100.00"))
        self.client.post(f"/settlement-requests/{request_id}/confirm", follow_redirects=True)
        self.client.post(f"/settlement-requests/{request_id}/reject", follow_redirects=True)
        with self.app.app_context():
            self.assertEqual(db.session.get(SettlementRequest, request_id).status, "CONFIRMED")
        self.assertEqual(asha_id != chitra_id, True)

    def test_rejection_and_stale_confirmation_are_safe(self):
        asha_id, bala_id, expense_id = self.seed_a_owes_b()
        self.login("0123456789")
        self.create_request(bala_id, "500.00")
        with self.app.app_context():
            request_id = SettlementRequest.query.one().id
            self.revise_debt(expense_id, "200.00")

        self.client.post("/logout")
        self.login("1234567890")
        response = self.client.post(f"/settlement-requests/{request_id}/confirm", follow_redirects=True)
        self.assertIn(b"stale", response.data)
        with self.app.app_context():
            self.assertEqual(db.session.get(SettlementRequest, request_id).status, "PENDING")
            self.assertEqual(compute_global_user_balances()[0][asha_id], Decimal("-200.00"))

        self.client.post(f"/settlement-requests/{request_id}/reject", follow_redirects=True)
        with self.app.app_context():
            request = db.session.get(SettlementRequest, request_id)
            self.assertEqual(request.status, "REJECTED")
            self.assertEqual(compute_global_user_balances()[0][bala_id], Decimal("200.00"))
        self.client.post(f"/settlement-requests/{request_id}/confirm", follow_redirects=True)
        self.client.post(f"/settlement-requests/{request_id}/reject", follow_redirects=True)
        with self.app.app_context():
            self.assertEqual(db.session.get(SettlementRequest, request_id).status, "REJECTED")


if __name__ == "__main__":
    unittest.main()
