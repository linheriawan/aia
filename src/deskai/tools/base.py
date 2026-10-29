"""Tool base class, risk levels, and the per-call context (ARCHITECTURE.md §8.1)."""

from __future__ import annotations

import difflib
import shutil
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from pathlib import Path

from deskai.core.messages import ImagePart, TextPart
from deskai.ocr.engine import OcrEngine
from deskai.providers.base import ModelInfo, Provider
from deskai.tools.sandbox import Sandbox

TRUNCATE_LIMIT = 20_000


class RiskLevel(IntEnum):
    READ = 0
    WRITE = 1
    DESTRUCTIVE = 2


class ConfirmDecision(IntEnum):
    DENY = 0
    ALLOW_ONCE = 1
    ALLOW_SESSION = 2


@dataclass(frozen=True)
class ToolContext:
    session_id: str
    workspace: Sandbox
    ocr: OcrEngine
    conn: sqlite3.Connection | None = None
    provider: Provider | None = None
    model: ModelInfo | None = None
    # One isolated LLM-extraction tool (extract_invoice) tracks duplicate
    # vendor/document-number pairs across calls within a single agent run —
    # a fresh ToolContext is built per `send_message`, so this never leaks
    # between turns or sessions (ARCHITECTURE.md §10.3: "tracked per agent run").
    extraction_batch: set[tuple[str, str]] = field(default_factory=set)


@dataclass(frozen=True)
class ToolResult:
    content: list[TextPart | ImagePart]
    is_error: bool = False

    @classmethod
    def text(cls, text: str, *, is_error: bool = False) -> ToolResult:
        return cls([TextPart(truncate(text))], is_error=is_error)


class Tool(ABC):
    name: str
    description: str
    input_schema: dict[str, object]
    risk: RiskLevel = RiskLevel.READ
    requires_vision: bool = False

    @abstractmethod
    async def run(self, args: dict[str, object], ctx: ToolContext) -> ToolResult: ...

    async def preview(self, args: dict[str, object], ctx: ToolContext) -> str:
        """Human-readable diff/summary of what `run` would change, without changing anything.

        Only called for RiskLevel.WRITE+ tools, to show the user before they
        confirm (ARCHITECTURE.md §8.1). READ tools never need this.
        """
        return ""


def truncate(text: str, limit: int = TRUNCATE_LIMIT) -> str:
    """Cap a tool result's size (ARCHITECTURE.md §6: '~20k characters are truncated')."""
    if len(text) <= limit:
        return text
    return (
        text[:limit] + f"\n\n[truncated — {len(text) - limit} more characters. "
        "Ask for a narrower range, page, or sheet instead of the whole file.]"
    )


def as_int(value: object, default: int) -> int:
    """Coerce a tool-call argument (parsed from the model's JSON) to an int."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return default
    return default


def backup_existing(path: Path) -> Path | None:
    """Copy `path` to `<name>.bak-<timestamp>` before a write, if it exists.

    CLAUDE.md hard rule: write-capable tools must back up existing files.
    """
    if not path.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak-{timestamp}")
    shutil.copy2(path, backup_path)
    return backup_path


def unified_diff(name: str, old: str, new: str) -> str:
    """A unified diff for the confirmation dialog (ARCHITECTURE.md §8.1: text files)."""
    diff = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=name, tofile=name
    )
    text = "".join(diff)
    return text or "(no changes)"
