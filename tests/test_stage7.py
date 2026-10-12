from test_factory import settlement_form_data
import unittest
from decimal import Decimal

from extensions import db
from helpers import compute_balances, compute_global_user_balances, compute_pairwise_user_obligations
from models import ConfirmedSettlement, Expense, ExpenseRecord, ExpenseRevision, Group, GroupMembership, SettlementRequest, User
import test_stage6


class Stage7Tests(unittest.TestCase):
    setUp = test_stage6.Stage6Tests.setUp
    register = test_stage6.Stage6Tests.register
    login = test_stage6.Stage6Tests.login
    seed = test_stage6.Stage6Tests.seed

    def archive(self, group_id):
        return self.client.post(f"/groups/{group_id}/delete", follow_redirects=True)

    def test_owner_archive_preserves_all_history_and_accounting(self):
        group_id, record_id, users = self.seed()
        with self.app.app_context():
            group = db.session.get(Group, group_id)
            before_global = dict(compute_global_user_balances()[0])
            before_pairwise = compute_pairwise_user_obligations()[0]
            before_group = dict(compute_balances(group))

        response = self.archive(group_id)
        self.assertIn(b"archived", response.data)
        with self.app.app_context():
            group = db.session.get(Group, group_id)
            self.assertTrue(group.is_archived)
            self.assertIsNotNone(group.archived_at)
            self.assertEqual(group.archived_by_user_id, users["0123456789"])
            self.assertEqual(GroupMembership.query.filter_by(group_id=group_id).count(), 3)
            self.assertEqual(ExpenseRecord.query.filter_by(group_id=group_id).count(), 1)
            self.assertEqual(ExpenseRevision.query.count(), 1)
            self.assertEqual(Expense.query.count(), 1)
            self.assertEqual(dict(compute_global_user_balances()[0]), before_global)
            self.assertEqual(compute_pairwise_user_obligations()[0], before_pairwise)
            self.assertEqual(dict(compute_balances(group)), before_group)

        self.assertNotIn(f'href="/groups/{group_id}"'.encode(), self.client.get("/").data)
        page = self.client.get(f"/groups/{group_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"changes are disabled", page.data)
        self.assertEqual(self.client.get(f"/groups/{group_id}/expenses/{record_id}/history").status_code, 200)

    def test_only_owner_can_archive(self):
        group_id, _, users = self.seed()
        self.client.post("/logout")
        self.login("1234567890")
        self.assertEqual(self.client.post(f"/groups/{group_id}/delete").status_code, 403)
        self.register("Dara", "3456789012")
        self.login("3456789012")
        self.assertEqual(self.client.post(f"/groups/{group_id}/delete").status_code, 403)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Group, group_id).archived_at)
            self.assertEqual(users["0123456789"], db.session.get(Group, group_id).owner_id)

    def test_archived_group_blocks_expense_mutation_but_keeps_member_history_access(self):
        group_id, record_id, users = self.seed()
        self.archive(group_id)
        self.assertEqual(self.client.get(f"/groups/{group_id}/expenses/new").status_code, 403)
        edit_data = {"description": "Nope", "paid_by": str(users["0123456789"]), "expense_type": "custom",
                     "custom_total": "500.00", f"custom_{users['1234567890']}": "500.00"}
        self.assertEqual(self.client.post(f"/groups/{group_id}/expenses/{record_id}/edit", data=edit_data).status_code, 403)
        self.assertEqual(self.client.post(f"/groups/{group_id}/expenses/{record_id}/delete").status_code, 403)
        with self.app.app_context():
            self.assertEqual(ExpenseRevision.query.count(), 1)
            self.assertEqual(ExpenseRecord.query.one().current_expense.amount, Decimal("500.00"))

        self.client.post("/logout")
        self.login("1234567890")
        self.assertEqual(self.client.get(f"/groups/{group_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/groups/{group_id}/expenses/{record_id}/history").status_code, 200)

    def test_archive_does_not_change_global_pending_or_confirmed_settlement_behavior(self):
        group_id, _, users = self.seed()
        self.client.post("/logout")
        self.login("1234567890")
        self.client.post("/settlement-requests", data=settlement_form_data(self.client, {"recipient_user_id": users["0123456789"], "amount": "500.00"}))
        with self.app.app_context():
            request_id = SettlementRequest.query.one().id
            self.assertEqual(SettlementRequest.query.one().status, "CONFIRMED")
        self.client.post("/logout")
        self.login("0123456789")
        self.archive(group_id)
        self.client.post(f"/settlement-requests/{request_id}/confirm")
        with self.app.app_context():
            request = db.session.get(SettlementRequest, request_id)
            payment = ConfirmedSettlement.query.one()
            self.assertEqual(request.status, "CONFIRMED")
            self.assertEqual(payment.amount, Decimal("500.00"))
            self.assertEqual(compute_global_user_balances()[0][users["0123456789"]], Decimal("0.00"))
            self.assertEqual(compute_global_user_balances()[0][users["1234567890"]], Decimal("0.00"))

    def test_empty_group_is_archived_not_physically_deleted(self):
        self.register("Asha", "0123456789")
        self.login("0123456789")
        self.client.post("/groups", data={"name": "Empty", "user_ids": []})
        with self.app.app_context():
            group_id = Group.query.filter_by(name="Empty").one().id
        self.archive(group_id)
        with self.app.app_context():
            group = db.session.get(Group, group_id)
            self.assertIsNotNone(group)
            self.assertTrue(group.is_archived)
            self.assertEqual(GroupMembership.query.filter_by(group_id=group_id).count(), 1)


if __name__ == "__main__":
    unittest.main()
