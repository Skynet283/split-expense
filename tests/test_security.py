from test_factory import settlement_form_data
import re
import unittest

from app import create_app
from config import Config
from extensions import db
from models import ExpenseRecord, Group, SettlementRequest, User
from test_factory import create_user
import test_stage6


class SecurityConfig(test_stage6.TestConfig):
    CSRF_ENABLED = True
    SECRET_KEY = "security-test-secret-that-is-not-used-outside-tests"


class CsrfReleaseHardeningTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(SecurityConfig)
        with self.app.app_context():
            db.drop_all()
            db.create_all()
        self.client = self.app.test_client()

    def token(self, path="/login"):
        self.client.get(path)
        with self.client.session_transaction() as session:
            return session["_csrf_token"]

    def post(self, path, data=None, *, token_path="/login"):
        payload = dict(data or {})
        payload["csrf_token"] = self.token(token_path)
        return self.client.post(path, data=payload)

    def register(self, name, login_id):
        return create_user(self.app, name, login_id)

    def login(self, login_id):
        return self.post("/login", {"login_id": login_id, "password": "secret"})

    def seed(self):
        self.register("Asha", "0123456789")
        self.register("Bala", "1234567890")
        self.login("0123456789")
        self.post("/groups", {"name": "Trip", "user_ids": ["1234567890"]}, token_path="/")
        with self.app.app_context():
            group = Group.query.one()
            users = {user.login_id: user.id for user in User.query.all()}
        self.post(f"/groups/{group.id}/expenses/new", {
            "description": "Dinner", "paid_by": users["0123456789"], "expense_type": "custom",
            "custom_total": "50.00", f"custom_{users['1234567890']}": "50.00"},
            token_path=f"/groups/{group.id}/expenses/new")
        with self.app.app_context():
            return group.id, ExpenseRecord.query.one().id, users

    def test_every_unsafe_route_rejects_missing_or_wrong_tokens_before_mutation(self):
        group_id, record_id, users = self.seed()
        endpoints = [
            ("/login", {"login_id": "0123456789", "password": "secret"}),
            ("/register", {"name": "Dara", "login_id": "3456789012", "password": "secret", "confirm_password": "secret"}),
            ("/groups", {"name": "Other"}),
            ("/settlement-requests", {"recipient_user_id": users["1234567890"], "amount": "1.00"}),
            ("/settlement-requests/999/confirm", {}),
            ("/settlement-requests/999/reject", {}),
            ("/logout", {}),
            (f"/groups/{group_id}/expenses/new", {}),
            (f"/groups/{group_id}/expenses/{record_id}/edit", {}),
            (f"/groups/{group_id}/expenses/{record_id}/delete", {}),
            (f"/groups/{group_id}/delete", {}),
        ]
        with self.app.app_context():
            before = (User.query.count(), Group.query.count(), ExpenseRecord.query.count(), SettlementRequest.query.count())
        for endpoint, data in endpoints:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.client.post(endpoint, data=data).status_code, 400)
                invalid = dict(data, csrf_token="wrong-token")
                self.assertEqual(self.client.post(endpoint, data=invalid).status_code, 400)
        with self.app.app_context():
            self.assertEqual((User.query.count(), Group.query.count(), ExpenseRecord.query.count(), SettlementRequest.query.count()), before)

    def test_rendered_forms_include_tokens_and_valid_tokens_preserve_workflows(self):
        group_id, record_id, users = self.seed()
        self.client.post("/logout", data={"csrf_token": self.token("/")})
        pages = [self.client.get("/login")]
        self.login("1234567890")
        request = self.post("/settlement-requests", settlement_form_data(self.client, {"recipient_user_id": users["0123456789"], "amount": "10.00"}), token_path="/")
        self.assertEqual(request.status_code, 302)
        self.client.post("/logout", data={"csrf_token": self.token("/")})
        self.login("0123456789")
        pages.extend([self.client.get("/"), self.client.get(f"/groups/{group_id}"),
                      self.client.get(f"/groups/{group_id}/expenses/new"),
                      self.client.get(f"/groups/{group_id}/expenses/{record_id}/edit")])
        for page in pages:
            forms = re.findall(r"<form\b[^>]*method=\"POST\"[^>]*>(.*?)</form>", page.get_data(as_text=True), re.S | re.I)
            self.assertTrue(forms)
            self.assertTrue(all(re.search(r'name=\"csrf_token\"[^>]*value=\"[^\"]+\"', form) for form in forms))
        with self.app.app_context():
            request_id = SettlementRequest.query.one().id
        confirmed = self.post(f"/settlement-requests/{request_id}/confirm", token_path="/")
        self.assertEqual(confirmed.status_code, 302)
        with self.app.app_context():
            self.assertEqual(SettlementRequest.query.one().status, "CONFIRMED")

    def test_csrf_tokens_are_session_bound_and_rotate_after_login_logout(self):
        first = self.token("/login")
        other = self.app.test_client()
        other.get("/login")
        with other.session_transaction() as session:
            second = session["_csrf_token"]
        self.assertNotEqual(first, second)
        self.register("Asha", "0123456789")
        self.login("0123456789")
        with self.client.session_transaction() as session:
            self.assertNotIn("_csrf_token", session)
        replacement = self.token("/")
        self.assertNotEqual(first, replacement)
        self.client.post("/logout", data={"csrf_token": replacement})
        with self.client.session_transaction() as session:
            self.assertNotIn("_csrf_token", session)


class ProductionConfigurationTests(unittest.TestCase):
    def test_production_requires_secret_and_sets_secure_cookie_defaults(self):
        class MissingSecret(Config):
            PRODUCTION = True
            SECRET_KEY = None
            SQLALCHEMY_DATABASE_URI = "sqlite://"
            INITIAL_ADMIN_ID = None

        with self.assertRaisesRegex(RuntimeError, "SECRET_KEY"):
            create_app(MissingSecret)

        class ProductionConfig(MissingSecret):
            SECRET_KEY = "a-long-random-production-secret-provided-by-the-environment"

        app = create_app(ProductionConfig)
        try:
            self.assertTrue(app.config["SESSION_COOKIE_SECURE"])
            self.assertTrue(app.config["SESSION_COOKIE_HTTPONLY"])
            self.assertEqual(app.config["SESSION_COOKIE_SAMESITE"], "Lax")
            self.assertEqual(app.config["PREFERRED_URL_SCHEME"], "https")
        finally:
            with app.app_context():
                db.session.remove()
                db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
