"""Tests for the load_skill tool (ARCHITECTURE.md §10.1 step 3)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from deskai.ocr.engine import OcrEngine
from deskai.skills.loader import Skill
from deskai.storage import db
from deskai.tools.base import ToolContext
from deskai.tools.sandbox import Sandbox
from deskai.tools.skill_tools import LoadSkillTool


class FakeSkillsManager:
    def __init__(self, skills: list[Skill]) -> None:
        self._skills = skills

    def enabled_skills(self) -> list[Skill]:
        return self._skills

    def get(self, name: str) -> Skill | None:
        return next((s for s in self._skills if s.name == name), None)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _ctx(tmp_path: Path) -> ToolContext:
    conn = db.connect(tmp_path / "deskai.db")
    return ToolContext(session_id="s1", workspace=Sandbox([], deny_list=[]), ocr=OcrEngine(conn))


def test_load_skill_returns_body_for_known_skill(tmp_path: Path) -> None:
    skill = Skill(
        name="invoice-list",
        description="desc",
        version=1,
        tools=[],
        body="Full instructions here.",
        source_path=Path("SKILL.md"),
    )
    tool = LoadSkillTool(FakeSkillsManager([skill]))

    result = _run(tool.run({"name": "invoice-list"}, _ctx(tmp_path)))

    assert not result.is_error
    assert result.content[0].text == "Full instructions here."


def test_load_skill_unknown_name_is_an_error_listing_available(tmp_path: Path) -> None:
    skill = Skill(
        name="invoice-list",
        description="desc",
        version=1,
        tools=[],
        body="body",
        source_path=Path("SKILL.md"),
    )
    tool = LoadSkillTool(FakeSkillsManager([skill]))

    result = _run(tool.run({"name": "nope"}, _ctx(tmp_path)))

    assert result.is_error
    assert "invoice-list" in result.content[0].text


def test_load_skill_with_no_skills_available(tmp_path: Path) -> None:
    tool = LoadSkillTool(FakeSkillsManager([]))

    result = _run(tool.run({"name": "nope"}, _ctx(tmp_path)))

    assert result.is_error
    assert "(none)" in result.content[0].text
