import unittest
from decimal import Decimal

from app import create_app
from config import Config
from extensions import db
from expense_history import create_expense_record
from helpers import compute_balances, compute_global_user_balances
from models import Expense, ExpenseSplit, Group, GroupMembership, Member, User


class TestConfig(Config):
    TESTING = True
    CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    INITIAL_ADMIN_ID = "9999999999"
    INITIAL_ADMIN_PASSWORD = "test-admin-password"
    INITIAL_ADMIN_NAME = "Test Administrator"


class Stage3Tests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        with self.app.app_context():
            db.drop_all()
            db.create_all()

    def add_user(self, name, login_id):
        user = User(name=name, login_id=login_id, password_hash="test-hash")
        db.session.add(user)
        db.session.flush()
        return user

    def add_group(self, name, users):
        group = Group(name=name, owner_id=users[0].id)
        db.session.add(group)
        db.session.flush()
        members = {}
        for user in users:
            db.session.add(GroupMembership(group_id=group.id, user_id=user.id))
            member = Member(name=user.name, group_id=group.id, user_id=user.id)
            db.session.add(member)
            db.session.flush()
            members[user.id] = member
        return group, members

    def add_global_expense(self, group, members, payer, amount, splits, expense_type="custom"):
        expense = Expense(description="test", group_id=group.id, paid_by_id=members[payer.id].id,
                          paid_by_user_id=payer.id, amount=Decimal(amount), expense_type=expense_type)
        db.session.add(expense)
        db.session.flush()
        create_expense_record(expense, payer.id)
        for user, owed in splits:
            db.session.add(ExpenseSplit(expense_id=expense.id, member_id=members[user.id].id,
                                        user_id=user.id, amount_owed=Decimal(owed)))
        return expense

    def test_single_and_multiple_expenses_use_paid_minus_owed_semantics(self):
        with self.app.app_context():
            asha = self.add_user("Asha", "0123456789")
            bala = self.add_user("Bala", "1234567890")
            chitra = self.add_user("Chitra", "2345678901")
            group, members = self.add_group("Trip", [asha, bala, chitra])
            self.add_global_expense(group, members, asha, "1000.00", [(asha, "500.00"), (bala, "500.00")])
            self.add_global_expense(group, members, bala, "90.00", [(asha, "30.00"), (bala, "60.00")])
            self.add_global_expense(group, members, chitra, "10.00", [(chitra, "10.00")])
            db.session.commit()

            balances, unresolved = compute_global_user_balances()
            self.assertEqual(balances[asha.id], Decimal("470.00"))
            self.assertEqual(balances[bala.id], Decimal("-470.00"))
            self.assertEqual(balances[chitra.id], Decimal("0.00"))
            self.assertEqual(sum(balances.values()), Decimal("0.00"))
            self.assertEqual(unresolved, [])

    def test_multiple_groups_equal_splits_and_decimal_precision(self):
        with self.app.app_context():
            asha = self.add_user("Asha", "0123456789")
            bala = self.add_user("Bala", "1234567890")
            chitra = self.add_user("Chitra", "2345678901")
            first, first_members = self.add_group("First", [asha, bala, chitra])
            second, second_members = self.add_group("Second", [asha, bala])
            self.add_global_expense(first, first_members, asha, "0.30",
                                    [(asha, "0.10"), (bala, "0.10"), (chitra, "0.10")], expense_type="equal")
            self.add_global_expense(second, second_members, bala, "600.00", [(asha, "600.00")])
            db.session.commit()

            balances, unresolved = compute_global_user_balances()
            self.assertEqual(balances[asha.id], Decimal("-599.80"))
            self.assertEqual(balances[bala.id], Decimal("599.90"))
            self.assertEqual(balances[chitra.id], Decimal("-0.10"))
            self.assertEqual(sum(balances.values()), Decimal("0.00"))
            self.assertEqual(unresolved, [])

    def test_unmapped_and_partially_mapped_expenses_are_excluded_and_reported(self):
        with self.app.app_context():
            asha = self.add_user("Asha", "0123456789")
            bala = self.add_user("Bala", "1234567890")
            group, members = self.add_group("Legacy", [asha, bala])
            mapped = self.add_global_expense(group, members, asha, "20.00", [(asha, "10.00"), (bala, "10.00")])

            legacy_a = Member(name="Rahul", group_id=group.id)
            legacy_b = Member(name="Rahul", group_id=group.id)
            db.session.add_all([legacy_a, legacy_b])
            db.session.flush()
            unmapped = Expense(description="old", group_id=group.id, paid_by_id=legacy_a.id, amount=Decimal("40.00"))
            db.session.add(unmapped)
            db.session.flush()
            create_expense_record(unmapped, None)
            db.session.add(ExpenseSplit(expense_id=unmapped.id, member_id=legacy_b.id, amount_owed=Decimal("40.00")))

            partial = Expense(description="partial", group_id=group.id, paid_by_id=members[asha.id].id,
                              paid_by_user_id=asha.id, amount=Decimal("30.00"))
            db.session.add(partial)
            db.session.flush()
            create_expense_record(partial, None)
            db.session.add(ExpenseSplit(expense_id=partial.id, member_id=legacy_b.id, amount_owed=Decimal("30.00")))
            db.session.commit()

            balances, unresolved = compute_global_user_balances()
            self.assertEqual(dict(balances), {asha.id: Decimal("10.00"), bala.id: Decimal("-10.00")})
            self.assertEqual({record["expense_id"] for record in unresolved}, {unmapped.id, partial.id})
            self.assertEqual({record["reason"] for record in unresolved}, {"payer_unmapped", "split_unmapped"})
            self.assertIsNone(db.session.get(Member, legacy_a.id).user_id)
            self.assertEqual(compute_balances(group)[legacy_a.id], Decimal("40.00"))
            self.assertEqual(mapped.paid_by_user_id, asha.id)


if __name__ == "__main__":
    unittest.main()
