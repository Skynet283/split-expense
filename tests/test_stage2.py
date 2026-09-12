import unittest
from decimal import Decimal

from app import create_app, initialize_database
from config import Config
from extensions import db
from helpers import compute_balances, compute_user_balances
from models import Expense, ExpenseRecord, ExpenseRevision, ExpenseSplit, Group, GroupMembership, Member, User


class TestConfig(Config):
    TESTING = True
    CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    INITIAL_ADMIN_ID = "9999999999"
    INITIAL_ADMIN_PASSWORD = "test-admin-password"
    INITIAL_ADMIN_NAME = "Test Administrator"


class Stage2Tests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        with self.app.app_context():
            db.drop_all()
            db.create_all()
        self.client = self.app.test_client()

    def register(self, name, login_id):
        self.client.post("/register", data={"name": name, "login_id": login_id,
                                              "password": "secret", "confirm_password": "secret"})

    def login(self, login_id):
        return self.client.post("/login", data={"login_id": login_id, "password": "secret"}, follow_redirects=True)

    def create_group(self, name, user_ids):
        response = self.client.post("/groups", data={"name": name, "user_ids": user_ids}, follow_redirects=True)
        with self.app.app_context():
            return Group.query.filter_by(name=name).one(), response

    def test_global_user_expenses_validate_members_and_edit(self):
        self.register("Asha", "0123456789")
        self.register("Asha", "1234567890")
        self.register("Chitra", "2345678901")
        self.login("0123456789")
        group, response = self.create_group("Trip", ["1234567890"])
        self.assertIn(b"Trip", response.data)

        with self.app.app_context():
            asha = User.query.filter_by(login_id="0123456789").one()
            bala = User.query.filter_by(login_id="1234567890").one()
            chitra = User.query.filter_by(login_id="2345678901").one()
            group_id, asha_id, bala_id, chitra_id = group.id, asha.id, bala.id, chitra.id
            attached_group = db.session.get(Group, group_id)
            self.assertEqual({member.user_id for member in attached_group.members}, {asha_id, bala_id})

        form = {"description": "Dinner", "paid_by": str(asha_id), "expense_type": "equal", "amount": "100.00",
                "shared_by": [str(asha_id), str(bala_id)]}
        self.client.post(f"/groups/{group_id}/expenses/new", data=form, follow_redirects=True)
        with self.app.app_context():
            expense = Expense.query.one()
            record = ExpenseRecord.query.one()
            group = db.session.get(Group, group_id)
            self.assertEqual(expense.paid_by_user_id, asha_id)
            self.assertEqual({split.user_id for split in expense.splits}, {asha_id, bala_id})
            self.assertTrue(all(split.member_id is not None for split in expense.splits))
            self.assertEqual(compute_user_balances(group)[asha_id], Decimal("50.00"))
            self.assertEqual(compute_user_balances(group)[bala_id], Decimal("-50.00"))

        page = self.client.get(f"/groups/{group_id}/expenses/{record.id}/edit")
        self.assertIn(b"0123456789", page.data)
        self.assertIn(b"1234567890", page.data)
        self.client.post(f"/groups/{group_id}/expenses/{record.id}/edit", data={
            "description": "Updated dinner", "paid_by": str(bala_id), "expense_type": "equal", "amount": "60.00",
            "shared_by": [str(asha_id), str(bala_id)],
        }, follow_redirects=True)
        with self.app.app_context():
            record = db.session.get(ExpenseRecord, record.id)
            expense = record.current_expense
            self.assertEqual(expense.paid_by_user_id, bala_id)
            self.assertEqual({split.user_id for split in expense.splits}, {asha_id, bala_id})
            self.assertEqual(ExpenseRevision.query.filter_by(expense_record_id=record.id).count(), 2)

        invalid_payer = dict(form, paid_by=str(chitra_id))
        response = self.client.post(f"/groups/{group_id}/expenses/new", data=invalid_payer, follow_redirects=True)
        self.assertIn(b"selected payer must belong", response.data)
        invalid_participant = dict(form, shared_by=[str(asha_id), str(chitra_id)])
        response = self.client.post(f"/groups/{group_id}/expenses/new", data=invalid_participant, follow_redirects=True)
        self.assertIn(b"valid group users", response.data)

    def test_legacy_members_and_expenses_remain_unmapped_and_usable(self):
        self.register("Owner", "0123456789")
        self.login("0123456789")
        with self.app.app_context():
            owner = User.query.filter_by(login_id="0123456789").one()
            group = Group(name="Historical", owner_id=owner.id)
            db.session.add(group)
            db.session.flush()
            db.session.add(GroupMembership(group_id=group.id, user_id=owner.id))
            legacy_a = Member(name="Rahul", group_id=group.id)
            legacy_b = Member(name="Rahul", group_id=group.id)
            db.session.add_all([legacy_a, legacy_b])
            db.session.flush()
            expense = Expense(description="Old bill", paid_by_id=legacy_a.id, amount=Decimal("40.00"), group_id=group.id)
            db.session.add(expense)
            db.session.flush()
            db.session.add_all([
                ExpenseSplit(expense_id=expense.id, member_id=legacy_a.id, amount_owed=Decimal("20.00")),
                ExpenseSplit(expense_id=expense.id, member_id=legacy_b.id, amount_owed=Decimal("20.00")),
            ])
            db.session.commit()
            initialize_database(self.app)
            self.assertIsNone(db.session.get(Member, legacy_a.id).user_id)
            self.assertIsNone(db.session.get(Member, legacy_b.id).user_id)
            self.assertEqual(compute_balances(group)[legacy_a.id], Decimal("20.00"))
            self.assertEqual(dict(compute_user_balances(group)), {})

    def test_user_can_participate_in_expenses_in_multiple_groups(self):
        self.register("Asha", "0123456789")
        self.register("Bala", "1234567890")
        self.login("0123456789")
        first, _ = self.create_group("First", ["1234567890"])
        second, _ = self.create_group("Second", ["1234567890"])
        with self.app.app_context():
            asha = User.query.filter_by(login_id="0123456789").one()
            bala = User.query.filter_by(login_id="1234567890").one()
            self.assertEqual(GroupMembership.query.filter_by(user_id=bala.id).count(), 2)
            asha_id, bala_id = asha.id, bala.id
            first_id, second_id = first.id, second.id
        for group_id in (first_id, second_id):
            self.client.post(f"/groups/{group_id}/expenses/new", data={
                "description": "Shared", "paid_by": str(asha_id), "expense_type": "equal", "amount": "20.00",
                "shared_by": [str(asha_id), str(bala_id)],
            })
        with self.app.app_context():
            expenses = Expense.query.order_by(Expense.group_id).all()
            self.assertEqual([expense.paid_by_user_id for expense in expenses], [asha_id, asha_id])
            self.assertEqual([{split.user_id for split in expense.splits} for expense in expenses],
                             [{asha_id, bala_id}, {asha_id, bala_id}])


if __name__ == "__main__":
    unittest.main()
