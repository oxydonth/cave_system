"""The broker's password file, written and checked without needing Mosquitto or Docker.

The format is what ``mosquitto_passwd`` writes: ``user:$7$101$<salt>$<hash>``, PBKDF2-HMAC-SHA512
with a 12-byte salt, both parts in standard Base64.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os

_ITERATIONS = 101
_SALT_BYTES = 12


def _hash(password: str, salt: bytes, iterations: int) -> str:
    digest = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, iterations)
    return base64.b64encode(digest).decode()


def password_line(user: str, password: str, *, salt: bytes | None = None) -> str:
    """One line of a Mosquitto password file."""
    salt = os.urandom(_SALT_BYTES) if salt is None else salt
    salt_b64 = base64.b64encode(salt).decode()
    return f"{user}:$7${_ITERATIONS}${salt_b64}${_hash(password, salt, _ITERATIONS)}\n"


def accepts(text: str, user: str, password: str) -> bool:
    """Whether a password file (its text) lets ``user`` in with ``password``."""
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name != user:
            continue
        parts = rest.split("$")
        if len(parts) != 5 or parts[1] != "7":
            continue
        try:
            salt = base64.b64decode(parts[3], validate=True)
            iterations = int(parts[2])
        except (binascii.Error, ValueError):
            continue
        if hmac.compare_digest(_hash(password, salt, iterations), parts[4]):
            return True
    return False
