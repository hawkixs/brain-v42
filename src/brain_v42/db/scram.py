"""PostgreSQL SCRAM verifiers without sending cleartext to the database."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import stringprep
from unicodedata import ucd_3_2_0


def _password_bytes(password: str) -> bytes:
    """RFC 4013 SASLprep, with PostgreSQL's raw UTF-8 fallback on prohibited input."""
    mapped = "".join(
        " " if stringprep.in_table_c12(char) else char
        for char in password
        if not stringprep.in_table_b1(char)
    )
    prepared = ucd_3_2_0.normalize("NFKC", mapped)
    prohibited = (
        stringprep.in_table_a1,
        stringprep.in_table_c12,
        stringprep.in_table_c21,
        stringprep.in_table_c22,
        stringprep.in_table_c3,
        stringprep.in_table_c4,
        stringprep.in_table_c5,
        stringprep.in_table_c6,
        stringprep.in_table_c7,
        stringprep.in_table_c8,
        stringprep.in_table_c9,
    )
    if any(check(char) for char in prepared for check in prohibited):
        return password.encode("utf-8")
    if any(stringprep.in_table_d1(char) for char in prepared) and (
        any(stringprep.in_table_d2(char) for char in prepared)
        or not stringprep.in_table_d1(prepared[0])
        or not stringprep.in_table_d1(prepared[-1])
    ):
        return password.encode("utf-8")
    return prepared.encode("utf-8")


def scram_verifier(password: str) -> str:
    """RFC 5802/7677 keys in PostgreSQL's storage format, using a fresh 128-bit salt."""
    salt = secrets.token_bytes(16)
    iterations = 4096
    salted_password = hashlib.pbkdf2_hmac("sha256", _password_bytes(password), salt, iterations)
    client_key = hmac.digest(salted_password, b"Client Key", "sha256")
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.digest(salted_password, b"Server Key", "sha256")
    salt_text, stored_text, server_text = (
        base64.b64encode(value).decode("ascii") for value in (salt, stored_key, server_key)
    )
    return f"SCRAM-SHA-256${iterations}:{salt_text}${stored_text}:{server_text}"
