"""
Passwords, JWT access tokens, refresh tokens, and OTPs.

"""

import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID, uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from jose import JWTError, jwt

from app.config import get_settings

settings = get_settings()
_password_hasher = PasswordHasher()

PASSWORD_POLICY_MESSAGE = (
    "Password must be at least 8 characters and include a letter, a number, and a symbol"
)

_HAS_UPPER = re.compile(r"[A-Z]")
_HAS_LOWER = re.compile(r"[a-z]")
_HAS_DIGIT = re.compile(r"\d")
_HAS_SYMBOL = re.compile(r"[^A-Za-z0-9]")


def validate_password_complexity(password: str) -> str:
    """Require 8–128 chars with upper, lower, digit, symbol; no edge spaces."""
    if password != password.strip():
        raise ValueError(PASSWORD_POLICY_MESSAGE)
    if not 8 <= len(password) <= 128:
        raise ValueError(PASSWORD_POLICY_MESSAGE)
    if not _HAS_UPPER.search(password):
        raise ValueError(PASSWORD_POLICY_MESSAGE)
    if not _HAS_LOWER.search(password):
        raise ValueError(PASSWORD_POLICY_MESSAGE)
    if not _HAS_DIGIT.search(password):
        raise ValueError(PASSWORD_POLICY_MESSAGE)
    if not _HAS_SYMBOL.search(password):
        raise ValueError(PASSWORD_POLICY_MESSAGE)
    return password


class PasswordManager:
    """Hash passwords and check them on login."""

    @staticmethod
    def hash(password: str) -> str:
        return _password_hasher.hash(password)

    @staticmethod
    def verify(plain: str, hashed: str) -> bool:
        try:
            return _password_hasher.verify(hashed, plain)
        except VerifyMismatchError:
            return False


class TokenManager:
    """Create/read access JWTs and random refresh tokens."""

    @staticmethod
    def create_access_token(*, user_id: UUID, tenant_id: UUID, email: str) -> str:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.access_token_expire_minutes
        )
        payload = {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "email": email,
            "type": "access",
            "jti": str(uuid4()),
            "exp": expire,
        }
        return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)

    @staticmethod
    def decode_access_token(token: str) -> dict:
        try:
            payload = jwt.decode(
                token,
                settings.jwt_secret_key,
                algorithms=[settings.jwt_algorithm],
            )
        except JWTError as exc:
            raise ValueError("Invalid or expired token") from exc

        if payload.get("type") != "access":
            raise ValueError("Invalid or expired token")

        return payload

    @staticmethod
    def issue_refresh_token() -> str:
        """Random string the browser stores (we only save a hash)."""
        return secrets.token_urlsafe(32)

    @staticmethod
    def hash_refresh_token(token: str) -> str:
        return hmac.new(
            settings.jwt_secret_key.encode("utf-8"),
            token.encode("utf-8"),
            sha256,
        ).hexdigest()

    @staticmethod
    def refresh_expiry_time() -> datetime:
        return datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)


class OTPManager:
    """Create and check 6-digit email OTPs (stored as hashes)."""

    @staticmethod
    def issue_code() -> str:
        number = secrets.randbelow(1_000_000)
        return f"{number:06d}"

    @staticmethod
    def hash_code(otp: str) -> str:
        return hmac.new(
            settings.jwt_secret_key.encode("utf-8"),
            otp.encode("utf-8"),
            sha256,
        ).hexdigest()

    @staticmethod
    def expiry_time() -> datetime:
        return datetime.now(timezone.utc) + timedelta(minutes=settings.otp_expire_minutes)

    @staticmethod
    def verify(stored_hash: str | None, provided: str) -> bool:
        if not stored_hash:
            return False
        provided_hash = OTPManager.hash_code(provided)
        return hmac.compare_digest(stored_hash, provided_hash)

    @staticmethod
    def is_expired(expires_at: datetime | None) -> bool:
        if expires_at is None:
            return True

        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        return datetime.now(timezone.utc) > expires_at
