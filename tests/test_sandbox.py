from pathlib import Path

import pytest

from deskai.storage import db, repositories
from deskai.tools.sandbox import (
    Sandbox,
    SandboxError,
    WorkspaceFolder,
    effective_deny_list,
    matches_local_only_rule,
)


def _sandbox(folder: Path, read_only: bool = False) -> Sandbox:
    # Pass an empty deny_list so the deny-list tests below are isolated from
    # whatever real home directory exists on the machine running the suite.
    return Sandbox([WorkspaceFolder(folder, read_only=read_only)], deny_list=[])


def test_relative_path_resolves_against_first_folder(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hi")
    sandbox = _sandbox(tmp_path)
    resolved = sandbox.resolve("notes.txt")
    assert resolved == (tmp_path / "notes.txt").resolve()


def test_absolute_path_inside_folder_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hi")
    sandbox = _sandbox(tmp_path)
    resolved = sandbox.resolve(str(tmp_path / "notes.txt"))
    assert resolved == (tmp_path / "notes.txt").resolve()


def test_absolute_path_outside_folder_is_rejected(tmp_path: Path) -> None:
    other = tmp_path.parent / "outside-sandbox-test"
    other.mkdir(exist_ok=True)
    sandbox = _sandbox(tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()
    with pytest.raises(SandboxError):
        sandbox.resolve(str(other))


def test_dot_dot_traversal_out_of_folder_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = _sandbox(workspace)
    with pytest.raises(SandboxError):
        sandbox.resolve("../secrets.txt")


def test_symlink_escaping_the_folder_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "token.txt").write_text("sensitive")

    link = workspace / "escape"
    link.symlink_to(secret_dir)

    sandbox = _sandbox(workspace)
    with pytest.raises(SandboxError):
        sandbox.resolve("escape/token.txt")


def test_no_folders_attached_rejects_relative_path() -> None:
    sandbox = Sandbox([], deny_list=[])
    with pytest.raises(SandboxError):
        sandbox.resolve("notes.txt")


def test_case_insensitive_folder_match_is_allowed(tmp_path: Path) -> None:
    """Both target OSes (Windows, macOS) default to case-insensitive filesystems:
    a path that only differs in case from an attached folder must still resolve
    inside it, or a legitimate access would be wrongly rejected."""
    workspace = tmp_path / "Workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("hi")
    sandbox = _sandbox(workspace)

    # Resolving a differently-cased path must not raise — on disk it is the same
    # file, even though `Path.resolve()` does not itself normalize case.
    differently_cased = str(workspace).replace("Workspace", "WORKSPACE") + "/notes.txt"
    resolved = sandbox.resolve(differently_cased)
    assert resolved.name == "notes.txt"


def test_case_insensitive_deny_list_match_is_rejected(tmp_path: Path) -> None:
    """The inverse of the above: a denied path must not be reachable just by
    changing its case, since the real filesystem would open the same file."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    denied = tmp_path / "Denied"
    denied.mkdir()
    (denied / "secret.txt").write_text("nope")

    sandbox = Sandbox([WorkspaceFolder(workspace)], deny_list=[denied])
    with pytest.raises(SandboxError):
        sandbox.resolve(str(denied).replace("Denied", "DENIED") + "/secret.txt")


def test_write_to_read_only_folder_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hi")
    sandbox = _sandbox(tmp_path, read_only=True)
    sandbox.resolve("notes.txt")  # read is fine
    with pytest.raises(SandboxError):
        sandbox.resolve("notes.txt", for_write=True)


def test_write_to_writable_folder_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hi")
    sandbox = _sandbox(tmp_path, read_only=False)
    sandbox.resolve("notes.txt", for_write=True)


def test_deny_list_blocks_even_when_also_inside_a_folder(tmp_path: Path) -> None:
    workspace = tmp_path
    denied_subdir = workspace / "denied"
    denied_subdir.mkdir()
    sandbox = Sandbox([WorkspaceFolder(workspace)], deny_list=[denied_subdir])
    with pytest.raises(SandboxError):
        sandbox.resolve("denied/file.txt")


def test_effective_deny_list_includes_the_hardcoded_baseline(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "deskai.db")
    try:
        deny_list = effective_deny_list(conn)
        assert (Path.home() / ".ssh") in deny_list
    finally:
        conn.close()


def test_effective_deny_list_adds_user_configured_paths_without_removing_the_baseline(
    tmp_path: Path,
) -> None:
    conn = db.connect(tmp_path / "deskai.db")
    try:
        extra = str(tmp_path / "secret-project")
        repositories.set_setting_list(conn, "privacy.extra_deny_paths", [extra])

        deny_list = effective_deny_list(conn)

        assert Path(extra) in deny_list
        assert (Path.home() / ".ssh") in deny_list
    finally:
        conn.close()


def test_sandbox_rejects_a_user_configured_extra_deny_path(tmp_path: Path) -> None:
    workspace = tmp_path
    secret = workspace / "secret-project"
    secret.mkdir()
    conn = db.connect(tmp_path / "deskai.db")
    try:
        repositories.set_setting_list(conn, "privacy.extra_deny_paths", [str(secret)])
        sandbox = Sandbox([WorkspaceFolder(workspace)], deny_list=effective_deny_list(conn))
        with pytest.raises(SandboxError):
            sandbox.resolve("secret-project/file.txt")
    finally:
        conn.close()


def test_matches_local_only_rule_true_for_a_folder_under_the_prefix(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "deskai.db")
    try:
        prefix = tmp_path / "sensitive"
        folder = prefix / "project-a"
        repositories.set_setting_list(conn, "privacy.local_only_folder_prefixes", [str(prefix)])
        assert matches_local_only_rule(conn, folder) is True
    finally:
        conn.close()


def test_matches_local_only_rule_false_for_an_unrelated_folder(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "deskai.db")
    try:
        repositories.set_setting_list(
            conn, "privacy.local_only_folder_prefixes", [str(tmp_path / "sensitive")]
        )
        assert matches_local_only_rule(conn, tmp_path / "public") is False
    finally:
        conn.close()


def test_matches_local_only_rule_is_case_insensitive(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "deskai.db")
    try:
        prefix = tmp_path / "Sensitive"
        repositories.set_setting_list(conn, "privacy.local_only_folder_prefixes", [str(prefix)])
        differently_cased = str(prefix).replace("Sensitive", "SENSITIVE") + "/project-a"
        assert matches_local_only_rule(conn, differently_cased) is True
    finally:
        conn.close()


def test_matches_local_only_rule_false_with_no_rules_configured(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "deskai.db")
    try:
        assert matches_local_only_rule(conn, tmp_path / "anything") is False
    finally:
        conn.close()
