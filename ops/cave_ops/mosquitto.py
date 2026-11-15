"""The broker's password file, written without needing Mosquitto or Docker.

The format is what ``mosquitto_passwd`` writes: ``user:$7$101$<salt>$<hash>``, PBKDF2-HMAC-SHA512
with 101 iterations and a 12-byte salt, both parts in standard Base64.
"""

from __future__ import annotations

import base64
import hashlib
import os

_ITERATIONS = 101
_SALT_BYTES = 12


def password_line(user: str, password: str, *, salt: bytes | None = None) -> str:
    """One line of a Mosquitto password file."""
    salt = os.urandom(_SALT_BYTES) if salt is None else salt
    digest = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, _ITERATIONS)
    salt_b64 = base64.b64encode(salt).decode()
    hash_b64 = base64.b64encode(digest).decode()
    return f"{user}:$7${_ITERATIONS}${salt_b64}${hash_b64}\n"
