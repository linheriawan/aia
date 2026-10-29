"""Per-session workspace sandbox (ARCHITECTURE.md §7).

Every tool that touches a path must resolve it through here (CLAUDE.md hard
rule). Targets are Windows and macOS only (CLAUDE.md), and both default to
case-insensitive filesystems, so containment is checked case-insensitively on
purpose: a lexical, case-sensitive check would let a differently-cased path
slip past the deny-list or a folder boundary even though the OS resolves it
to the same file.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from deskai import paths


class SandboxError(Exception):
    """A requested path is outside the workspace, denied, or its folder is read-only."""


@dataclass(frozen=True)
class WorkspaceFolder:
    path: Path
    read_only: bool = False


def default_deny_list() -> list[Path]:
    home = Path.home()
    return [home / ".ssh", home / ".aws", home / ".gnupg", paths.data_dir()]


def effective_deny_list(conn: sqlite3.Connection) -> list[Path]:
    """The hardcoded baseline plus the user's own additions (Settings → Privacy).

    The baseline is never user-removable — only extendable.
    """
    from deskai.storage import repositories  # local import: sandbox.py stays DB-free otherwise

    extra = [Path(p) for p in repositories.get_setting_list(conn, "privacy.extra_deny_paths")]
    return default_deny_list() + extra


def _normalize_for_compare(path: Path) -> str:
    text = str(path)
    if len(text) > 1 and text.endswith(os.sep):
        text = text.rstrip(os.sep)
    return text.casefold()


def _is_within(candidate: Path, root: Path) -> bool:
    candidate_str = _normalize_for_compare(candidate)
    root_str = _normalize_for_compare(root)
    return candidate_str == root_str or candidate_str.startswith(root_str + os.sep)


def matches_local_only_rule(conn: sqlite3.Connection, folder_path: str | Path) -> bool:
    """Whether `folder_path` falls under one of the Local-only prefixes (Settings → Privacy):
    a session with such a folder attached must use a provider marked local (ARCHITECTURE.md §7).
    """
    from deskai.storage import repositories  # local import: sandbox.py stays DB-free otherwise

    prefixes = repositories.get_setting_list(conn, "privacy.local_only_folder_prefixes")
    resolved = _normalize_for_compare(Path(folder_path).resolve(strict=False))
    return any(
        resolved.startswith(_normalize_for_compare(Path(p).resolve(strict=False))) for p in prefixes
    )


class Sandbox:
    def __init__(
        self, folders: list[WorkspaceFolder] | None = None, deny_list: list[Path] | None = None
    ) -> None:
        self.folders = folders or []
        self._deny_list = [
            p.resolve(strict=False)
            for p in (deny_list if deny_list is not None else default_deny_list())
        ]

    def has_folders(self) -> bool:
        return bool(self.folders)

    def resolve(self, raw_path: str, *, for_write: bool = False) -> Path:
        """Resolve `raw_path` against the workspace, raising `SandboxError` if it escapes it.

        Relative paths resolve against the first workspace folder. Absolute
        paths, `..` segments and symlinks are all followed via `Path.resolve`
        before the containment check, so none of them can be used to escape.
        """
        if not raw_path:
            raise SandboxError("Empty path")

        candidate = Path(raw_path)
        if not candidate.is_absolute():
            if not self.folders:
                raise SandboxError("No workspace folder attached — ask the user to attach one")
            candidate = self.folders[0].path / candidate

        resolved = candidate.resolve(strict=False)

        for denied in self._deny_list:
            if _is_within(resolved, denied):
                raise SandboxError(f"Path is in the global deny-list: {raw_path}")

        for folder in self.folders:
            if _is_within(resolved, folder.path.resolve(strict=False)):
                if for_write and folder.read_only:
                    raise SandboxError(f"Folder is read-only, cannot write: {folder.path}")
                return resolved

        raise SandboxError(f"Path is outside the attached workspace folders: {raw_path}")
