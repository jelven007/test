from __future__ import annotations

import hmac
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence


class IdentityConflict(ValueError):
    pass


class VerificationRejected(ValueError):
    pass


class IdentitySettingsMixin:
    """PostgreSQL-backed users, sessions, and editable board references."""

    def email_is_registered(self, email: str) -> bool:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM banxia.app_user
                        WHERE email = %s AND active
                    )
                    """,
                    (email,),
                )
                return bool(cursor.fetchone()[0])

    def create_email_challenge(
        self,
        *,
        email: str,
        code_digest: str,
        requested_ip: str,
        expires_at: datetime,
    ) -> str:
        challenge_id = str(uuid.uuid4())
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE banxia.email_verification_challenge
                    SET consumed_at = now()
                    WHERE email = %s
                      AND purpose = 'register'
                      AND consumed_at IS NULL
                    """,
                    (email,),
                )
                cursor.execute(
                    """
                    INSERT INTO banxia.email_verification_challenge (
                        challenge_id, email, code_digest, requested_ip,
                        expires_at
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        challenge_id,
                        email,
                        code_digest,
                        requested_ip,
                        expires_at,
                    ),
                )
        return challenge_id

    def invalidate_email_challenge(self, challenge_id: str) -> None:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE banxia.email_verification_challenge
                    SET consumed_at = now()
                    WHERE challenge_id = %s AND consumed_at IS NULL
                    """,
                    (challenge_id,),
                )

    def register_user(
        self,
        *,
        challenge_id: str,
        email: str,
        code_digest: str,
        password_hash: str,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        invalid_code = False
        user_id = ""
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT email, code_digest, expires_at, attempt_count,
                           consumed_at
                    FROM banxia.email_verification_challenge
                    WHERE challenge_id = %s
                    FOR UPDATE
                    """,
                    (challenge_id,),
                )
                challenge = cursor.fetchone()
                if challenge is None or str(challenge[0]) != email:
                    raise VerificationRejected("验证码无效")
                if challenge[4] is not None:
                    raise VerificationRejected("验证码已使用")
                expires_at = challenge[2]
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at <= now:
                    raise VerificationRejected("验证码已过期")
                if int(challenge[3]) >= 5:
                    raise VerificationRejected("验证码尝试次数过多")
                if not hmac.compare_digest(str(challenge[1]), code_digest):
                    cursor.execute(
                        """
                        UPDATE banxia.email_verification_challenge
                        SET attempt_count = LEAST(attempt_count + 1, 5)
                        WHERE challenge_id = %s
                        """,
                        (challenge_id,),
                    )
                    invalid_code = True
                else:
                    cursor.execute(
                        "SELECT 1 FROM banxia.app_user WHERE email = %s",
                        (email,),
                    )
                    if cursor.fetchone() is not None:
                        raise IdentityConflict("该邮箱已注册")
                    user_id = str(uuid.uuid4())
                    cursor.execute(
                        """
                        INSERT INTO banxia.app_user (
                            user_id, email, password_hash, email_verified_at
                        ) VALUES (%s, %s, %s, %s)
                        """,
                        (user_id, email, password_hash, now),
                    )
                    cursor.execute(
                        """
                        UPDATE banxia.email_verification_challenge
                        SET consumed_at = %s
                        WHERE challenge_id = %s
                        """,
                        (now, challenge_id),
                    )
        if invalid_code:
            raise VerificationRejected("验证码无效")
        return {"user_id": user_id, "email": email}

    def get_user_by_email(self, email: str) -> Optional[dict[str, Any]]:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT user_id, email, password_hash, active, locked_until
                    FROM banxia.app_user
                    WHERE email = %s
                    """,
                    (email,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return {
            "user_id": str(row[0]),
            "email": str(row[1]),
            "password_hash": str(row[2]),
            "active": bool(row[3]),
            "locked_until": row[4],
        }

    def record_login_failure(self, user_id: str) -> None:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE banxia.app_user
                    SET failed_login_count = failed_login_count + 1,
                        locked_until = CASE
                            WHEN failed_login_count + 1 >= 5
                            THEN now() + interval '15 minutes'
                            ELSE locked_until
                        END,
                        updated_at = now()
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )

    def record_login_success(self, user_id: str) -> None:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE banxia.app_user
                    SET failed_login_count = 0,
                        locked_until = NULL,
                        updated_at = now()
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )

    def create_user_session(
        self,
        *,
        token_digest: str,
        csrf_digest: str,
        user_id: str,
        user_agent: str,
        remote_ip: str,
        expires_at: datetime,
    ) -> None:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO banxia.user_session (
                        token_digest, csrf_digest, user_id, user_agent,
                        remote_ip, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        token_digest,
                        csrf_digest,
                        user_id,
                        user_agent[:500],
                        remote_ip,
                        expires_at,
                    ),
                )

    def get_user_session(
        self,
        token_digest: str,
    ) -> Optional[dict[str, Any]]:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT session.user_id, account.email,
                           session.csrf_digest, session.expires_at
                    FROM banxia.user_session session
                    JOIN banxia.app_user account
                      ON account.user_id = session.user_id
                    WHERE session.token_digest = %s
                      AND session.revoked_at IS NULL
                      AND session.expires_at > now()
                      AND account.active
                    """,
                    (token_digest,),
                )
                row = cursor.fetchone()
                if row is not None:
                    cursor.execute(
                        """
                        UPDATE banxia.user_session
                        SET last_seen_at = now()
                        WHERE token_digest = %s
                          AND last_seen_at < now() - interval '5 minutes'
                        """,
                        (token_digest,),
                    )
        if row is None:
            return None
        return {
            "user_id": str(row[0]),
            "email": str(row[1]),
            "csrf_digest": str(row[2]),
            "expires_at": row[3],
        }

    def revoke_user_session(self, token_digest: str) -> None:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE banxia.user_session
                    SET revoked_at = now()
                    WHERE token_digest = %s AND revoked_at IS NULL
                    """,
                    (token_digest,),
                )

    def list_new_boards(
        self,
        *,
        active_only: bool = False,
    ) -> list[dict[str, Any]]:
        where = "WHERE active" if active_only else ""
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT new_board_id, name, description, sort_order, active
                    FROM banxia.new_board_reference
                    {where}
                    ORDER BY sort_order, name
                    """
                )
                rows = cursor.fetchall()
        return [
            {
                "id": str(row[0]),
                "name": str(row[1]),
                "description": str(row[2]),
                "sort_order": int(row[3]),
                "active": bool(row[4]),
            }
            for row in rows
        ]

    def replace_new_boards(
        self,
        items: Sequence[Mapping[str, Any]],
        *,
        user_id: str,
    ) -> list[dict[str, Any]]:
        retained_ids: list[str] = []
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT new_board_id
                    FROM banxia.new_board_reference
                    FOR UPDATE
                    """
                )
                existing_ids = {str(row[0]) for row in cursor.fetchall()}
                for item in items:
                    item_id = str(item.get("id") or uuid.uuid4())
                    if item.get("id") and item_id not in existing_ids:
                        raise ValueError("新板块记录不存在")
                    retained_ids.append(item_id)
                    cursor.execute(
                        """
                        INSERT INTO banxia.new_board_reference (
                            new_board_id, name, description, sort_order,
                            active, created_by
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (new_board_id) DO UPDATE SET
                            name = EXCLUDED.name,
                            description = EXCLUDED.description,
                            sort_order = EXCLUDED.sort_order,
                            active = EXCLUDED.active,
                            updated_at = now()
                        """,
                        (
                            item_id,
                            item["name"],
                            item.get("description", ""),
                            item.get("sort_order", 0),
                            item.get("active", True),
                            user_id,
                        ),
                    )
                if retained_ids:
                    cursor.execute(
                        """
                        DELETE FROM banxia.new_board_reference
                        WHERE NOT (new_board_id = ANY(%s::uuid[]))
                        """,
                        (retained_ids,),
                    )
                else:
                    cursor.execute("DELETE FROM banxia.new_board_reference")
        return self.list_new_boards()

    def set_security_new_board(
        self,
        symbol: str,
        new_board_id: Optional[str],
    ) -> dict[str, Any]:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                if new_board_id is not None:
                    cursor.execute(
                        """
                        SELECT 1 FROM banxia.new_board_reference
                        WHERE new_board_id = %s AND active
                        """,
                        (new_board_id,),
                    )
                    if cursor.fetchone() is None:
                        raise ValueError("新板块选项无效")
                cursor.execute(
                    """
                    UPDATE banxia.security_master
                    SET new_board_id = %s
                    WHERE symbol = %s
                      AND instrument_type = 'stock'
                      AND listing_status = 'active'
                    RETURNING instrument_id
                    """,
                    (new_board_id, symbol),
                )
                if cursor.fetchone() is None:
                    raise LookupError("股票不存在")
        result = self.get_security(symbol)
        if result is None:
            raise LookupError("股票不存在")
        return result
