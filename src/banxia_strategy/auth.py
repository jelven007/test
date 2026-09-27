from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import smtplib
import ssl
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .adapters.identity_settings import IdentityConflict


SESSION_COOKIE = "banxia_session"
CSRF_COOKIE = "banxia_csrf"
EMAIL_PATTERN = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+$",
    re.IGNORECASE,
)


class AuthenticationRejected(ValueError):
    pass


class MailDeliveryError(RuntimeError):
    pass


class RateLimitExceeded(ValueError):
    def __init__(self, retry_after: int):
        super().__init__("请求过于频繁，请稍后再试")
        self.retry_after = retry_after


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        raise ValueError("邮箱格式无效")
    local, domain = email.rsplit("@", 1)
    if len(local) > 64 or ".." in local or domain.startswith("-"):
        raise ValueError("邮箱格式无效")
    return email


def validate_password(value: str) -> str:
    if not 10 <= len(value) <= 128:
        raise ValueError("密码长度必须为 10 至 128 个字符")
    categories = sum(
        (
            any(character.islower() for character in value),
            any(character.isupper() for character in value),
            any(character.isdigit() for character in value),
            any(not character.isalnum() for character in value),
        )
    )
    if categories < 3:
        raise ValueError("密码需包含大小写字母、数字或符号中的至少三类")
    return value


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SlidingWindowLimiter:
    def __init__(self):
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, *, maximum: int, window_seconds: int) -> None:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= maximum:
                retry_after = max(1, int(events[0] + window_seconds - now) + 1)
                raise RateLimitExceeded(retry_after)
            events.append(now)


@dataclass(frozen=True)
class SmtpSettings:
    host: str = "smtp.126.com"
    port: int = 465
    username: Optional[str] = None
    password: Optional[str] = None
    sender: Optional[str] = None
    timeout_seconds: int = 10


class SmtpVerificationMailer:
    def __init__(self, settings: SmtpSettings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.username
            and self.settings.password
            and (self.settings.sender or self.settings.username)
        )

    def send_verification_code(self, email: str, code: str) -> None:
        if not self.configured:
            raise MailDeliveryError(
                "126 邮箱 SMTP 尚未配置，请设置账号和授权码"
            )
        message = EmailMessage()
        message["Subject"] = "一进二系统注册验证码"
        message["From"] = self.settings.sender or self.settings.username
        message["To"] = email
        message.set_content(
            "\n".join(
                (
                    "你正在注册一进二策略系统。",
                    f"验证码：{code}",
                    "验证码 10 分钟内有效，请勿转发。",
                    "如非本人操作，请忽略本邮件。",
                )
            )
        )
        try:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                self.settings.host,
                self.settings.port,
                timeout=self.settings.timeout_seconds,
                context=context,
            ) as client:
                client.login(
                    self.settings.username,
                    self.settings.password,
                )
                client.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise MailDeliveryError("验证码邮件发送失败") from exc


class AuthenticationManager:
    def __init__(
        self,
        repository: Any,
        mailer: Any,
        *,
        secret: Optional[str] = None,
        session_hours: int = 24 * 7,
    ):
        self.repository = repository
        self.mailer = mailer
        self.session_hours = session_hours
        self._secret = (
            secret.encode("utf-8") if secret else secrets.token_bytes(32)
        )
        if secret is not None and len(self._secret) < 32:
            raise ValueError("BANXIA_AUTH_SECRET 至少需要 32 个字符")
        self._passwords = PasswordHasher(
            time_cost=3,
            memory_cost=65536,
            parallelism=2,
        )
        self._dummy_hash = self._passwords.hash(
            secrets.token_urlsafe(24)
        )
        self._limiter = SlidingWindowLimiter()

    def _code_digest(self, email: str, code: str) -> str:
        return hmac.new(
            self._secret,
            f"{email}:{code}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def request_registration_code(
        self,
        *,
        email: str,
        remote_ip: str,
    ) -> dict[str, Any]:
        email = normalize_email(email)
        self._limiter.check(
            f"code:email:{email}",
            maximum=3,
            window_seconds=3600,
        )
        self._limiter.check(
            f"code:ip:{remote_ip}",
            maximum=10,
            window_seconds=3600,
        )
        if self.repository.email_is_registered(email):
            raise IdentityConflict("该邮箱已注册")
        code = f"{secrets.randbelow(1_000_000):06d}"
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        challenge_id = self.repository.create_email_challenge(
            email=email,
            code_digest=self._code_digest(email, code),
            requested_ip=remote_ip,
            expires_at=expires_at,
        )
        try:
            self.mailer.send_verification_code(email, code)
        except Exception:
            self.repository.invalidate_email_challenge(challenge_id)
            raise
        return {
            "challenge_id": challenge_id,
            "email": email,
            "expires_in": 600,
        }

    def register(
        self,
        *,
        challenge_id: str,
        email: str,
        code: str,
        password: str,
        remote_ip: str,
        user_agent: str,
    ) -> dict[str, Any]:
        email = normalize_email(email)
        validate_password(password)
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError("验证码必须为 6 位数字")
        self._limiter.check(
            f"register:ip:{remote_ip}",
            maximum=10,
            window_seconds=3600,
        )
        account = self.repository.register_user(
            challenge_id=challenge_id,
            email=email,
            code_digest=self._code_digest(email, code),
            password_hash=self._passwords.hash(password),
        )
        return {
            **account,
            **self._create_session(
                account["user_id"],
                remote_ip=remote_ip,
                user_agent=user_agent,
            ),
        }

    def login(
        self,
        *,
        email: str,
        password: str,
        remote_ip: str,
        user_agent: str,
    ) -> dict[str, Any]:
        email = normalize_email(email)
        self._limiter.check(
            f"login:ip:{remote_ip}",
            maximum=30,
            window_seconds=900,
        )
        self._limiter.check(
            f"login:email:{email}",
            maximum=10,
            window_seconds=900,
        )
        account = self.repository.get_user_by_email(email)
        encoded = account["password_hash"] if account else self._dummy_hash
        valid = False
        try:
            valid = self._passwords.verify(encoded, password)
        except (VerifyMismatchError, InvalidHashError):
            valid = False
        if account is None or not valid or not account["active"]:
            if account is not None:
                self.repository.record_login_failure(account["user_id"])
            raise AuthenticationRejected("邮箱或密码错误")
        locked_until = account.get("locked_until")
        if locked_until is not None:
            if locked_until.tzinfo is None:
                locked_until = locked_until.replace(tzinfo=timezone.utc)
            if locked_until > datetime.now(timezone.utc):
                raise AuthenticationRejected("登录暂时锁定，请稍后再试")
        self.repository.record_login_success(account["user_id"])
        return {
            "user_id": account["user_id"],
            "email": account["email"],
            **self._create_session(
                account["user_id"],
                remote_ip=remote_ip,
                user_agent=user_agent,
            ),
        }

    def _create_session(
        self,
        user_id: str,
        *,
        remote_ip: str,
        user_agent: str,
    ) -> dict[str, Any]:
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(
            hours=self.session_hours
        )
        self.repository.create_user_session(
            token_digest=_digest(session_token),
            csrf_digest=_digest(csrf_token),
            user_id=user_id,
            user_agent=user_agent,
            remote_ip=remote_ip,
            expires_at=expires_at,
        )
        return {
            "session_token": session_token,
            "csrf_token": csrf_token,
            "expires_at": expires_at,
        }

    def resolve_session(self, session_token: Optional[str]):
        if not session_token:
            return None
        return self.repository.get_user_session(_digest(session_token))

    @staticmethod
    def verify_csrf(session: dict[str, Any], csrf_token: Optional[str]) -> bool:
        if not csrf_token:
            return False
        return hmac.compare_digest(
            session["csrf_digest"],
            _digest(csrf_token),
        )

    def logout(self, session_token: Optional[str]) -> None:
        if session_token:
            self.repository.revoke_user_session(_digest(session_token))
