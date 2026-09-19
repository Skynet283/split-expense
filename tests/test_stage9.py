import unittest

from extensions import db
from models import Group, SettlementRequest, User

try:
    from tests import test_stage6
except ImportError:  # unittest discovery adds tests/ directly to sys.path.
    import test_stage6


class Stage9Tests(unittest.TestCase):
    setUp = test_stage6.Stage6Tests.setUp
    register = test_stage6.Stage6Tests.register
    login = test_stage6.Stage6Tests.login
    seed = test_stage6.Stage6Tests.seed
    edit = test_stage6.Stage6Tests.edit

    def promote(self, login_id):
        with self.app.app_context():
            user = User.query.filter_by(login_id=login_id).one()
            user.is_admin = True
            db.session.commit()

    def test_navigation_only_shows_management_link_to_backend_admin(self):
        self.register("Asha", "0123456789")
        self.register("Bala", "1234567890")
        self.login("0123456789")
        self.promote("0123456789")
        self.assertNotIn(b">User Management<", self.client.get("/").data)
        self.client.post("/logout")
        self.login("1234567890")
        self.assertNotIn(b">User Management<", self.client.get("/").data)
        self.client.post("/logout")
        self.client.post("/admin/login", data={"username": "test-admin", "password": "test-admin-password"})
        self.assertIn(b">User Management<", self.client.get("/admin").data)

    def test_empty_group_and_active_group_empty_states_are_clear(self):
        self.register("Asha", "0123456789")
        self.login("0123456789")
        self.assertIn(b"No active groups yet.", self.client.get("/").data)
        self.client.post("/groups", data={"name": "Empty", "user_ids": []})
        with self.app.app_context():
            group_id = Group.query.filter_by(name="Empty").one().id
        self.assertIn(b"No active expenses yet.", self.client.get(f"/groups/{group_id}").data)

    def test_group_actions_use_clear_revision_and_void_labels(self):
        group_id, record_id, _ = self.seed()
        page = self.client.get(f"/groups/{group_id}")
        self.assertIn(b"Edit", page.data)
        self.assertIn(b"Void", page.data)
        self.assertIn(b"History", page.data)
        edit_page = self.client.get(f"/groups/{group_id}/expenses/{record_id}/edit")
        self.assertIn(b"Saving creates a new revision", edit_page.data)
        self.assertIn(b"Save New Revision", edit_page.data)

    def test_archived_group_has_read_only_presentation(self):
        group_id, _, _ = self.seed()
        self.client.post(f"/groups/{group_id}/delete")
        page = self.client.get(f"/groups/{group_id}")
        self.assertIn(b"Archived", page.data)
        self.assertIn(b"changes are disabled", page.data)
        self.assertNotIn(b">Add Expense<", page.data)
        self.assertNotIn(b">Edit<", page.data)
        self.assertNotIn(b">Void<", page.data)

    def test_expense_history_displays_multiple_versions_and_actor(self):
        group_id, record_id, users = self.seed()
        self.edit(group_id, record_id, users, description="Corrected dinner")
        page = self.client.get(f"/groups/{group_id}/expenses/{record_id}/history")
        self.assertIn(b"Version 1", page.data)
        self.assertIn(b"Version 2", page.data)
        self.assertIn(b"Current version", page.data)
        self.assertIn(b"Recorded", page.data)
        self.assertIn(b"by Asha", page.data)
        self.assertIn(b"Corrected dinner", page.data)

    def test_settlement_statuses_and_recipient_actions_are_presentational(self):
        group_id, _, users = self.seed()
        self.client.post("/logout")
        self.login("1234567890")
        self.client.post("/settlement-requests", data={"recipient_user_id": users["0123456789"], "amount": "200.00"})
        requester_page = self.client.get("/")
        self.assertIn(b"Settlement Requests", requester_page.data)
        self.assertIn(b"Pending", requester_page.data)
        self.assertIn(b"Awaiting confirmation", requester_page.data)
        self.assertNotIn(b"Confirm payment", requester_page.data)

        self.client.post("/logout")
        self.login("0123456789")
        recipient_page = self.client.get("/")
        self.assertIn(b"Confirm payment", recipient_page.data)
        self.assertIn(b"Reject request", recipient_page.data)
        with self.app.app_context():
            first_id = SettlementRequest.query.one().id
        self.client.post(f"/settlement-requests/{first_id}/confirm")

        self.client.post("/logout")
        self.login("1234567890")
        self.client.post("/settlement-requests", data={"recipient_user_id": users["0123456789"], "amount": "100.00"})
        self.client.post("/logout")
        self.login("0123456789")
        with self.app.app_context():
            second_id = SettlementRequest.query.order_by(SettlementRequest.id.desc()).first().id
        self.client.post(f"/settlement-requests/{second_id}/reject")
        page = self.client.get("/")
        self.assertIn(b"Confirmed", page.data)
        self.assertIn(b"Rejected", page.data)
        self.assertIn(b"This request is read-only.", page.data)
        self.assertNotIn(b"Confirm payment", page.data)


if __name__ == "__main__":
    unittest.main()
