"""Keyring wrapper. The only place secrets (API keys, tokens) may be read or written.

Never write a secret to the DB, logs, exceptions or test fixtures — see CLAUDE.md.
"""

from __future__ import annotations

import keyring
import keyring.errors

_SERVICE = "deskai"


def get_secret(key: str) -> str | None:
    return keyring.get_password(_SERVICE, key)


def set_secret(key: str, value: str) -> None:
    keyring.set_password(_SERVICE, key, value)


def delete_secret(key: str) -> None:
    try:
        keyring.delete_password(_SERVICE, key)
    except keyring.errors.PasswordDeleteError:
        pass
