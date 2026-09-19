import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from decimal import Decimal
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
from unittest.mock import patch

import openpyxl
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app import create_app, initialize_database
from extensions import db
from expense_history import iter_active_expenses
from helpers import compute_global_user_balances, compute_pairwise_user_obligations
from models import (ConfirmedSettlement, Expense, ExpenseRecord, ExpenseRevision,
                    ExpenseSplit, Group, GroupMembership, Member, SettlementRequest, User)
import routes
import test_stage6


class Stage10Tests(unittest.TestCase):
    setUp = test_stage6.Stage6Tests.setUp
    register = test_stage6.Stage6Tests.register
    login = test_stage6.Stage6Tests.login
    seed = test_stage6.Stage6Tests.seed
    edit = test_stage6.Stage6Tests.edit

    def switch_user(self, login_id):
        self.client.post('/logout')
        self.login(login_id)

    def request_payment(self, users, amount):
        self.switch_user('1234567890')
        response = self.client.post('/settlement-requests', data={
            'recipient_user_id': users['0123456789'], 'amount': amount})
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            request_id = SettlementRequest.query.order_by(SettlementRequest.id.desc()).first().id
        self.switch_user('0123456789')
        return request_id

    def database_state(self):
        with self.app.app_context():
            return {model.__tablename__: list(db.session.execute(select(model.__table__).order_by(model.id)))
                    for model in (User, Group, GroupMembership, Member, Expense, ExpenseSplit,
                                  ExpenseRecord, ExpenseRevision, SettlementRequest, ConfirmedSettlement)}

    def assert_balances(self, users, values):
        with self.app.app_context():
            balances, unresolved = compute_global_user_balances()
            obligations, pairwise_unresolved = compute_pairwise_user_obligations()
            self.assertEqual(sum(balances.values()), Decimal('0.00'))
            self.assertEqual(unresolved, pairwise_unresolved)
            reconstructed = {identifier: Decimal('0.00') for identifier in users.values()}
            for (debtor, creditor), amount in obligations.items():
                self.assertGreater(amount, 0)
                reconstructed[debtor] -= amount
                reconstructed[creditor] += amount
            for login_id, value in zip(('0123456789', '1234567890', '2345678901'), values):
                self.assertEqual(balances[users[login_id]], Decimal(value))
                self.assertEqual(reconstructed[users[login_id]], Decimal(value))

    def test_multigroup_partial_payment_correction_void_archive_and_legacy_coexist(self):
        group_id, record_id, users = self.seed('0.30')
        self.client.post('/groups', data={'name': 'Second', 'user_ids': ['1234567890', '2345678901']})
        with self.app.app_context():
            second_id = Group.query.filter_by(name='Second').one().id
        self.client.post(f'/groups/{second_id}/expenses/new', data={
            'paid_by': users['1234567890'], 'expense_type': 'equal', 'amount': '0.05',
            'shared_by': [users['0123456789'], users['2345678901']]})
        request_id = self.request_payment(users, '0.10')
        self.client.post(f'/settlement-requests/{request_id}/confirm')
        self.assert_balances(users, ('0.17', '-0.15', '-0.02'))
        with self.app.app_context():
            payment_before = tuple(db.session.execute(select(ConfirmedSettlement.__table__)).one())
            old = db.session.get(ExpenseRecord, record_id).current_expense
            original_id = old.id
            legacy = Member(group_id=second_id, name='Asha')  # Same name is not identity evidence.
            db.session.add(legacy)
            db.session.flush()
            expense = Expense(group_id=second_id, paid_by_id=legacy.id, amount=Decimal('99.99'))
            db.session.add(expense)
            db.session.flush()
            db.session.add(ExpenseSplit(expense_id=expense.id, member_id=legacy.id, amount_owed=Decimal('99.99')))
            db.session.commit()
            legacy_id = expense.id
        initialize_database(self.app)
        self.edit(group_id, record_id, users, total='0.07')
        self.assert_balances(users, ('-0.06', '0.08', '-0.02'))
        self.client.post(f'/groups/{group_id}/expenses/{record_id}/delete')
        self.assert_balances(users, ('-0.13', '0.15', '-0.02'))
        self.client.post(f'/groups/{group_id}/delete')
        self.client.post(f'/groups/{second_id}/delete')
        self.assert_balances(users, ('-0.13', '0.15', '-0.02'))
        with self.app.app_context():
            self.assertEqual(tuple(db.session.execute(select(ConfirmedSettlement.__table__)).one()), payment_before)
            self.assertEqual(db.session.get(Expense, original_id).amount, Decimal('0.30'))
            self.assertEqual(len(db.session.get(ExpenseRecord, record_id).revisions), 2)
            self.assertEqual(len(list(iter_active_expenses())), 2)
            self.assertEqual(compute_global_user_balances()[1], [{'expense_id': legacy_id, 'reason': 'payer_unmapped'}])
            self.assertIsNone(db.session.get(Expense, legacy_id).paid_by_user_id)
            self.assertEqual(GroupMembership.query.count(), 6)

    def test_nonfinite_expense_inputs_are_validation_errors_and_leave_no_rows(self):
        group_id, record_id, users = self.seed()
        before = self.database_state()
        for amount in ('NaN', 'sNaN', 'Infinity', '-Infinity', '1e1000'):
            for endpoint in ('new', f'{record_id}/edit'):
                with self.subTest(amount=amount, endpoint=endpoint):
                    response = self.client.post(f'/groups/{group_id}/expenses/{endpoint}', data={
                        'paid_by': users['0123456789'], 'expense_type': 'equal', 'amount': amount,
                        'shared_by': [users['1234567890']]}, follow_redirects=True)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(b'Enter a valid amount', response.data)
                    self.assertEqual(self.database_state(), before)

    def test_write_failures_rollback_every_uncovered_mutation(self):
        group_id, record_id, users = self.seed()
        form = {'description': 'Changed', 'paid_by': users['0123456789'], 'expense_type': 'equal',
                'amount': '10.00', 'shared_by': [users['1234567890']]}
        mutations = [(f'/groups/{group_id}/expenses/new', form),
                     (f'/groups/{group_id}/expenses/{record_id}/edit', form),
                     (f'/groups/{group_id}/expenses/{record_id}/delete', {}),
                     (f'/groups/{group_id}/delete', {}),
                     ('/groups', {'name': 'Will fail', 'user_ids': ['1234567890']})]

        def fail_commit():
            db.session.flush()
            raise SQLAlchemyError('simulated failure after flush')

        for endpoint, form in mutations:
            before = self.database_state()
            with self.subTest(endpoint=endpoint), patch.object(db.session, 'commit', side_effect=fail_commit):
                with self.assertLogs('routes', level='ERROR'):
                    response = self.client.post(endpoint, data=form)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self.database_state(), before)
        self.switch_user('1234567890')
        before = self.database_state()
        with patch.object(db.session, 'commit', side_effect=fail_commit), self.assertLogs('routes', level='ERROR'):
            self.client.post('/settlement-requests', data={'recipient_user_id': users['0123456789'], 'amount': '10.00'})
        self.assertEqual(self.database_state(), before)
        self.client.post('/logout')
        self.client.post('/admin/login', data={'username': 'test-admin', 'password': 'test-admin-password'})
        with patch.object(db.session, 'commit', side_effect=fail_commit), self.assertLogs('routes', level='ERROR'):
            response = self.client.post('/admin/users', data={'name': 'Dara', 'login_id': '3456789012',
                                                               'password': 'secret', 'confirm_password': 'secret'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.database_state(), before)

    def test_authentication_cross_group_admin_and_http_method_boundaries(self):
        group_id, record_id, users = self.seed()
        self.client.post('/groups', data={'name': 'Private'})
        with self.app.app_context():
            other_id = Group.query.filter_by(name='Private').one().id
        for suffix, method in (('edit', 'get'), ('edit', 'post'), ('delete', 'post'), ('history', 'get')):
            response = getattr(self.client, method)(f'/groups/{other_id}/expenses/{record_id}/{suffix}')
            self.assertEqual(response.status_code, 404)
        writes = [('/groups', {}), (f'/groups/{group_id}/expenses/new', {}),
                  (f'/groups/{group_id}/expenses/{record_id}/edit', {}),
                  (f'/groups/{group_id}/expenses/{record_id}/delete', {}),
                  (f'/groups/{group_id}/delete', {}), ('/settlement-requests', {}),
                  ('/settlement-requests/999/confirm', {}), ('/settlement-requests/999/reject', {}), ('/logout', {})]
        self.client.post('/logout')
        before = self.database_state()
        for endpoint, form in writes:
            self.assertEqual(self.client.post(endpoint, data=form).status_code, 302)
        self.assertEqual(self.database_state(), before)
        self.login('1234567890')
        with self.app.app_context():
            db.session.get(User, users['1234567890']).is_admin = True
            db.session.commit()
        self.assertEqual(self.client.get('/admin').status_code, 403)
        created = self.client.post(f'/groups/{group_id}/expenses/new', data={
            'description': 'Member-created expense', 'paid_by': users['1234567890'],
            'expense_type': 'equal', 'amount': '10.00',
            'shared_by': [users['0123456789'], users['1234567890']],
        }, follow_redirects=True)
        self.assertEqual(created.status_code, 200)
        self.assertIn(b'Expense added!', created.data)
        for endpoint in (f'/groups/{group_id}/expenses/{record_id}/edit',
                         f'/groups/{group_id}/expenses/{record_id}/delete', f'/groups/{group_id}/delete'):
            self.assertEqual(self.client.post(endpoint).status_code, 403)
        for endpoint in (f'/groups/{other_id}', f'/groups/{other_id}/export'):
            self.assertEqual(self.client.get(endpoint).status_code, 403)
        self.assertEqual(self.client.post('/admin').status_code, 405)
        self.assertEqual(self.client.get('/groups/999999').status_code, 404)
        for endpoint in (f'/groups/{group_id}/delete', f'/groups/{group_id}/expenses/{record_id}/delete',
                         '/settlement-requests/999/confirm', '/settlement-requests/999/reject', '/logout'):
            self.assertEqual(self.client.get(endpoint).status_code, 405)

    def test_redirect_and_user_text_do_not_become_executable_content(self):
        group_id, record_id, users = self.seed()
        with self.app.test_request_context():
            for value in ('//evil.example', '/\\evil.example', '/\n/evil.example', '/\t/evil.example'):
                self.assertEqual(routes.safe_next_url(value), '/')
            self.assertEqual(routes.safe_next_url('/groups/1?x=1'), '/groups/1?x=1')
        self.edit(group_id, record_id, users, description='=1+1')
        workbook = openpyxl.load_workbook(BytesIO(self.client.get(f'/groups/{group_id}/export').data))
        self.assertEqual(workbook['Expenses Log']['C2'].value, '=1+1')
        self.assertEqual(workbook['Expenses Log']['C2'].data_type, 's')
        payload = "');alert(1);//"
        self.client.post('/groups', data={'name': payload})

        class Handlers(HTMLParser):
            def __init__(self):
                super().__init__()
                self.values = []

            def handle_starttag(self, tag, attrs):
                self.values.extend(value for name, value in attrs if name.startswith('on'))

        parser = Handlers()
        parser.feed(self.client.get('/').get_data(as_text=True))
        self.assertTrue(parser.values)
        self.assertTrue(all(payload not in value for value in parser.values))
        self.client.post(f'/groups/{group_id}/expenses/{record_id}/delete')
        self.client.post(f'/groups/{group_id}/delete')
        self.assertNotIn(f'href="/groups/{group_id}/expenses/new"'.encode(), self.client.get(f'/groups/{group_id}').data)

    def test_bootstrap_requires_explicit_credentials_and_never_promotes(self):
        class NoBootstrap(test_stage6.TestConfig):
            INITIAL_ADMIN_ID = None
            INITIAL_ADMIN_PASSWORD = None
            SECRET_KEY = None

        app = create_app(NoBootstrap)
        with app.app_context():
            self.assertEqual(User.query.count(), 0)
            self.assertTrue(app.secret_key)
        initialize_database(app)
        with app.app_context():
            self.assertEqual(User.query.count(), 0)
        app.config['INITIAL_ADMIN_ID'] = '3456789012'
        with self.assertRaisesRegex(ValueError, 'INITIAL_ADMIN_PASSWORD'):
            initialize_database(app)
        app.config['INITIAL_ADMIN_PASSWORD'] = 'explicit-test-password'
        initialize_database(app)
        with app.app_context():
            user = User.query.one()
            self.assertFalse(user.is_admin)
            original_hash = user.password_hash
        app.config['INITIAL_ADMIN_PASSWORD'] = 'must-not-reset-existing-password'
        initialize_database(app)
        with app.app_context():
            self.assertEqual(User.query.one().password_hash, original_hash)

    def test_full_legacy_upgrade_is_repeatable_and_preserves_financial_rows(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / 'legacy.db'
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript('''
                    CREATE TABLE users (id INTEGER PRIMARY KEY, login_id VARCHAR(10) UNIQUE NOT NULL, password_hash TEXT NOT NULL);
                    INSERT INTO users VALUES (7, '0123456789', 'legacy-hash');
                    CREATE TABLE "group" (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
                    INSERT INTO "group" VALUES (11, 'Legacy');
                    CREATE TABLE member (id INTEGER PRIMARY KEY, name TEXT NOT NULL, group_id INTEGER NOT NULL);
                    INSERT INTO member VALUES (13, 'Asha', 11);
                    CREATE TABLE expense (id INTEGER PRIMARY KEY, description TEXT, paid_by_id INTEGER NOT NULL,
                        amount NUMERIC NOT NULL, expense_type TEXT, group_id INTEGER NOT NULL, created_at DATETIME);
                    INSERT INTO expense VALUES (17, 'Old', 13, 12.34, 'custom', 11, '2020-01-01 00:00:00');
                    CREATE TABLE expense_split (id INTEGER PRIMARY KEY, expense_id INTEGER NOT NULL,
                        member_id INTEGER NOT NULL, amount_owed NUMERIC NOT NULL);
                    INSERT INTO expense_split VALUES (19, 17, 13, 12.34);
                ''')
                original = connection.execute('SELECT * FROM expense').fetchall()

            class LegacyConfig(test_stage6.TestConfig):
                SQLALCHEMY_DATABASE_URI = 'sqlite:///' + database.as_posix()
                INITIAL_ADMIN_ID = '0123456789'

            app = create_app(LegacyConfig)
            try:
                initialize_database(app)
                initialize_database(app)
                with app.app_context():
                    self.assertEqual(User.query.count(), 1)
                    self.assertFalse(User.query.one().is_admin)
                    self.assertEqual(GroupMembership.query.count(), 1)
                    self.assertEqual(Group.query.one().owner_id, 7)
                    self.assertIsNone(Group.query.one().archived_at)
                    self.assertIsNone(Member.query.one().user_id)
                    self.assertEqual(ExpenseRecord.query.one().id, 17)
                    self.assertEqual(ExpenseRevision.query.one().version_number, 1)
                    self.assertEqual(ConfirmedSettlement.query.count(), 0)
                    self.assertEqual(SettlementRequest.query.count(), 0)
                    self.assertEqual(dict(compute_global_user_balances()[0]), {})
                    rows = db.session.execute(text('SELECT id, description, paid_by_id, amount, expense_type, group_id, created_at FROM expense')).all()
                    self.assertEqual([tuple(row) for row in rows], original)
                    self.assertEqual(db.session.execute(text('PRAGMA foreign_keys')).scalar(), 0)
            finally:
                with app.app_context():
                    db.session.remove()
                    db.engine.dispose()

    def test_backfill_failure_rolls_back_entire_batch_and_retry_completes(self):
        group_id, _, _ = self.seed()
        with self.app.app_context():
            member = Member.query.filter_by(group_id=group_id).first()
            for description in ('Legacy one', 'Legacy two'):
                db.session.add(Expense(group_id=group_id, paid_by_id=member.id, amount=Decimal('1.00'), description=description))
            db.session.commit()
        before = self.database_state()
        original_add = db.session.add
        revisions = 0

        def fail_second_revision(row, *args, **kwargs):
            nonlocal revisions
            if isinstance(row, ExpenseRevision):
                revisions += 1
                if revisions == 2:
                    raise SQLAlchemyError('simulated interrupted backfill')
            return original_add(row, *args, **kwargs)

        # Prevent the optional test bootstrap account from affecting the comparison.
        self.app.config['INITIAL_ADMIN_ID'] = None
        with patch.object(db.session, 'add', side_effect=fail_second_revision):
            with self.assertRaises(SQLAlchemyError):
                initialize_database(self.app)
        self.assertEqual(self.database_state(), before)
        initialize_database(self.app)
        initialize_database(self.app)
        with self.app.app_context():
            self.assertEqual(ExpenseRecord.query.count(), 3)
            self.assertEqual(ExpenseRevision.query.count(), 3)

    def file_app(self, directory):
        class FileConfig(test_stage6.TestConfig):
            SQLALCHEMY_DATABASE_URI = 'sqlite:///' + (Path(directory) / 'release.db').as_posix()

        self.app = create_app(FileConfig)
        self.client = self.app.test_client()

    def dispose(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def test_concurrent_request_creation_cannot_exceed_pending_limit(self):
        with TemporaryDirectory() as directory:
            self.file_app(directory)
            try:
                _, _, users = self.seed()
                clients = [self.app.test_client(), self.app.test_client()]
                for client in clients:
                    client.post('/login', data={'login_id': '1234567890', 'password': 'secret'})
                barrier = Barrier(2)
                original_parse = routes.parse_settlement_amount

                def simultaneous_parse(value):
                    amount = original_parse(value)
                    barrier.wait(timeout=10)
                    return amount

                with patch.object(routes, 'parse_settlement_amount', side_effect=simultaneous_parse):
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        futures = [executor.submit(client.post, '/settlement-requests', data={
                            'recipient_user_id': users['0123456789'], 'amount': '300.00'}) for client in clients]
                        self.assertEqual([future.result(timeout=15).status_code for future in futures], [302, 302])
                with self.app.app_context():
                    self.assertEqual(SettlementRequest.query.count(), 1)
                    self.assertEqual(ConfirmedSettlement.query.count(), 0)
            finally:
                self.dispose()

    def test_archive_between_initial_read_and_expense_write_blocks_each_mutation(self):
        for action in ('new', 'edit', 'delete'):
            with self.subTest(action=action), TemporaryDirectory() as directory:
                self.file_app(directory)
                try:
                    group_id, record_id, users = self.seed()
                    writer = self.app.test_client()
                    writer.post('/login', data={'login_id': '0123456789', 'password': 'secret'})
                    reached, archived = Event(), Event()
                    original_claim = routes.claim_active_group

                    def delayed_claim(group):
                        reached.set()
                        if not archived.wait(timeout=10):
                            raise RuntimeError('archive did not complete')
                        return original_claim(group)

                    suffix = 'new' if action == 'new' else f'{record_id}/{action}'
                    with patch.object(routes, 'claim_active_group', side_effect=delayed_claim):
                        with ThreadPoolExecutor(max_workers=1) as executor:
                            future = executor.submit(writer.post, f'/groups/{group_id}/expenses/{suffix}', data={
                                'paid_by': users['0123456789'], 'expense_type': 'equal', 'amount': '600.00',
                                'shared_by': [users['1234567890']]})
                            try:
                                self.assertTrue(reached.wait(timeout=10))
                                self.assertEqual(self.client.post(f'/groups/{group_id}/delete').status_code, 302)
                            finally:
                                archived.set()
                            self.assertEqual(future.result(timeout=15).status_code, 403)
                    with self.app.app_context():
                        self.assertTrue(db.session.get(Group, group_id).is_archived)
                        self.assertEqual(Expense.query.count(), 1)
                        self.assertEqual(ExpenseRevision.query.count(), 1)
                        self.assertEqual(ExpenseRecord.query.one().current_expense.amount, Decimal('500.00'))
                finally:
                    self.dispose()

    def test_concurrent_confirmation_and_correction_have_a_valid_serial_outcome(self):
        with TemporaryDirectory() as directory:
            self.file_app(directory)
            try:
                group_id, record_id, users = self.seed()
                request_id = self.request_payment(users, '500.00')
                editor = self.app.test_client()
                editor.post('/login', data={'login_id': '0123456789', 'password': 'secret'})
                barrier = Barrier(2)
                original_get = db.get_or_404

                def simultaneous_read(model, identifier, **kwargs):
                    row = original_get(model, identifier, **kwargs)
                    if model in (ExpenseRecord, SettlementRequest):
                        barrier.wait(timeout=10)
                    return row

                with patch.object(db, 'get_or_404', side_effect=simultaneous_read):
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        confirm = executor.submit(self.client.post, f'/settlement-requests/{request_id}/confirm')
                        edit = executor.submit(editor.post, f'/groups/{group_id}/expenses/{record_id}/edit', data={
                            'paid_by': users['0123456789'], 'expense_type': 'custom', 'custom_total': '200.00',
                            f"custom_{users['1234567890']}": '200.00'})
                        self.assertEqual(confirm.result(timeout=15).status_code, 302)
                        self.assertEqual(edit.result(timeout=15).status_code, 302)
                with self.app.app_context():
                    count = ConfirmedSettlement.query.count()
                    self.assertIn(count, (0, 1))
                    self.assertEqual(db.session.get(SettlementRequest, request_id).status, 'CONFIRMED' if count else 'PENDING')
                    self.assertEqual(ExpenseRecord.query.one().current_expense.amount, Decimal('200.00'))
                    self.assertEqual(ExpenseRevision.query.count(), 2)
                self.assert_balances(users, ('-300.00', '300.00', '0.00') if count else ('200.00', '-200.00', '0.00'))
            finally:
                self.dispose()


if __name__ == '__main__':
    unittest.main()
