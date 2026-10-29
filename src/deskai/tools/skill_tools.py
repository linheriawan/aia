"""load_skill tool (ARCHITECTURE.md §10.1 step 3): hands the model one
skill's full instructions. The system prompt only ever carries each enabled
skill's name and description, so the model calls this when a request
matches one from that index."""

from __future__ import annotations

from deskai.skills.manager import SkillsManager
from deskai.tools.base import RiskLevel, Tool, ToolContext, ToolResult


class LoadSkillTool(Tool):
    name = "load_skill"
    description = "Get the full instructions for a skill named in the system prompt's skill index."
    input_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    risk = RiskLevel.READ

    def __init__(self, skills_manager: SkillsManager) -> None:
        self._skills_manager = skills_manager

    async def run(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        name = str(args.get("name", ""))
        skill = self._skills_manager.get(name)
        if skill is None:
            available = ", ".join(s.name for s in self._skills_manager.enabled_skills())
            return ToolResult.text(
                f"No such skill: {name!r}. Available: {available or '(none)'}", is_error=True
            )
        return ToolResult.text(skill.body)
