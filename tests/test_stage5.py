import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app import create_app, initialize_database
from extensions import db
from expense_history import append_expense_revision, claim_active_expense_record, void_expense_record
from helpers import compute_global_user_balances, compute_pairwise_user_obligations, parse_settlement_amount
from models import ConfirmedSettlement, Expense, ExpenseRecord, ExpenseRevision, ExpenseSplit, SettlementRequest, User
import test_stage4


class Stage5Tests(unittest.TestCase):
    # Reuse only fixture helpers, without inheriting/re-running Stage-4 tests.
    setUp = test_stage4.Stage4Tests.setUp
    add_user = test_stage4.Stage4Tests.add_user
    add_group_with_expense = test_stage4.Stage4Tests.add_group_with_expense
    seed_a_owes_b = test_stage4.Stage4Tests.seed_a_owes_b
    login = test_stage4.Stage4Tests.login
    create_request = test_stage4.Stage4Tests.create_request
    revise_debt = test_stage4.Stage4Tests.revise_debt

    def prepare(self, amounts=("500.00",), debt="500.00"):
        self.payer_id, self.recipient_id, self.expense_id = self.seed_a_owes_b(debt)
        self.login("0123456789")
        for amount in amounts:
            self.create_request(self.recipient_id, amount)
        with self.app.app_context():
            ids = [row.id for row in SettlementRequest.query.order_by(SettlementRequest.id).all()]
        self.client.post("/logout")
        self.login("1234567890")
        return ids

    def confirm(self, request_id):
        return self.client.post(f"/settlement-requests/{request_id}/confirm", follow_redirects=True)

    def record_for_expense(self, expense_id):
        return ExpenseRevision.query.filter_by(expense_id=expense_id).one().expense_record

    def revise_as_unresolved(self, expense_id):
        old = db.session.get(Expense, expense_id)
        record = claim_active_expense_record(self.record_for_expense(expense_id).id)
        snapshot = Expense(description=old.description, group_id=old.group_id, paid_by_id=old.paid_by_id,
                           paid_by_user_id=old.paid_by_user_id, amount=old.amount,
                           expense_type=old.expense_type)
        db.session.add(snapshot)
        db.session.flush()
        for split in old.splits:
            db.session.add(ExpenseSplit(expense_id=snapshot.id, member_id=split.member_id,
                                        user_id=None, amount_owed=split.amount_owed))
        append_expense_revision(record, snapshot, record.group.owner_id)
        db.session.commit()
        return record

    def assert_position(self, outstanding):
        expected = Decimal(outstanding)
        balances, _ = compute_global_user_balances()
        self.assertEqual(balances[self.payer_id], -expected)
        self.assertEqual(balances[self.recipient_id], expected)
        self.assertEqual(sum(balances.values()), Decimal("0.00"))
        obligations, _ = compute_pairwise_user_obligations()
        pair = (self.payer_id, self.recipient_id) if expected > 0 else (self.recipient_id, self.payer_id)
        self.assertEqual(obligations, {pair: abs(expected)} if expected else {})

    def test_full_payment_snapshot_and_repeated_resolution(self):
        request_id, = self.prepare()
        self.confirm(request_id)
        with self.app.app_context():
            payment = ConfirmedSettlement.query.one()
            request = db.session.get(SettlementRequest, request_id)
            self.assertEqual(payment.settlement_request_id, request_id)
            self.assertEqual(payment.payer_user_id, self.payer_id)
            self.assertEqual(payment.recipient_user_id, self.recipient_id)
            self.assertEqual(payment.amount, Decimal("500.00"))
            self.assertIsInstance(payment.amount, Decimal)
            self.assertEqual(payment.confirmed_at, request.resolved_at)
            self.assertEqual(request.status, "CONFIRMED")
            self.assertEqual(payment.payer.login_id, "0123456789")
            self.assert_position("0.00")
        self.confirm(request_id)
        self.client.post(f"/settlement-requests/{request_id}/reject")
        with self.app.app_context():
            self.assertEqual(ConfirmedSettlement.query.count(), 1)
            self.assertEqual(db.session.get(SettlementRequest, request_id).status, "CONFIRMED")
            self.assert_position("0.00")

    def test_pending_and_rejected_are_neutral(self):
        request_id, = self.prepare()
        with self.app.app_context():
            self.assertEqual(ConfirmedSettlement.query.count(), 0)
            self.assert_position("500.00")
        self.client.post(f"/settlement-requests/{request_id}/reject")
        self.confirm(request_id)
        with self.app.app_context():
            self.assertEqual(ConfirmedSettlement.query.count(), 0)
            self.assert_position("500.00")

    def test_two_pending_partial_then_full(self):
        first, second = self.prepare(("300.00", "200.00"))
        self.confirm(first)
        with self.app.app_context():
            self.assert_position("200.00")
            self.assertEqual(db.session.get(SettlementRequest, second).status, "PENDING")
        self.confirm(second)
        with self.app.app_context():
            self.assert_position("0.00")
            self.assertEqual(ConfirmedSettlement.query.count(), 2)

    def test_stale_after_payment_and_expense_change(self):
        first, second = self.prepare(("300.00", "200.00"))
        self.confirm(first)
        with self.app.app_context():
            self.revise_debt(self.expense_id, "400.00")
        response = self.confirm(second)
        self.assertIn(b"stale", response.data)
        with self.app.app_context():
            request = db.session.get(SettlementRequest, second)
            self.assertEqual(request.status, "PENDING")
            self.assertEqual(request.amount, Decimal("200.00"))
            self.assertIsNone(request.resolved_at)
            self.assertEqual(ConfirmedSettlement.query.count(), 1)
            self.assert_position("100.00")

    def test_overpayment_reverses_and_payment_survives_expense_void(self):
        request_id, = self.prepare(("700.00",), debt="700.00")
        self.confirm(request_id)
        with self.app.app_context():
            self.revise_debt(self.expense_id, "500.00")
            self.assert_position("-200.00")
            record = self.record_for_expense(self.expense_id)
            void_expense_record(record.id, record.group.owner_id)
            db.session.commit()
            self.assert_position("-700.00")
            self.assertEqual(ConfirmedSettlement.query.one().amount, Decimal("700.00"))

    def test_reciprocal_expenses_net_after_payment(self):
        request_id, = self.prepare(("300.00",))
        self.confirm(request_id)
        with self.app.app_context():
            payer = db.session.get(User, self.payer_id)
            recipient = db.session.get(User, self.recipient_id)
            self.add_group_with_expense(payer, [recipient], "50.00", ["50.00"])
            db.session.commit()
            self.assert_position("150.00")

    def test_decimal_precision_and_amount_bounds(self):
        first, second = self.prepare(("0.10", "0.20"), debt="0.30")
        self.confirm(first)
        with self.app.app_context():
            self.assert_position("0.20")
        self.confirm(second)
        with self.app.app_context():
            self.assert_position("0.00")
        for value in ("NaN", "Infinity", "1.001", "1e100", "10000000000.00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_settlement_amount(value)

    def test_unique_request_constraint(self):
        request_id, = self.prepare()
        self.confirm(request_id)
        with self.app.app_context():
            db.session.add(ConfirmedSettlement(
                settlement_request_id=request_id, payer_user_id=self.payer_id,
                recipient_user_id=self.recipient_id, amount=Decimal("1.00"),
                confirmed_at=datetime.now(timezone.utc)))
            with self.assertRaises(IntegrityError):
                db.session.commit()
            db.session.rollback()
            self.assertEqual(ConfirmedSettlement.query.count(), 1)
            self.assert_position("0.00")

    def test_failure_after_flush_rolls_back_both_records(self):
        request_id, = self.prepare()

        def fail_commit():
            db.session.flush()
            raise SQLAlchemyError("simulated commit failure after writes")

        with patch.object(db.session, "commit", side_effect=fail_commit):
            with self.assertLogs("routes", level="ERROR"):
                response = self.confirm(request_id)
        self.assertIn(b"could not resolve", response.data)
        with self.app.app_context():
            request = db.session.get(SettlementRequest, request_id)
            self.assertEqual(request.status, "PENDING")
            self.assertIsNone(request.resolved_at)
            self.assertEqual(ConfirmedSettlement.query.count(), 0)
            self.assert_position("500.00")

    def test_historical_confirmation_not_backfilled_or_counted(self):
        request_id, = self.prepare()
        with self.app.app_context():
            request = db.session.get(SettlementRequest, request_id)
            request.status = "CONFIRMED"
            request.resolved_at = datetime.now(timezone.utc)
            db.session.commit()
        initialize_database(self.app)
        self.confirm(request_id)
        with self.app.app_context():
            self.assertEqual(ConfirmedSettlement.query.count(), 0)
            self.assert_position("500.00")

    def test_snapshot_accounting_independent_of_request_fields(self):
        request_id, = self.prepare(("300.00",))
        self.confirm(request_id)
        with self.app.app_context():
            # Deliberate out-of-band corruption proves accounting uses snapshots.
            request = db.session.get(SettlementRequest, request_id)
            request.status = "REJECTED"
            request.amount = Decimal("1.00")
            request.requester_user_id, request.recipient_user_id = self.recipient_id, self.payer_id
            db.session.commit()
            self.assert_position("200.00")
            self.assertEqual(ConfirmedSettlement.query.one().amount, Decimal("300.00"))

    def test_unresolved_expense_does_not_hide_confirmed_payment(self):
        request_id, = self.prepare(("300.00",))
        self.confirm(request_id)
        with self.app.app_context():
            record = self.revise_as_unresolved(self.expense_id)
            current_id = record.current_expense_id
            expected = [{"expense_id": current_id, "reason": "split_unmapped"}]
            self.assertEqual(compute_global_user_balances()[1], expected)
            self.assertEqual(compute_pairwise_user_obligations()[1], expected)
            self.assertIsNone(record.current_expense.splits[0].user_id)
            self.assert_position("-300.00")

    def test_database_rejects_invalid_payment_fields(self):
        request_id, = self.prepare()
        for overrides in ({"amount": Decimal("0.00")}, {"amount": Decimal("-1.00")},
                          {"recipient_user_id": self.payer_id}, {"settlement_request_id": None},
                          {"payer_user_id": None}, {"confirmed_at": None}):
            with self.subTest(overrides=overrides), self.app.app_context():
                fields = dict(settlement_request_id=request_id, payer_user_id=self.payer_id,
                              recipient_user_id=self.recipient_id, amount=Decimal("1.00"),
                              confirmed_at=datetime.now(timezone.utc))
                fields.update(overrides)
                db.session.add(ConfirmedSettlement(**fields))
                with self.assertRaises(IntegrityError):
                    db.session.commit()
                db.session.rollback()
                self.assertEqual(ConfirmedSettlement.query.count(), 0)

    def test_concurrent_confirmations_revalidate_after_writer_claim(self):
        # A file database gives each thread its own connection and real SQLite
        # writer contention; in-memory StaticPool would not test this correctly.
        with TemporaryDirectory() as directory:
            class FileConfig(test_stage4.TestConfig):
                SQLALCHEMY_DATABASE_URI = "sqlite:///" + (Path(directory) / "settlements.db").as_posix()

            self.app = create_app(FileConfig)
            self.client = self.app.test_client()
            try:
                first, second = self.prepare(("300.00", "300.00"), debt="600.00")
                with self.app.app_context():
                    self.revise_debt(self.expense_id, "500.00")
                clients = [self.app.test_client(), self.app.test_client()]
                for client in clients:
                    client.post("/login", data={"login_id": "1234567890", "password": "secret"})
                barrier = Barrier(2)
                original_get = db.get_or_404

                def simultaneous_read(model, identifier, **kwargs):
                    row = original_get(model, identifier, **kwargs)
                    if model is SettlementRequest:
                        barrier.wait(timeout=10)
                    return row

                with patch.object(db, "get_or_404", side_effect=simultaneous_read):
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        futures = [executor.submit(client.post, f"/settlement-requests/{identifier}/confirm",
                                                   follow_redirects=True)
                                   for client, identifier in zip(clients, (first, second))]
                        responses = [future.result(timeout=15) for future in futures]
                self.assertTrue(all(response.status_code == 200 for response in responses))
                self.assertEqual(sum(b"stale" in response.data for response in responses), 1)
                with self.app.app_context():
                    self.assertEqual(ConfirmedSettlement.query.count(), 1)
                    self.assertEqual(SettlementRequest.query.filter_by(status="PENDING").count(), 1)
                    self.assert_position("200.00")
            finally:
                with self.app.app_context():
                    db.session.remove()
                    db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
