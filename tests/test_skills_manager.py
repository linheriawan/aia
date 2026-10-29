"""Tests for SkillsManager: the install-on-startup + enable/disable +
Reload lifecycle a running app needs (ARCHITECTURE.md §10.1)."""

from __future__ import annotations

from pathlib import Path

from deskai import paths
from deskai.skills.manager import SkillsManager
from deskai.storage import db


def _conn(tmp_path: Path):  # type: ignore[no-untyped-def]
    return db.connect(tmp_path / "deskai.db")


def test_manager_installs_builtin_skills_on_first_use(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "data")
    manager = SkillsManager(_conn(tmp_path))

    names = {s.name for s in manager.enabled_skills()}
    assert "invoice-list" in names
    assert manager.errors() == []


def test_manager_set_disabled_removes_from_enabled_list(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "data")
    manager = SkillsManager(_conn(tmp_path))

    manager.set_disabled("invoice-list", True)
    assert "invoice-list" not in {s.name for s in manager.enabled_skills()}
    assert "invoice-list" in manager.all_skill_names()
    assert manager.get("invoice-list") is None

    manager.set_disabled("invoice-list", False)
    assert "invoice-list" in {s.name for s in manager.enabled_skills()}


def test_manager_disabled_state_persists_across_a_fresh_manager(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "data")
    conn = _conn(tmp_path)
    SkillsManager(conn).set_disabled("invoice-list", True)

    reloaded = SkillsManager(conn)
    assert "invoice-list" not in {s.name for s in reloaded.enabled_skills()}


def test_manager_reload_picks_up_a_newly_added_user_skill(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "data")
    manager = SkillsManager(_conn(tmp_path))

    user_skill = paths.skills_dir() / "custom" / "SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text("---\nname: custom\ndescription: Custom.\n---\nDo the custom thing.\n")

    manager.reload()
    assert "custom" in {s.name for s in manager.enabled_skills()}


def test_manager_get_returns_none_for_unknown_skill(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "data")
    manager = SkillsManager(_conn(tmp_path))
    assert manager.get("does-not-exist") is None
