"""User data directory layout, via platformdirs so it's correct per-OS."""

from __future__ import annotations

from pathlib import Path

import platformdirs

APP_NAME = "deskai"


def data_dir() -> Path:
    return Path(platformdirs.user_data_dir(APP_NAME))


def db_path() -> Path:
    return data_dir() / "deskai.db"


def logs_dir() -> Path:
    return data_dir() / "logs"


def backups_dir() -> Path:
    return data_dir() / "backups"


def skills_dir() -> Path:
    return data_dir() / "skills"
