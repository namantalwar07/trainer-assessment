"""Password hashing (stdlib scrypt), password policy, roles."""

from __future__ import annotations

import hashlib
import hmac
import os
import re

ROLES = ("operator", "reviewer", "admin")
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        h = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=_N, r=_R, p=_P, dklen=32)
        return hmac.compare_digest(h.hex(), hash_hex)
    except ValueError:
        return False


def password_policy_error(password: str) -> str | None:
    if len(password) < 12:
        return "Password must be at least 12 characters"
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "Password must contain letters and digits"
    if len(set(password)) < 5:
        return "Password is too repetitive"
    return None
