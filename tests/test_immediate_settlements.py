import sqlite3
import unittest
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app import create_app, initialize_database
from extensions import db
from helpers import compute_global_user_balances, compute_pairwise_user_obligations, get_user_settlement_summary
from models import (ConfirmedSettlement, Expense, ExpenseRecord, ExpenseRevision, ExpenseSplit, Group, GroupMembership,
                    SettlementRequest, SettlementReversal)
from test_factory import settlement_form_data
import routes
import test_global_settle_up


class ImmediateSettlementTests(unittest.TestCase):
    setUp = test_global_settle_up.GlobalSettleUpTests.setUp
    token = test_global_settle_up.GlobalSettleUpTests.token
    post = test_global_settle_up.GlobalSettleUpTests.post
    add_users = test_global_settle_up.GlobalSettleUpTests.add_users
    add_obligation = test_global_settle_up.GlobalSettleUpTests.add_obligation
    login = test_global_settle_up.GlobalSettleUpTests.login
    logout = test_global_settle_up.GlobalSettleUpTests.logout
    section = test_global_settle_up.GlobalSettleUpTests.section
    pairwise = test_global_settle_up.GlobalSettleUpTests.pairwise
    global_balances = test_global_settle_up.GlobalSettleUpTests.global_balances

    def seed(self, debt="30.34"):
        users = self.add_users()
        self.add_obligation(users, "Asha", "Bala", debt, "Trip")
        return users

    def record(self, users, amount, *, creditor=False):
        self.login("1234567890" if creditor else "0123456789")
        form = settlement_form_data(self.client, {"payer_user_id": users["Asha"],
            "recipient_user_id": users["Bala"], "amount": amount})
        return self.post("/settlements", form, token_path="/")

    def payment_id(self):
        with self.app.app_context():
            return ConfirmedSettlement.query.one().id

    def snapshot(self, models):
        with self.app.app_context():
            return {model.__tablename__: [tuple(row) for row in db.session.execute(
                select(model.__table__).order_by(model.id))] for model in models}

    def test_debtor_full_payment_is_immediate_without_confirmation(self):
        users = self.seed()
        self.assertEqual(self.record(users, "30.34").status_code, 302)
        self.assertEqual(self.pairwise(), {})
        self.assertEqual(self.global_balances()[users["Asha"]], Decimal("0.00"))
        with self.app.app_context():
            self.assertEqual(SettlementRequest.query.one().status, "CONFIRMED")
            self.assertEqual(ConfirmedSettlement.query.one().amount, Decimal("30.34"))

    def test_debtor_partial_payment_immediately_leaves_exact_cents(self):
        users = self.seed()
        self.record(users, "15.00")
        self.assertEqual(self.pairwise(), {(users["Asha"], users["Bala"]): Decimal("15.34")})
        self.assertEqual(self.global_balances()[users["Bala"]], Decimal("15.34"))

    def test_creditor_full_received_payment_keeps_debtor_direction(self):
        users = self.seed("144.00")
        self.record(users, "144.00", creditor=True)
        self.assertEqual(self.pairwise(), {})
        with self.app.app_context():
            payment = ConfirmedSettlement.query.one()
            self.assertEqual((payment.payer_user_id, payment.recipient_user_id, payment.recorded_by_user_id),
                             (users["Asha"], users["Bala"], users["Bala"]))

    def test_creditor_partial_received_payment_leaves_remaining_debt(self):
        users = self.seed("144.00")
        self.record(users, "50.00", creditor=True)
        self.assertEqual(self.pairwise(), {(users["Asha"], users["Bala"]): Decimal("94.00")})
        self.assertEqual(self.global_balances()[users["Asha"]], Decimal("-94.00"))

    def test_both_dashboard_forms_submit_correct_direction_and_include_csrf(self):
        users = self.seed()
        for login_id, label in (("0123456789", "Amount paid"), ("1234567890", "Amount received")):
            self.login(login_id)
            section = self.section()
            self.assertIn(label, section)
            self.assertIn('action="/settlements"', section)
            self.assertIn(f'name="payer_user_id" value="{users["Asha"]}"', section)
            self.assertIn(f'name="recipient_user_id" value="{users["Bala"]}"', section)
            self.assertIn('name="csrf_token"', section)
            self.assertIn('name="submission_token"', section)
            self.assertRegex(section, r'max="30\.34" value="30\.34"')
            self.logout()

    def test_payment_and_reversal_history_are_private_and_audited(self):
        users = self.seed()
        # Group visibility alone must never grant visibility of a global payment.
        with self.app.app_context():
            db.session.add(GroupMembership(group_id=Group.query.one().id, user_id=users["Chitra"]))
            db.session.commit()
        self.record(users, "15.00", creditor=True)
        identifier = self.payment_id()
        for path in ("/", "/activity"):
            page = self.client.get(path).get_data(as_text=True)
            self.assertIn("Asha</strong> paid", page)
            self.assertIn("Bala", page)
            self.assertIn("₹15.00", page)
            self.assertIn("Recorded by Bala", page)
            self.assertIn("UTC", page)
            self.assertIn("Reverse settlement", page)
        self.post(f"/settlements/{identifier}/reverse", token_path="/activity")
        page = self.client.get("/activity").get_data(as_text=True)
        self.assertIn(f"reversed settlement #{identifier}", page)
        self.assertIn("Reversed by Bala", page)
        self.assertNotIn("Reverse settlement</button>", page)
        self.logout()
        self.login("0123456789")
        self.assertIn("Reversed by Bala", self.client.get("/activity").get_data(as_text=True))
        self.logout()
        self.login("2345678901")
        self.assertNotIn("₹15.00", self.client.get("/activity").get_data(as_text=True))
        self.assertNotIn("₹15.00", self.client.get("/").get_data(as_text=True))

    def test_rendered_incoming_form_records_a_partial_payment(self):
        users = self.seed("144.00")
        self.login("1234567890")
        section = self.section()
        form_html = re.search(r'<form method="POST" action="/settlements".*?</form>', section, re.S).group(0)
        form = dict(re.findall(r'name="([^"]+)" value="([^"]+)"', form_html))
        form["amount"] = "50.00"
        self.assertEqual(self.client.post("/settlements", data=form).status_code, 302)
        self.assertEqual(self.pairwise(), {(users["Asha"], users["Bala"]): Decimal("94.00")})

    def test_reversal_restores_obligation_preserves_payment_and_expenses_and_is_single(self):
        users = self.seed()
        before = self.snapshot((Expense, ExpenseSplit, ExpenseRecord, ExpenseRevision))
        self.record(users, "15.00")
        identifier = self.payment_id()
        payment_before = self.snapshot((ConfirmedSettlement, SettlementRequest))
        self.post(f"/settlements/{identifier}/reverse", token_path="/activity")
        self.assertEqual(self.pairwise(), {(users["Asha"], users["Bala"]): Decimal("30.34")})
        self.assertEqual(self.global_balances()[users["Asha"]], Decimal("-30.34"))
        self.assertEqual(self.snapshot((Expense, ExpenseSplit, ExpenseRecord, ExpenseRevision)), before)
        self.assertEqual(self.snapshot((ConfirmedSettlement, SettlementRequest)), payment_before)
        self.post(f"/settlements/{identifier}/reverse", token_path="/activity")
        with self.app.app_context():
            reversal = SettlementReversal.query.one()
            self.assertEqual(reversal.reversed_by_user_id, users["Asha"])
            self.assertEqual(reversal.confirmed_settlement_id, identifier)
            self.assertIsNotNone(reversal.reversed_at)

    def test_unauthorized_create_reverse_and_missing_csrf_are_rejected(self):
        users = self.seed()
        self.record(users, "15.00")
        identifier = self.payment_id()
        self.assertEqual(self.client.post(f"/settlements/{identifier}/reverse").status_code, 400)
        self.assertEqual(self.client.post(f"/settlements/{identifier}/reverse", data={"csrf_token": "wrong"}).status_code, 400)
        self.logout()
        self.login("2345678901")
        self.assertEqual(self.post(f"/settlements/{identifier}/reverse", token_path="/").status_code, 403)
        form = settlement_form_data(self.client, {"payer_user_id": users["Asha"],
            "recipient_user_id": users["Bala"], "amount": "1.00"})
        self.assertEqual(self.post("/settlements", form, token_path="/").status_code, 403)
        with self.app.app_context():
            self.assertEqual(SettlementReversal.query.count(), 0)
            self.assertEqual(ConfirmedSettlement.query.count(), 1)
        self.logout()
        self.login("0123456789")
        self.assertEqual(self.client.post("/settlements", data=form).status_code, 400)
        self.assertEqual(self.client.get(f"/settlements/{identifier}/reverse").status_code, 405)
        self.assertEqual(self.post("/settlements/999999/reverse", token_path="/").status_code, 404)
        self.logout()
        self.assertEqual(self.post(f"/settlements/{identifier}/reverse").status_code, 302)

    def test_invalid_amounts_overpayment_and_wrong_direction_do_not_record(self):
        users = self.seed()
        self.login("0123456789")
        for amount in ("0", "-1", "30.35", "1.001", "NaN", "Infinity", "bad"):
            with self.subTest(amount=amount):
                form = settlement_form_data(self.client, {"payer_user_id": users["Asha"],
                    "recipient_user_id": users["Bala"], "amount": amount})
                self.assertEqual(self.post("/settlements", form, token_path="/").status_code, 302)
        form = settlement_form_data(self.client, {"payer_user_id": users["Bala"],
            "recipient_user_id": users["Asha"], "amount": "1.00"})
        self.post("/settlements", form, token_path="/")
        with self.app.app_context():
            self.assertEqual(ConfirmedSettlement.query.count(), 0)
            self.assertEqual(SettlementRequest.query.count(), 0)
        self.assertEqual(self.pairwise(), {(users["Asha"], users["Bala"]): Decimal("30.34")})

    def test_same_signed_form_is_idempotent_including_after_reversal(self):
        users = self.seed()
        self.login("0123456789")
        form = settlement_form_data(self.client, {"payer_user_id": users["Asha"],
            "recipient_user_id": users["Bala"], "amount": "5.00"})
        for _ in range(2):
            self.post("/settlements", form, token_path="/")
        self.assertEqual(self.pairwise(), {(users["Asha"], users["Bala"]): Decimal("25.34")})
        identifier = self.payment_id()
        self.post(f"/settlements/{identifier}/reverse", token_path="/")
        self.post("/settlements", form, token_path="/")
        self.post("/settlements", dict(form, amount="6.00"), token_path="/")
        with self.app.app_context():
            self.assertEqual(ConfirmedSettlement.query.count(), 1)
            self.assertEqual(SettlementReversal.query.count(), 1)
        self.assertEqual(self.pairwise(), {(users["Asha"], users["Bala"]): Decimal("30.34")})

    def test_submission_token_is_required_signed_and_bound_to_actor_and_direction(self):
        users = self.seed()
        self.login("0123456789")
        base = {"payer_user_id": users["Asha"], "recipient_user_id": users["Bala"], "amount": "5.00"}
        form = settlement_form_data(self.client, base)
        for payload in (base, dict(base, submission_token="forged"),
                        dict(form, payer_user_id=users["Bala"], recipient_user_id=users["Asha"])):
            self.post("/settlements", payload, token_path="/")
        self.logout()
        self.login("1234567890")
        self.post("/settlements", form, token_path="/")
        with self.app.app_context():
            self.assertEqual(ConfirmedSettlement.query.count(), 0)

    def test_obsolete_pending_requests_reserve_nothing_and_do_not_block_new_payment(self):
        users = self.seed()
        with self.app.app_context():
            old = SettlementRequest(requester_user_id=users["Asha"], recipient_user_id=users["Bala"],
                                    amount=Decimal("30.34"), status="PENDING")
            db.session.add(old)
            db.session.commit()
            identifier = old.id
            outgoing, incoming = get_user_settlement_summary(users["Asha"])
            self.assertEqual(outgoing[0]["available_amount"], Decimal("30.34"))
            self.assertEqual(incoming, [])
        self.record(users, "30.34", creditor=True)
        self.assertEqual(self.pairwise(), {})
        for action in ("confirm", "reject"):
            self.post(f"/settlement-requests/{identifier}/{action}", token_path="/")
        with self.app.app_context():
            old = db.session.get(SettlementRequest, identifier)
            self.assertEqual(old.status, "PENDING")
            self.assertIsNone(old.resolved_at)
            self.assertEqual(ConfirmedSettlement.query.count(), 1)
        self.assertIn("Obsolete request", self.client.get("/activity").get_data(as_text=True))

    def test_archived_multiple_groups_reciprocal_obligations_and_reversal(self):
        users = self.add_users()
        first = self.add_obligation(users, "Asha", "Bala", "40.00", "Trip")
        self.add_obligation(users, "Asha", "Bala", "60.00", "Dinner")
        self.add_obligation(users, "Bala", "Asha", "25.00", "Meal refund")
        with self.app.app_context():
            db.session.get(Group, first).archived_at = datetime.now(timezone.utc)
            db.session.commit()
        self.record(users, "30.00", creditor=True)
        self.assertEqual(self.pairwise(), {(users["Asha"], users["Bala"]): Decimal("45.00")})
        self.post(f"/settlements/{self.payment_id()}/reverse", token_path="/")
        self.assertEqual(self.pairwise(), {(users["Asha"], users["Bala"]): Decimal("75.00")})

    def test_failed_reversal_rolls_back_and_database_prevents_duplicate_reversal(self):
        users = self.seed()
        self.record(users, "15.00")
        identifier = self.payment_id()

        def fail_commit():
            db.session.flush()
            raise SQLAlchemyError("simulated reversal failure")

        with patch.object(db.session, "commit", side_effect=fail_commit), self.assertLogs("routes", level="ERROR"):
            self.post(f"/settlements/{identifier}/reverse", token_path="/")
        self.assertEqual(self.pairwise(), {(users["Asha"], users["Bala"]): Decimal("15.34")})
        with self.app.app_context():
            self.assertEqual(SettlementReversal.query.count(), 0)
        self.post(f"/settlements/{identifier}/reverse", token_path="/")
        with self.app.app_context():
            db.session.add(SettlementReversal(confirmed_settlement_id=identifier,
                reversed_by_user_id=users["Bala"], reversed_at=datetime.now(timezone.utc)))
            with self.assertRaises(IntegrityError):
                db.session.commit()
            db.session.rollback()
            self.assertEqual(SettlementReversal.query.count(), 1)

    def test_database_submission_unique_index_prevents_duplicate_snapshots(self):
        users = self.seed()
        self.record(users, "5.00")
        with self.app.app_context():
            existing = ConfirmedSettlement.query.one()
            request = SettlementRequest(requester_user_id=users["Asha"], recipient_user_id=users["Bala"],
                                        amount=Decimal("5.00"), status="CONFIRMED")
            db.session.add(request)
            db.session.flush()
            db.session.add(ConfirmedSettlement(settlement_request_id=request.id, payer_user_id=users["Asha"],
                recipient_user_id=users["Bala"], amount=Decimal("5.00"), confirmed_at=datetime.now(timezone.utc),
                recorded_by_user_id=users["Asha"], submission_id=existing.submission_id))
            with self.assertRaises(IntegrityError):
                db.session.commit()
            db.session.rollback()
            self.assertEqual(ConfirmedSettlement.query.count(), 1)
            self.assertEqual(SettlementRequest.query.count(), 1)

    def test_real_legacy_sqlite_upgrade_preserves_existing_records_and_accounting(self):
        # Build the old settlement schema in a disposable file before startup.
        with TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.db"
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript('''
                    CREATE TABLE users (id INTEGER PRIMARY KEY, login_id VARCHAR(10), password_hash TEXT);
                    INSERT INTO users VALUES (1, '0123456789', 'hash'), (2, '1234567890', 'hash');
                    CREATE TABLE settlement_request (id INTEGER PRIMARY KEY, requester_user_id INTEGER NOT NULL,
                        recipient_user_id INTEGER NOT NULL, amount NUMERIC NOT NULL, status VARCHAR(20) NOT NULL,
                        created_at DATETIME NOT NULL, resolved_at DATETIME);
                    INSERT INTO settlement_request VALUES
                        (10, 1, 2, 15, 'CONFIRMED', '2020-01-01 00:00:00', '2020-01-02 00:00:00'),
                        (11, 1, 2, 100, 'PENDING', '2020-01-01 00:00:00', NULL),
                        (12, 1, 2, 100, 'REJECTED', '2020-01-01 00:00:00', '2020-01-02 00:00:00'),
                        (13, 1, 2, 100, 'CONFIRMED', '2020-01-01 00:00:00', '2020-01-02 00:00:00');
                    CREATE TABLE confirmed_settlement (id INTEGER PRIMARY KEY, settlement_request_id INTEGER NOT NULL UNIQUE,
                        payer_user_id INTEGER NOT NULL, recipient_user_id INTEGER NOT NULL, amount NUMERIC NOT NULL,
                        confirmed_at DATETIME NOT NULL);
                    INSERT INTO confirmed_settlement VALUES (20, 10, 1, 2, 15, '2020-01-02 00:00:00');
                ''')
                original_requests = connection.execute("SELECT * FROM settlement_request").fetchall()
                original_payment = connection.execute("SELECT * FROM confirmed_settlement").fetchall()

            class LegacyConfig(test_global_settle_up.GlobalSettleUpConfig):
                SQLALCHEMY_DATABASE_URI = "sqlite:///" + database.as_posix()
                INITIAL_ADMIN_ID = None

            app = create_app(LegacyConfig)
            try:
                initialize_database(app)
                initialize_database(app)
                with app.app_context():
                    self.assertEqual([tuple(r) for r in db.session.execute(text("SELECT * FROM settlement_request"))], original_requests)
                    self.assertEqual([tuple(r) for r in db.session.execute(text(
                        "SELECT id, settlement_request_id, payer_user_id, recipient_user_id, amount, confirmed_at FROM confirmed_settlement"))], original_payment)
                    payment = ConfirmedSettlement.query.one()
                    self.assertIsNone(payment.recorded_by_user_id)
                    self.assertIsNone(payment.submission_id)
                    self.assertEqual(compute_pairwise_user_obligations()[0], {(2, 1): Decimal("15.00")})
                    self.assertEqual(compute_global_user_balances()[0][1], Decimal("15.00"))
                    self.assertEqual(SettlementReversal.query.count(), 0)
                client = app.test_client()
                with client.session_transaction() as session:
                    session["_user_id"] = "2"
                    session["_fresh"] = True
                client.get("/activity")
                with client.session_transaction() as session:
                    csrf = session["_csrf_token"]
                self.assertEqual(client.post("/settlements/20/reverse", data={"csrf_token": csrf}).status_code, 302)
                with app.app_context():
                    self.assertEqual(compute_pairwise_user_obligations()[0], {})
                    self.assertEqual(ConfirmedSettlement.query.count(), 1)
                    self.assertEqual(SettlementRequest.query.count(), 4)
                    self.assertEqual(SettlementReversal.query.count(), 1)
            finally:
                with app.app_context():
                    db.session.remove()
                    db.engine.dispose()

    def concurrent_case(self, *, same_form=False, reverse=False):
        with TemporaryDirectory() as directory:
            class FileConfig(test_global_settle_up.GlobalSettleUpConfig):
                SQLALCHEMY_DATABASE_URI = "sqlite:///" + (Path(directory) / "race.db").as_posix()

            self.app = create_app(FileConfig)
            self.client = self.app.test_client()
            try:
                users = self.seed("100.00")
                if reverse:
                    self.record(users, "30.00")
                    endpoint = f"/settlements/{self.payment_id()}/reverse"
                else:
                    endpoint = "/settlements"
                clients = [self.app.test_client(), self.app.test_client()]
                forms = []
                for index, client in enumerate(clients):
                    with client.session_transaction() as session:
                        session["_user_id"] = str(users["Asha"] if same_form or index == 0 else users["Bala"])
                        session["_fresh"] = True
                    client.get("/")
                    form = {} if reverse else settlement_form_data(client, {"payer_user_id": users["Asha"],
                        "recipient_user_id": users["Bala"], "amount": "30.00" if same_form else "60.00"})
                    forms.append(form)
                if same_form:
                    forms[1] = dict(forms[0])
                for client, form in zip(clients, forms):
                    with client.session_transaction() as session:
                        form["csrf_token"] = session["_csrf_token"]
                barrier = Barrier(2)
                if reverse:
                    original_get = db.get_or_404

                    def simultaneous_get(model, identifier, **kwargs):
                        row = original_get(model, identifier, **kwargs)
                        if model is ConfirmedSettlement:
                            barrier.wait(timeout=10)
                        return row

                    claim_patch = patch.object(db, "get_or_404", side_effect=simultaneous_get)
                else:
                    original_parse = routes.parse_settlement_amount

                    def simultaneous_parse(value):
                        amount = original_parse(value)
                        barrier.wait(timeout=10)
                        return amount

                    claim_patch = patch.object(routes, "parse_settlement_amount", side_effect=simultaneous_parse)
                with claim_patch, ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(client.post, endpoint, data=form) for client, form in zip(clients, forms)]
                    self.assertEqual([f.result(timeout=15).status_code for f in futures], [302, 302])
                with self.app.app_context():
                    self.assertEqual(ConfirmedSettlement.query.count(), 1)
                    self.assertEqual(SettlementRequest.query.count(), 1)
                    self.assertEqual(SettlementReversal.query.count(), 1 if reverse else 0)
                    expected = Decimal("100.00" if reverse else "70.00" if same_form else "40.00")
                    self.assertEqual(compute_pairwise_user_obligations()[0], {(users["Asha"], users["Bala"]): expected})
            finally:
                with self.app.app_context():
                    db.session.remove()
                    db.engine.dispose()

    def test_concurrent_same_partial_form_records_once(self):
        self.concurrent_case(same_form=True)

    def test_concurrent_opposite_participants_cannot_overpay(self):
        self.concurrent_case()

    def test_concurrent_reversal_records_once(self):
        self.concurrent_case(reverse=True)


if __name__ == "__main__":
    unittest.main()
