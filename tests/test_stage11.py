import unittest
from decimal import Decimal

from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from helpers import compute_pairwise_user_obligations
from models import ActivityEvent, ExpenseRecord, ExpenseRevision, Group, GroupMembership, User
import test_security


class Stage11Config(test_security.SecurityConfig):
    SECRET_KEY = "stage-11-test-secret"


class Stage11Tests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(Stage11Config)
        with self.app.app_context():
            db.drop_all()
            db.create_all()
            for name, login_id in (("Asha", "0123456789"), ("Bala", "1234567890"),
                                   ("Chitra", "2345678901"), ("Dara", "3456789012")):
                db.session.add(User(name=name, login_id=login_id,
                                    password_hash=generate_password_hash("secret")))
            db.session.commit()
            self.users = {user.login_id: user.id for user in User.query.all()}
        self.client = self.app.test_client()

    def token(self, path="/login"):
        self.client.get(path)
        with self.client.session_transaction() as session:
            return session["_csrf_token"]

    def post(self, path, data=None, *, token_path="/"):
        payload = dict(data or {})
        payload["csrf_token"] = self.token(token_path)
        return self.client.post(path, data=payload)

    def login(self, login_id):
        return self.post("/login", {"login_id": login_id, "password": "secret"}, token_path="/login")

    def logout(self):
        return self.post("/logout")

    def create_group(self, owner_login, name, member_logins):
        self.login(owner_login)
        self.post("/groups", {"name": name, "user_ids": member_logins})
        with self.app.app_context():
            return Group.query.filter_by(name=name).one().id

    def expense_form(self, description, payer_login, debtor_login, amount):
        return {
            "description": description,
            "paid_by": str(self.users[payer_login]),
            "expense_type": "custom",
            "custom_total": amount,
            f"custom_{self.users[debtor_login]}": amount,
        }

    def add_expense(self, group_id, description, payer_login, debtor_login, amount="50.00"):
        response = self.post(
            f"/groups/{group_id}/expenses/new",
            self.expense_form(description, payer_login, debtor_login, amount),
            token_path=f"/groups/{group_id}/expenses/new",
        )
        with self.app.app_context():
            record = ExpenseRecord.query.filter_by(group_id=group_id).order_by(ExpenseRecord.id.desc()).first()
            return response, record.id if record else None

    def test_creator_and_active_member_can_add_but_nonmember_and_archive_cannot(self):
        group_id = self.create_group("0123456789", "Trip", ["1234567890"])
        owner_response, owner_record_id = self.add_expense(
            group_id, "Owner dinner", "0123456789", "1234567890")
        self.assertEqual(owner_response.status_code, 302)
        self.logout()

        self.login("1234567890")
        self.assertIn(b"Add Expense", self.client.get(f"/groups/{group_id}").data)
        member_response, member_record_id = self.add_expense(
            group_id, "Member cab", "1234567890", "0123456789")
        self.assertEqual(member_response.status_code, 302)
        self.logout()

        self.login("2345678901")
        self.assertEqual(self.client.get(f"/groups/{group_id}/expenses/new").status_code, 403)
        forbidden = self.post(
            f"/groups/{group_id}/expenses/new",
            self.expense_form("Hidden", "0123456789", "1234567890", "10.00"),
        )
        self.assertEqual(forbidden.status_code, 403)

        with self.app.app_context():
            records = ExpenseRecord.query.order_by(ExpenseRecord.id).all()
            self.assertEqual([record.id for record in records], [owner_record_id, member_record_id])
            self.assertEqual(records[0].created_by_user_id, self.users["0123456789"])
            self.assertEqual(records[1].created_by_user_id, self.users["1234567890"])

        self.logout()
        self.login("0123456789")
        self.post(f"/groups/{group_id}/delete")
        self.assertEqual(self.client.get(f"/groups/{group_id}/expenses/new").status_code, 403)

    def test_only_expense_creator_can_edit_and_revision_history_remains_immutable(self):
        group_id = self.create_group("0123456789", "Trip", ["1234567890"])
        self.logout()
        self.login("1234567890")
        _, record_id = self.add_expense(group_id, "Cab", "1234567890", "0123456789", "50.00")
        original_expense_id = None
        with self.app.app_context():
            original_expense_id = db.session.get(ExpenseRecord, record_id).current_expense_id

        self.logout()
        self.login("0123456789")
        self.assertEqual(self.client.get(f"/groups/{group_id}/expenses/{record_id}/edit").status_code, 403)
        self.assertEqual(self.post(
            f"/groups/{group_id}/expenses/{record_id}/edit",
            self.expense_form("Owner attempt", "1234567890", "0123456789", "60.00"),
        ).status_code, 403)
        self.logout()
        self.login("2345678901")
        self.assertEqual(self.client.get(f"/groups/{group_id}/expenses/{record_id}/edit").status_code, 403)

        self.logout()
        self.login("1234567890")
        edited = self.post(
            f"/groups/{group_id}/expenses/{record_id}/edit",
            self.expense_form("Cab corrected", "1234567890", "0123456789", "70.00"),
            token_path=f"/groups/{group_id}/expenses/{record_id}/edit",
        )
        self.assertEqual(edited.status_code, 302)
        history = self.client.get(f"/groups/{group_id}/expenses/{record_id}/history")
        self.assertIn(b"Version 2", history.data)
        with self.app.app_context():
            record = db.session.get(ExpenseRecord, record_id)
            self.assertEqual(ExpenseRevision.query.filter_by(expense_record_id=record_id).count(), 2)
            self.assertNotEqual(record.current_expense_id, original_expense_id)
            self.assertEqual(db.session.get(ExpenseRevision, record.revisions[0].id).expense.amount,
                             Decimal("50.00"))
            self.assertEqual(record.current_expense.amount, Decimal("70.00"))

    def test_legacy_unknown_creator_is_viewable_but_not_editable(self):
        group_id = self.create_group("0123456789", "Legacy", ["1234567890"])
        _, record_id = self.add_expense(group_id, "Old", "0123456789", "1234567890")
        with self.app.app_context():
            record = db.session.get(ExpenseRecord, record_id)
            record.created_by_user_id = None
            record.revisions[0].recorded_by_user_id = None
            db.session.commit()
        self.assertEqual(self.client.get(f"/groups/{group_id}/expenses/{record_id}/history").status_code, 200)
        self.assertEqual(self.client.get(f"/groups/{group_id}/expenses/{record_id}/edit").status_code, 403)

    def test_create_and_edit_emit_structured_events_without_changing_accounting(self):
        group_id = self.create_group("0123456789", "Trip", ["1234567890"])
        _, record_id = self.add_expense(group_id, "Dinner", "0123456789", "1234567890", "50.00")
        with self.app.app_context():
            self.assertEqual(compute_pairwise_user_obligations()[0], {
                (self.users["1234567890"], self.users["0123456789"]): Decimal("50.00")})
            created = ActivityEvent.query.one()
            self.assertEqual(created.event_type, "expense_created")
            self.assertEqual(created.expense_record_id, record_id)
            self.assertEqual(created.details, {"description": "Dinner", "amount": "50.00"})

        self.post(
            f"/groups/{group_id}/expenses/{record_id}/edit",
            self.expense_form("Dinner corrected", "0123456789", "1234567890", "70.00"),
            token_path=f"/groups/{group_id}/expenses/{record_id}/edit",
        )
        with self.app.app_context():
            events = ActivityEvent.query.order_by(ActivityEvent.id).all()
            self.assertEqual([event.event_type for event in events], ["expense_created", "expense_revised"])
            self.assertEqual(events[1].details, {
                "description": "Dinner corrected", "before_amount": "50.00", "after_amount": "70.00"})
            self.assertEqual(compute_pairwise_user_obligations()[0], {
                (self.users["1234567890"], self.users["0123456789"]): Decimal("70.00")})

    def test_global_activity_is_newest_first_private_and_includes_visible_archives(self):
        first_group = self.create_group("0123456789", "Goa Trip", ["1234567890"])
        _, _ = self.add_expense(first_group, "First dinner", "0123456789", "1234567890")
        self.logout()
        second_group = self.create_group("1234567890", "Flat Expenses", ["2345678901"])
        _, _ = self.add_expense(second_group, "Second groceries", "1234567890", "2345678901")

        page = self.client.get("/activity").get_data(as_text=True)
        self.assertLess(page.index("Second groceries"), page.index("First dinner"))
        self.assertIn("Goa Trip", page)
        self.assertIn("Flat Expenses", page)

        self.logout()
        self.login("0123456789")
        self.post(f"/groups/{first_group}/delete")
        page = self.client.get("/activity").get_data(as_text=True)
        self.assertIn("First dinner", page)
        self.assertNotIn("Second groceries", page)

        self.logout()
        self.login("3456789012")
        page = self.client.get("/activity").get_data(as_text=True)
        self.assertNotIn("First dinner", page)
        self.assertNotIn("Second groceries", page)

    def test_activity_requires_authentication_and_expense_csrf_still_blocks_events(self):
        self.assertEqual(self.client.get("/activity").status_code, 302)
        group_id = self.create_group("0123456789", "Trip", ["1234567890"])
        response = self.client.post(
            f"/groups/{group_id}/expenses/new",
            data=self.expense_form("No token", "0123456789", "1234567890", "10.00"),
        )
        self.assertEqual(response.status_code, 400)
        with self.app.app_context():
            self.assertEqual(ExpenseRecord.query.count(), 0)
            self.assertEqual(ActivityEvent.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
