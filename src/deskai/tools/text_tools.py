"""Plain-text tools (ARCHITECTURE.md §8.2): read, write (create/overwrite), and
exact-match edit."""

from __future__ import annotations

import asyncio
from pathlib import Path

from deskai.tools.base import (
    RiskLevel,
    Tool,
    ToolContext,
    ToolResult,
    as_int,
    backup_existing,
    truncate,
    unified_diff,
)


class ReadTextTool(Tool):
    name = "read_text"
    description = "Read a text file (.md, .txt, .csv, .log), optionally a line range."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        },
        "required": ["path"],
    }
    risk = RiskLevel.READ

    async def run(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        resolved = ctx.workspace.resolve(str(args.get("path", "")))
        start_line = args.get("start_line")
        end_line = args.get("end_line")
        return await asyncio.to_thread(
            self._read,
            resolved,
            as_int(start_line, default=0) or None,
            as_int(end_line, default=0) or None,
        )

    def _read(self, path: Path, start_line: int | None, end_line: int | None) -> ToolResult:
        if not path.exists():
            return ToolResult.text(f"Not found: {path}", is_error=True)
        if not path.is_file():
            return ToolResult.text(f"Not a file: {path}", is_error=True)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return ToolResult.text(f"Could not read {path}: {exc}", is_error=True)

        total = len(lines)
        start = max(1, start_line or 1)
        end = min(total, end_line or total)
        if total and start > total:
            return ToolResult.text(
                f"{path.name} has {total} lines; start_line {start} is past the end.",
                is_error=True,
            )

        selected = lines[start - 1 : end]
        numbered = "\n".join(f"{i}: {line}" for i, line in enumerate(selected, start=start))
        header = f"{path.name} — lines {start}-{end} of {total}\n"
        return ToolResult.text(truncate(header + numbered))


class WriteTextTool(Tool):
    name = "write_text"
    description = "Create a new text file, or overwrite an existing one (overwrite=true required)."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "overwrite": {"type": "boolean"},
        },
        "required": ["path", "content"],
    }
    risk = RiskLevel.WRITE

    async def preview(self, args: dict[str, object], ctx: ToolContext) -> str:
        resolved = ctx.workspace.resolve(str(args.get("path", "")), for_write=True)
        content = str(args.get("content", ""))
        overwrite = bool(args.get("overwrite", False))
        return await asyncio.to_thread(self._preview, resolved, content, overwrite)

    def _preview(self, path: Path, content: str, overwrite: bool) -> str:
        if not path.exists():
            return f"Create new file: {path}\n+{len(content.splitlines())} lines"
        if not overwrite:
            return f"{path} already exists — pass overwrite=true to replace it."
        old = path.read_text(encoding="utf-8", errors="replace")
        return unified_diff(path.name, old, content)

    async def run(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        resolved = ctx.workspace.resolve(str(args.get("path", "")), for_write=True)
        content = str(args.get("content", ""))
        overwrite = bool(args.get("overwrite", False))
        return await asyncio.to_thread(self._write, resolved, content, overwrite)

    def _write(self, path: Path, content: str, overwrite: bool) -> ToolResult:
        if path.exists() and not overwrite:
            return ToolResult.text(
                f"{path} already exists — pass overwrite=true to replace it.", is_error=True
            )
        backup_existing(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return ToolResult.text(f"Wrote {len(content)} characters to {path}")


class EditTextTool(Tool):
    name = "edit_text"
    description = "Replace one exact text match in a file. Fails if it matches zero or 2+ times."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old": {"type": "string"},
            "new": {"type": "string"},
        },
        "required": ["path", "old", "new"],
    }
    risk = RiskLevel.WRITE

    async def preview(self, args: dict[str, object], ctx: ToolContext) -> str:
        resolved = ctx.workspace.resolve(str(args.get("path", "")), for_write=True)
        old, new = str(args.get("old", "")), str(args.get("new", ""))
        return await asyncio.to_thread(self._preview, resolved, old, new)

    def _preview(self, path: Path, old: str, new: str) -> str:
        if not path.exists():
            return f"Not found: {path}"
        content = path.read_text(encoding="utf-8", errors="replace")
        count = content.count(old)
        if count != 1:
            return f"'{old}' matches {count} times in {path.name} — edit_text needs exactly 1."
        return unified_diff(path.name, content, content.replace(old, new, 1))

    async def run(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        resolved = ctx.workspace.resolve(str(args.get("path", "")), for_write=True)
        old, new = str(args.get("old", "")), str(args.get("new", ""))
        return await asyncio.to_thread(self._edit, resolved, old, new)

    def _edit(self, path: Path, old: str, new: str) -> ToolResult:
        if not path.exists():
            return ToolResult.text(f"Not found: {path}", is_error=True)
        content = path.read_text(encoding="utf-8", errors="replace")
        count = content.count(old)
        if count != 1:
            return ToolResult.text(
                f"'{old}' matches {count} times; edit_text needs exactly 1 match.", is_error=True
            )
        backup_existing(path)
        path.write_text(content.replace(old, new, 1), encoding="utf-8")
        return ToolResult.text(f"Replaced 1 occurrence in {path}")
