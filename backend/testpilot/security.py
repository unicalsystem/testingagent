"""Password hashing, session tokens and secret encryption."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings

_ph = PasswordHasher()
_DUMMY_HASH = _ph.hash("timing-equaliser-not-a-real-password")


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, hashed: str | None) -> bool:
    try:
        return _ph.verify(hashed or _DUMMY_HASH, password) and hashed is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_problems(password: str) -> list[str]:
    out = []
    if len(password) < 12:
        out.append("at least 12 characters")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        out.append("letters and digits")
    return out


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    key = get_settings().session_secret.get_secret_value().encode() or b"dev-only"
    return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()


# ---- project secrets -------------------------------------------------------

class SecretsUnavailable(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = get_settings().credential_encryption_key.get_secret_value()
    if not key:
        raise SecretsUnavailable("CREDENTIAL_ENCRYPTION_KEY is not configured")
    try:
        return Fernet(key.encode())
    except ValueError as e:  # malformed key
        raise SecretsUnavailable("CREDENTIAL_ENCRYPTION_KEY is not a valid Fernet key") from e


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise SecretsUnavailable("Stored secret cannot be decrypted with the current key") from e
