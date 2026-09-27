from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from banxia_strategy.api import ApiServices, create_api_app
from banxia_strategy.web_server import ReportStore


USER_ID = "00000000-0000-0000-0000-000000000001"
BOARD_ID = "00000000-0000-0000-0000-000000000002"


class FakeCache:
    def ready(self):
        return True


class FakeRepository:
    def __init__(self):
        self.items = []
        self.saved_by = None
        self.stock_board = None

    def ready(self):
        return True

    def list_new_boards(self):
        return list(self.items)

    def replace_new_boards(self, items, *, user_id):
        self.saved_by = user_id
        self.items = [
            {
                **item,
                "id": item["id"] or BOARD_ID,
            }
            for item in items
        ]
        return list(self.items)

    def set_security_new_board(self, symbol, board_id):
        if symbol != "600001":
            raise LookupError("股票不存在")
        self.stock_board = board_id
        board = next(
            (item for item in self.items if item["id"] == board_id),
            None,
        )
        return {"symbol": symbol, "new_board": board}


class FakeAuthManager:
    session_hours = 168

    def __init__(self):
        self.revoked = False

    def resolve_session(self, token):
        if token != "session-token" or self.revoked:
            return None
        return {
            "user_id": USER_ID,
            "email": "user@126.com",
            "csrf_digest": "csrf",
        }

    def verify_csrf(self, _session, token):
        return token == "csrf-token"

    def request_registration_code(self, *, email, remote_ip):
        return {
            "challenge_id": "00000000-0000-0000-0000-000000000003",
            "email": email.lower(),
            "expires_in": 600,
        }

    def register(self, **_kwargs):
        return self.login()

    def login(self, **_kwargs):
        self.revoked = False
        return {
            "user_id": USER_ID,
            "email": "user@126.com",
            "session_token": "session-token",
            "csrf_token": "csrf-token",
        }

    def logout(self, _token):
        self.revoked = True


class AuthenticationApiTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.repository = FakeRepository()
        self.auth = FakeAuthManager()
        app = create_api_app(
            ApiServices(
                reports=ReportStore([Path(self.directory.name)]),
                repository=self.repository,
                cache=FakeCache(),
                auth_enabled=True,
                auth_manager=self.auth,
            )
        )
        self.client = TestClient(app)

    def tearDown(self):
        self.directory.cleanup()

    def login(self):
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": "user@126.com", "password": "Strong#Pass1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("HttpOnly", response.headers.get_list("set-cookie")[0])
        self.assertEqual(
            self.client.cookies.get("banxia_csrf"),
            "csrf-token",
        )

    def test_protected_pages_session_and_logout(self):
        redirect = self.client.get("/", follow_redirects=False)
        self.assertEqual(redirect.status_code, 303)
        self.assertEqual(redirect.headers["location"], "/login?next=/")
        self.assertEqual(self.client.get("/login").status_code, 200)
        self.assertEqual(
            self.client.get("/api/v1/health/live").status_code,
            200,
        )
        self.assertEqual(
            self.client.get("/api/v1/settings/new-boards").status_code,
            401,
        )

        self.login()
        me = self.client.get("/api/v1/auth/me")
        self.assertEqual(me.json()["user"]["email"], "user@126.com")
        self.assertEqual(self.client.get("/settings").status_code, 200)
        self.assertEqual(
            self.client.get("/login", follow_redirects=False).status_code,
            303,
        )

        denied = self.client.put(
            "/api/v1/settings/new-boards",
            json={"items": []},
        )
        self.assertEqual(denied.status_code, 403)
        logged_out = self.client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": "csrf-token"},
        )
        self.assertEqual(logged_out.status_code, 200)
        self.assertEqual(
            self.client.get("/api/v1/auth/me").status_code,
            401,
        )

    def test_new_board_settings_and_stock_assignment(self):
        self.login()
        headers = {"X-CSRF-Token": "csrf-token"}
        saved = self.client.put(
            "/api/v1/settings/new-boards",
            headers=headers,
            json={
                "items": [
                    {
                        "name": "机器人",
                        "description": "活跃方向",
                        "sort_order": 0,
                        "active": True,
                    }
                ]
            },
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["items"][0]["name"], "机器人")
        self.assertEqual(self.repository.saved_by, USER_ID)

        assigned = self.client.put(
            "/api/v1/stocks/600001/new-board",
            headers=headers,
            json={"new_board_id": BOARD_ID},
        )
        self.assertEqual(assigned.status_code, 200)
        self.assertEqual(assigned.json()["new_board"]["id"], BOARD_ID)


if __name__ == "__main__":
    unittest.main()
