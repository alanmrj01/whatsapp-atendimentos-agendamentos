from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

ACCESS_TTL_SECONDS = 600
REFRESH_TTL_SECONDS = 30 * 24 * 3600
COOKIE_NAME = "atende_refresh"
COOKIE_PATH = "/api/v1/auth"
hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, type=Type.ID)


def normalize_email(value: str) -> str:
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise ValueError("Invalid email")
    return value


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 1024:
        raise ValueError("Password must contain 12 to 1024 characters")
    return hasher.hash(password)


@lru_cache(maxsize=1)
def dummy_hash() -> str:
    return hasher.hash(secrets.token_urlsafe(32))


def verify_password(password: str, stored_hash: str | None) -> bool:
    try:
        valid = hasher.verify(stored_hash or dummy_hash(), password)
        return bool(valid and stored_hash)
    except (VerificationError, InvalidHashError):
        return False


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def access_token(user_id: UUID, session_id: UUID, key: str) -> str:
    return jwt.encode({
        "sub": str(user_id), "session_id": str(session_id),
        "exp": datetime.now(UTC) + timedelta(seconds=ACCESS_TTL_SECONDS),
        "jti": str(uuid4()),
    }, key, algorithm="HS256")


def decode_access(token: str, key: str) -> tuple[UUID, UUID]:
    try:
        claims = jwt.decode(token, key, algorithms=["HS256"], options={
            "require": ["sub", "session_id", "exp", "jti"],
        })
        UUID(claims["jti"])
        return UUID(claims["sub"]), UUID(claims["session_id"])
    except (jwt.PyJWTError, ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("Invalid access token") from None
