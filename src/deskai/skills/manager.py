"""Owns the loaded skill set for the running app (ARCHITECTURE.md §10.1):
installs built-ins, scans the user's skills dir, and exposes Reload — one
instance lives in `AppController` and is shared by every `ChatSession`, the
same way the `ToolRegistry` is.
"""

from __future__ import annotations

import sqlite3

from deskai import paths
from deskai.skills.loader import (
    Skill,
    SkillLoadError,
    builtin_skills_dir,
    install_builtin_skills,
    load_skills,
)
from deskai.storage import repositories

_DISABLED_SETTING_KEY = "skills.disabled_names"
_HASHES_SETTING_KEY = "skills.builtin_hashes"


class SkillsManager:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._skills: list[Skill] = []
        self._all_skills: list[Skill] = []
        self._errors: list[SkillLoadError] = []
        self.reload()

    def reload(self) -> None:
        known_hashes_raw = repositories.get_setting_json(self._conn, _HASHES_SETTING_KEY, {})
        known_hashes = known_hashes_raw if isinstance(known_hashes_raw, dict) else {}
        updated_hashes = install_builtin_skills(
            builtin_skills_dir(), paths.skills_dir(), known_hashes
        )
        if updated_hashes != known_hashes:
            repositories.set_setting_json(self._conn, _HASHES_SETTING_KEY, updated_hashes)

        result = load_skills(paths.skills_dir())
        self._errors = result.errors
        self._all_skills = result.skills

        disabled = set(repositories.get_setting_list(self._conn, _DISABLED_SETTING_KEY))
        self._skills = [s for s in result.skills if s.name not in disabled]

    def enabled_skills(self) -> list[Skill]:
        return list(self._skills)

    def all_skills(self) -> list[Skill]:
        return list(self._all_skills)

    def all_skill_names(self) -> list[str]:
        return [s.name for s in self._all_skills]

    def errors(self) -> list[SkillLoadError]:
        return list(self._errors)

    def get(self, name: str) -> Skill | None:
        return next((s for s in self._skills if s.name == name), None)

    def disabled_names(self) -> set[str]:
        return set(repositories.get_setting_list(self._conn, _DISABLED_SETTING_KEY))

    def set_disabled(self, name: str, disabled: bool) -> None:
        current = self.disabled_names()
        if disabled:
            current.add(name)
        else:
            current.discard(name)
        repositories.set_setting_list(self._conn, _DISABLED_SETTING_KEY, sorted(current))
        self.reload()
