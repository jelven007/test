from __future__ import annotations

import hashlib
import hmac
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

from banxia_strategy.adapters.identity_settings import VerificationRejected
from banxia_strategy.auth import (
    AuthenticationManager,
    AuthenticationRejected,
    RateLimitExceeded,
    SmtpSettings,
    SmtpVerificationMailer,
    normalize_email,
    validate_password,
)


class FakeMailer:
    def __init__(self):
        self.messages = []

    def send_verification_code(self, email, code):
        self.messages.append((email, code))


class FakeIdentityRepository:
    def __init__(self):
        self.challenges = {}
        self.users = {}
        self.sessions = {}
        self.failures = 0

    def email_is_registered(self, email):
        return email in self.users

    def create_email_challenge(
        self,
        *,
        email,
        code_digest,
        requested_ip,
        expires_at,
    ):
        challenge_id = str(uuid.uuid4())
        self.challenges[challenge_id] = {
            "email": email,
            "code_digest": code_digest,
            "expires_at": expires_at,
            "consumed": False,
        }
        return challenge_id

    def invalidate_email_challenge(self, challenge_id):
        self.challenges[challenge_id]["consumed"] = True

    def register_user(
        self,
        *,
        challenge_id,
        email,
        code_digest,
        password_hash,
    ):
        challenge = self.challenges[challenge_id]
        if (
            challenge["consumed"]
            or challenge["expires_at"] <= datetime.now(timezone.utc)
            or not hmac.compare_digest(
                challenge["code_digest"],
                code_digest,
            )
        ):
            raise VerificationRejected("验证码无效")
        user_id = str(uuid.uuid4())
        self.users[email] = {
            "user_id": user_id,
            "email": email,
            "password_hash": password_hash,
            "active": True,
            "locked_until": None,
        }
        challenge["consumed"] = True
        return {"user_id": user_id, "email": email}

    def get_user_by_email(self, email):
        return self.users.get(email)

    def record_login_failure(self, _user_id):
        self.failures += 1

    def record_login_success(self, _user_id):
        self.failures = 0

    def create_user_session(
        self,
        *,
        token_digest,
        csrf_digest,
        user_id,
        user_agent,
        remote_ip,
        expires_at,
    ):
        self.sessions[token_digest] = {
            "user_id": user_id,
            "email": next(
                item["email"]
                for item in self.users.values()
                if item["user_id"] == user_id
            ),
            "csrf_digest": csrf_digest,
            "expires_at": expires_at,
        }

    def get_user_session(self, token_digest):
        return self.sessions.get(token_digest)

    def revoke_user_session(self, token_digest):
        self.sessions.pop(token_digest, None)


class AuthenticationManagerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repository = FakeIdentityRepository()
        cls.mailer = FakeMailer()
        cls.manager = AuthenticationManager(
            cls.repository,
            cls.mailer,
            secret="s" * 32,
        )

    def test_registration_code_register_login_and_logout(self):
        email = "Trader@126.com"
        challenge = self.manager.request_registration_code(
            email=email,
            remote_ip="127.0.0.1",
        )
        sent_email, code = self.mailer.messages[-1]
        self.assertEqual(sent_email, "trader@126.com")

        registered = self.manager.register(
            challenge_id=challenge["challenge_id"],
            email=email,
            code=code,
            password="Strong#Pass1",
            remote_ip="127.0.0.1",
            user_agent="test",
        )
        session = self.manager.resolve_session(
            registered["session_token"]
        )
        self.assertEqual(session["email"], "trader@126.com")
        self.assertTrue(
            self.manager.verify_csrf(session, registered["csrf_token"])
        )
        self.assertFalse(self.manager.verify_csrf(session, "wrong"))

        with self.assertRaises(AuthenticationRejected):
            self.manager.login(
                email=email,
                password="Wrong#Pass1",
                remote_ip="127.0.0.2",
                user_agent="test",
            )
        logged_in = self.manager.login(
            email=email,
            password="Strong#Pass1",
            remote_ip="127.0.0.2",
            user_agent="test",
        )
        self.assertEqual(logged_in["email"], "trader@126.com")
        self.manager.logout(logged_in["session_token"])
        digest = hashlib.sha256(
            logged_in["session_token"].encode("utf-8")
        ).hexdigest()
        self.assertNotIn(digest, self.repository.sessions)

    def test_validation_and_rate_limit(self):
        self.assertEqual(
            normalize_email(" User@Example.com "),
            "user@example.com",
        )
        with self.assertRaises(ValueError):
            normalize_email("invalid")
        with self.assertRaises(ValueError):
            validate_password("password")

        for _index in range(3):
            self.manager.request_registration_code(
                email="limited@example.com",
                remote_ip="192.0.2.1",
            )
        with self.assertRaises(RateLimitExceeded):
            self.manager.request_registration_code(
                email="limited@example.com",
                remote_ip="192.0.2.1",
            )

    @patch("banxia_strategy.auth.smtplib.SMTP_SSL")
    def test_126_smtp_sends_to_user_supplied_address(self, smtp_ssl):
        client = smtp_ssl.return_value.__enter__.return_value
        mailer = SmtpVerificationMailer(
            SmtpSettings(
                username="sender@126.com",
                password="authorization-code",
            )
        )

        mailer.send_verification_code("recipient@example.com", "123456")

        self.assertEqual(smtp_ssl.call_args.args[:2], ("smtp.126.com", 465))
        client.login.assert_called_once_with(
            "sender@126.com",
            "authorization-code",
        )
        message = client.send_message.call_args.args[0]
        self.assertEqual(message["To"], "recipient@example.com")
        self.assertIn("123456", message.get_content())


if __name__ == "__main__":
    unittest.main()
