import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app import initialize_database
from extensions import db
from expense_history import void_expense_record
from helpers import compute_global_user_balances, compute_pairwise_user_obligations, parse_settlement_amount
from models import ConfirmedSettlement, ExpenseRevision, SettlementRequest, User
import test_stage4


class Stage5Tests(unittest.TestCase):
    setUp = test_stage4.Stage4Tests.setUp
    add_user = test_stage4.Stage4Tests.add_user
    add_group_with_expense = test_stage4.Stage4Tests.add_group_with_expense
    seed_a_owes_b = test_stage4.Stage4Tests.seed_a_owes_b
    login = test_stage4.Stage4Tests.login
    create_request = test_stage4.Stage4Tests.create_request
    revise_debt = test_stage4.Stage4Tests.revise_debt

    def prepare(self, amount='500.00', debt='500.00'):
        self.payer_id, self.recipient_id, self.expense_id = self.seed_a_owes_b(debt)
        self.login('0123456789')
        self.create_request(self.recipient_id, amount)

    def assert_position(self, outstanding):
        expected = Decimal(outstanding)
        balances, _ = compute_global_user_balances()
        self.assertEqual(balances[self.payer_id], -expected)
        self.assertEqual(balances[self.recipient_id], expected)
        self.assertEqual(sum(balances.values()), Decimal('0.00'))
        pair = (self.payer_id, self.recipient_id) if expected > 0 else (self.recipient_id, self.payer_id)
        self.assertEqual(compute_pairwise_user_obligations()[0], {pair: abs(expected)} if expected else {})

    def test_full_payment_snapshot_and_repeated_retired_resolution(self):
        self.prepare()
        with self.app.app_context():
            payment = ConfirmedSettlement.query.one()
            request = payment.settlement_request
            identifier = request.id
            self.assertEqual(payment.payer_user_id, self.payer_id)
            self.assertEqual(payment.recipient_user_id, self.recipient_id)
            self.assertEqual(payment.recorded_by_user_id, self.payer_id)
            self.assertEqual(payment.amount, Decimal('500.00'))
            self.assertIsInstance(payment.amount, Decimal)
            self.assertEqual(payment.confirmed_at, request.resolved_at)
            self.assertEqual(request.status, 'CONFIRMED')
            self.assert_position('0.00')
        for action in ('confirm', 'reject', 'confirm'):
            self.client.post(f'/settlement-requests/{identifier}/{action}')
        with self.app.app_context():
            self.assertEqual(ConfirmedSettlement.query.count(), 1)
            self.assertEqual(SettlementRequest.query.one().status, 'CONFIRMED')
            self.assert_position('0.00')

    def test_legacy_requests_are_neutral_and_retired_endpoints_preserve_them(self):
        self.payer_id, self.recipient_id, _ = self.seed_a_owes_b()
        with self.app.app_context():
            for status in ('PENDING', 'REJECTED', 'CONFIRMED'):
                db.session.add(SettlementRequest(requester_user_id=self.payer_id,
                    recipient_user_id=self.recipient_id, amount=Decimal('500.00'), status=status))
            db.session.commit()
            before = [(r.id, r.status, r.amount, r.resolved_at) for r in SettlementRequest.query.all()]
            self.assert_position('500.00')
        initialize_database(self.app)
        initialize_database(self.app)
        self.login('1234567890')
        for identifier, _, _, _ in before:
            for action in ('confirm', 'reject'):
                self.client.post(f'/settlement-requests/{identifier}/{action}')
        with self.app.app_context():
            self.assertEqual([(r.id, r.status, r.amount, r.resolved_at) for r in SettlementRequest.query.all()], before)
            self.assertEqual(ConfirmedSettlement.query.count(), 0)
            self.assert_position('500.00')

    def test_two_immediate_partial_payments_then_full(self):
        self.prepare('300.00')
        with self.app.app_context():
            self.assert_position('200.00')
        self.create_request(self.recipient_id, '200.00')
        with self.app.app_context():
            self.assert_position('0.00')
            self.assertEqual(ConfirmedSettlement.query.count(), 2)

    def test_stale_new_payment_after_payment_and_expense_change(self):
        self.prepare('300.00')
        with self.app.app_context():
            self.revise_debt(self.expense_id, '400.00')
        self.assertIn(b'exceeds the current outstanding obligation', self.create_request(self.recipient_id, '200.00').data)
        with self.app.app_context():
            self.assertEqual(ConfirmedSettlement.query.count(), 1)
            self.assert_position('100.00')

    def test_payment_survives_expense_revision_and_void(self):
        self.prepare('700.00', '700.00')
        with self.app.app_context():
            self.revise_debt(self.expense_id, '500.00')
            self.assert_position('-200.00')
            record = ExpenseRevision.query.filter_by(expense_id=self.expense_id).one().expense_record
            void_expense_record(record.id, record.group.owner_id)
            db.session.commit()
            self.assert_position('-700.00')
            self.assertEqual(ConfirmedSettlement.query.one().amount, Decimal('700.00'))

    def test_reciprocal_expenses_net_after_payment(self):
        self.prepare('300.00')
        with self.app.app_context():
            self.add_group_with_expense(db.session.get(User, self.payer_id),
                [db.session.get(User, self.recipient_id)], '50.00', ['50.00'])
            db.session.commit()
            self.assert_position('150.00')

    def test_decimal_precision_and_amount_bounds(self):
        self.prepare('0.10', '0.30')
        with self.app.app_context():
            self.assert_position('0.20')
        self.create_request(self.recipient_id, '0.20')
        with self.app.app_context():
            self.assert_position('0.00')
        for value in ('NaN', 'Infinity', '1.001', '1e100', '10000000000.00', '0', '-1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_settlement_amount(value)

    def test_unique_request_constraint(self):
        self.prepare()
        with self.app.app_context():
            db.session.add(ConfirmedSettlement(settlement_request_id=SettlementRequest.query.one().id,
                payer_user_id=self.payer_id, recipient_user_id=self.recipient_id,
                amount=Decimal('1.00'), confirmed_at=datetime.now(timezone.utc)))
            with self.assertRaises(IntegrityError):
                db.session.commit()
            db.session.rollback()
            self.assertEqual(ConfirmedSettlement.query.count(), 1)
            self.assert_position('0.00')

    def test_failure_after_flush_rolls_back_request_and_payment(self):
        self.payer_id, self.recipient_id, _ = self.seed_a_owes_b()
        self.login('0123456789')

        def fail_commit():
            db.session.flush()
            raise SQLAlchemyError('simulated commit failure after writes')

        with patch.object(db.session, 'commit', side_effect=fail_commit), self.assertLogs('routes', level='ERROR'):
            response = self.create_request(self.recipient_id, '100.00')
        self.assertIn(b'could not record', response.data)
        with self.app.app_context():
            self.assertEqual(SettlementRequest.query.count(), 0)
            self.assertEqual(ConfirmedSettlement.query.count(), 0)
            self.assert_position('500.00')

    def test_snapshot_accounting_independent_of_request_fields(self):
        self.prepare('300.00')
        with self.app.app_context():
            request = SettlementRequest.query.one()
            request.status = 'REJECTED'
            request.amount = Decimal('1.00')
            request.requester_user_id, request.recipient_user_id = self.recipient_id, self.payer_id
            db.session.commit()
            self.assert_position('200.00')
            self.assertEqual(ConfirmedSettlement.query.one().amount, Decimal('300.00'))

    def test_unresolved_expense_does_not_hide_payment(self):
        from expense_history import append_expense_revision, claim_active_expense_record
        from models import Expense, ExpenseSplit
        self.prepare('300.00')
        with self.app.app_context():
            old = db.session.get(Expense, self.expense_id)
            root = ExpenseRevision.query.filter_by(expense_id=old.id).one().expense_record
            record = claim_active_expense_record(root.id)
            snapshot = Expense(description=old.description, group_id=old.group_id, paid_by_id=old.paid_by_id,
                paid_by_user_id=old.paid_by_user_id, amount=old.amount, expense_type=old.expense_type)
            db.session.add(snapshot)
            db.session.flush()
            for split in old.splits:
                db.session.add(ExpenseSplit(expense_id=snapshot.id, member_id=split.member_id,
                                           user_id=None, amount_owed=split.amount_owed))
            append_expense_revision(record, snapshot, record.group.owner_id)
            db.session.commit()
            expected = [{'expense_id': record.current_expense_id, 'reason': 'split_unmapped'}]
            self.assertEqual(compute_global_user_balances()[1], expected)
            self.assertEqual(compute_pairwise_user_obligations()[1], expected)
            self.assert_position('-300.00')

    def test_database_rejects_invalid_payment_fields(self):
        self.payer_id, self.recipient_id, _ = self.seed_a_owes_b()
        with self.app.app_context():
            request = SettlementRequest(requester_user_id=self.payer_id, recipient_user_id=self.recipient_id,
                                        amount=Decimal('1.00'), status='PENDING')
            db.session.add(request)
            db.session.commit()
            request_id = request.id
        for overrides in ({'amount': Decimal('0.00')}, {'amount': Decimal('-1.00')},
                          {'recipient_user_id': self.payer_id}, {'settlement_request_id': None},
                          {'payer_user_id': None}, {'confirmed_at': None}):
            with self.subTest(overrides=overrides), self.app.app_context():
                fields = dict(settlement_request_id=request_id, payer_user_id=self.payer_id,
                    recipient_user_id=self.recipient_id, amount=Decimal('1.00'), confirmed_at=datetime.now(timezone.utc))
                fields.update(overrides)
                db.session.add(ConfirmedSettlement(**fields))
                with self.assertRaises(IntegrityError):
                    db.session.commit()
                db.session.rollback()
                self.assertEqual(ConfirmedSettlement.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
