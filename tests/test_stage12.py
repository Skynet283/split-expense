import unittest

from app import create_app
from config import Config
from extensions import db
from models import Group, GroupMembership, Member
from test_factory import create_user


class TestConfig(Config):
    TESTING = True
    CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    INITIAL_ADMIN_ID = None


class Stage12Tests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        with self.app.app_context():
            db.drop_all()
            db.create_all()
        self.client = self.app.test_client()
        for name, login_id in (("Asha Owner", "0123456789"), ("Sachin Pawar", "1234567890"),
                               ("Sai Kumar", "5839201746"), ("Ruthika Shah", "8134567890")):
            create_user(self.app, name, login_id)
        self.client.post("/login", data={"login_id": "0123456789", "password": "secret"})

    def test_picker_renders_only_eligible_public_user_data(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-name="Sachin Pawar" data-login-id="1234567890"', page)
        self.assertIn('data-name="Sai Kumar" data-login-id="5839201746"', page)
        self.assertNotIn('data-name="Asha Owner"', page)
        self.assertNotIn("password_hash", page)
        self.assertNotIn("is_admin", page)

    def test_picker_has_inspectable_name_and_user_id_filtering_and_multiselect(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("option.dataset.name.toLocaleLowerCase().includes(query)", page)
        self.assertIn("option.dataset.loginId.includes(query)", page)
        self.assertIn("selected.set(option.dataset.loginId", page)
        self.assertIn("selected.delete(user.loginId)", page)
        self.assertIn("hidden.name = 'user_ids'", page)
        self.assertIn("No matching registered users", page)

    def test_multiple_selected_users_are_added_atomically(self):
        response = self.client.post("/groups", data={
            "name": "Trip", "user_ids": ["1234567890", "5839201746", "8134567890"]
        }, follow_redirects=True)
        self.assertIn(b"Group &#34;Trip&#34; created!", response.data)
        with self.app.app_context():
            group = Group.query.filter_by(name="Trip").one()
            self.assertEqual(GroupMembership.query.filter_by(group_id=group.id).count(), 4)
            self.assertEqual(Member.query.filter_by(group_id=group.id).count(), 4)

    def test_duplicate_selections_do_not_create_duplicate_memberships(self):
        self.client.post("/groups", data={
            "name": "Trip", "user_ids": ["1234567890", "1234567890", "0123456789"]
        })
        with self.app.app_context():
            group = Group.query.filter_by(name="Trip").one()
            self.assertEqual(GroupMembership.query.filter_by(group_id=group.id).count(), 2)
            self.assertEqual(Member.query.filter_by(group_id=group.id).count(), 2)

    def test_invalid_or_unregistered_selection_rejects_the_entire_group(self):
        for invalid_ids in (["1234"], ["1234567890", "9999999999"]):
            with self.subTest(invalid_ids=invalid_ids):
                response = self.client.post("/groups", data={"name": "Rejected", "user_ids": invalid_ids},
                                            follow_redirects=True)
                self.assertIn(b"alert-danger", response.data)
                with self.app.app_context():
                    self.assertEqual(Group.query.count(), 0)
                    self.assertEqual(GroupMembership.query.count(), 0)

    def test_authentication_and_existing_archive_policy_are_unchanged(self):
        self.client.post("/groups", data={"name": "Trip", "user_ids": ["1234567890"]})
        with self.app.app_context():
            group = Group.query.one()
            group_id = group.id
            before = GroupMembership.query.filter_by(group_id=group_id).count()
        self.client.post("/logout")
        self.assertEqual(self.client.post("/groups", data={"name": "Unauthorized"}).status_code, 302)
        self.client.post("/login", data={"login_id": "1234567890", "password": "secret"})
        self.assertEqual(self.client.post(f"/groups/{group_id}/delete").status_code, 403)
        with self.app.app_context():
            self.assertFalse(db.session.get(Group, group_id).is_archived)
            self.assertEqual(GroupMembership.query.filter_by(group_id=group_id).count(), before)

    def test_group_multiselect_submission_remains_csrf_protected(self):
        class CsrfConfig(TestConfig):
            CSRF_ENABLED = True
            SECRET_KEY = "stage-12-test-secret"

        app = create_app(CsrfConfig)
        with app.app_context():
            db.drop_all()
            db.create_all()
            from models import User
            from werkzeug.security import generate_password_hash
            owner = User(name="Owner", login_id="1111111111", password_hash=generate_password_hash("secret"))
            selected = User(name="Selected", login_id="2222222222", password_hash=generate_password_hash("secret"))
            db.session.add_all([owner, selected])
            db.session.commit()
            owner_id = owner.id
        client = app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(owner_id)
            session["_fresh"] = True
        response = client.post("/groups", data={"name": "Blocked", "user_ids": ["2222222222"]})
        self.assertEqual(response.status_code, 400)
        with app.app_context():
            self.assertEqual(Group.query.count(), 0)
            self.assertEqual(GroupMembership.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
