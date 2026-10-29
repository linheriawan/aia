"""Tests for skill parsing, scanning, and the built-in-skill install/update
logic that never overwrites a user's edited copy (ARCHITECTURE.md §10.1)."""

from __future__ import annotations

from pathlib import Path

from deskai.skills.loader import (
    builtin_skills_dir,
    install_builtin_skills,
    load_skills,
    parse_skill_file,
    skill_index,
)


def _write_skill(
    path: Path, name: str = "test-skill", description: str = "A test skill.", body: str = "Do it."
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\nversion: 1\n"
        f"tools: [list_dir]\n---\n# Instructions\n{body}\n"
    )
    return path


def test_parse_skill_file_reads_frontmatter_and_body(tmp_path: Path) -> None:
    skill_file = _write_skill(tmp_path / "s" / "SKILL.md")
    skill = parse_skill_file(skill_file)
    assert skill.name == "test-skill"
    assert skill.description == "A test skill."
    assert skill.version == 1
    assert skill.tools == ["list_dir"]
    assert "Do it." in skill.body


def test_parse_skill_file_missing_frontmatter_raises(tmp_path: Path) -> None:
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("# Just a heading, no frontmatter")
    try:
        parse_skill_file(skill_file)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "frontmatter" in str(exc)


def test_parse_skill_file_unclosed_frontmatter_raises(tmp_path: Path) -> None:
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("---\nname: x\ndescription: y\n# body without closing delimiter")
    try:
        parse_skill_file(skill_file)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "closed" in str(exc)


def test_parse_skill_file_missing_name_raises(tmp_path: Path) -> None:
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("---\ndescription: y\n---\nbody")
    try:
        parse_skill_file(skill_file)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "name" in str(exc)


def test_parse_skill_file_empty_body_raises(tmp_path: Path) -> None:
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("---\nname: x\ndescription: y\n---\n")
    try:
        parse_skill_file(skill_file)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "empty" in str(exc)


def test_load_skills_scans_directory_and_skips_invalid(tmp_path: Path) -> None:
    _write_skill(tmp_path / "good" / "SKILL.md", name="good")
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "SKILL.md").write_text("not valid frontmatter at all")
    (tmp_path / "no-skill-file").mkdir()

    result = load_skills(tmp_path)

    assert [s.name for s in result.skills] == ["good"]
    assert len(result.errors) == 1
    assert "bad" in str(result.errors[0].path)


def test_load_skills_on_missing_directory_returns_empty_result(tmp_path: Path) -> None:
    result = load_skills(tmp_path / "does-not-exist")
    assert result.skills == []
    assert result.errors == []


def test_skill_index_lists_name_and_description() -> None:
    result = load_skills(builtin_skills_dir())
    assert result.errors == []
    index = skill_index(result.skills)
    assert "invoice-list" in index


def test_skill_index_empty() -> None:
    assert skill_index([]) == "(none installed)"


def test_install_builtin_skills_copies_on_first_run(tmp_path: Path) -> None:
    builtin_dir = tmp_path / "builtin"
    _write_skill(builtin_dir / "demo" / "SKILL.md", name="demo")
    user_dir = tmp_path / "user"

    hashes = install_builtin_skills(builtin_dir, user_dir, {})

    assert (user_dir / "demo" / "SKILL.md").exists()
    assert "demo" in hashes


def test_install_builtin_skills_does_not_overwrite_user_edits(tmp_path: Path) -> None:
    builtin_dir = tmp_path / "builtin"
    builtin_file = _write_skill(builtin_dir / "demo" / "SKILL.md", name="demo", body="Original.")
    user_dir = tmp_path / "user"

    hashes = install_builtin_skills(builtin_dir, user_dir, {})

    user_file = user_dir / "demo" / "SKILL.md"
    user_file.write_text(user_file.read_text().replace("Original.", "User's own edit."))

    # Ship an updated built-in version.
    builtin_file.write_text(builtin_file.read_text().replace("Original.", "Updated upstream."))

    updated_hashes = install_builtin_skills(builtin_dir, user_dir, hashes)

    assert "User's own edit." in user_file.read_text()
    # The hash map is left pointing at the (now stale) previously-installed
    # version, since nothing was actually installed over the user's edit.
    assert updated_hashes["demo"] == hashes["demo"]


def test_install_builtin_skills_updates_when_user_has_not_edited(tmp_path: Path) -> None:
    builtin_dir = tmp_path / "builtin"
    builtin_file = _write_skill(builtin_dir / "demo" / "SKILL.md", name="demo", body="Original.")
    user_dir = tmp_path / "user"

    hashes = install_builtin_skills(builtin_dir, user_dir, {})
    builtin_file.write_text(builtin_file.read_text().replace("Original.", "Updated upstream."))

    updated_hashes = install_builtin_skills(builtin_dir, user_dir, hashes)

    user_file = user_dir / "demo" / "SKILL.md"
    assert "Updated upstream." in user_file.read_text()
    assert updated_hashes["demo"] != hashes["demo"]
