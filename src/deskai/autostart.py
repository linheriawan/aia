"""Launch at login (ARCHITECTURE.md §4, §15 Phase 7): a Windows registry Run
key, or a macOS LaunchAgent plist. Linux is out of scope for v1 — desktop
environments vary too much for one approach to cover — so `is_enabled`
reports False there and `enable`/`disable` raise `AutostartError`.

Not a model-facing tool (no `ToolContext`, nothing under a workspace
folder), so this doesn't go through `tools/sandbox.py` — the same as
`paths.py` and `storage/secrets.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

_APP_NAME = "deskai"
_MACOS_LABEL = "com.deskai.app"


class AutostartError(Exception):
    """Could not read or write the OS's own autostart mechanism."""


def _launch_command() -> list[str]:
    """A packaged build (PyInstaller sets `sys.frozen`) runs its own
    executable directly; in dev, re-launch through the same interpreter."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "deskai"]


def is_enabled() -> bool:
    if sys.platform == "win32":
        return _windows_is_enabled()
    if sys.platform == "darwin":
        return _macos_is_enabled()
    return False


def enable() -> None:
    if sys.platform == "win32":
        _windows_enable()
    elif sys.platform == "darwin":
        _macos_enable()
    else:
        raise AutostartError(f"launch at login isn't supported on {sys.platform}")


def disable() -> None:
    if sys.platform == "win32":
        _windows_disable()
    elif sys.platform == "darwin":
        _macos_disable()
    else:
        raise AutostartError(f"launch at login isn't supported on {sys.platform}")


# --- Windows: HKCU\Software\Microsoft\Windows\CurrentVersion\Run -----------


def _windows_run_key() -> object:
    import winreg

    return winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_ALL_ACCESS,
    )


def _windows_is_enabled() -> bool:
    import winreg

    try:
        with _windows_run_key() as key:
            winreg.QueryValueEx(key, _APP_NAME)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AutostartError(str(exc)) from exc


def _windows_enable() -> None:
    import winreg

    command = " ".join(f'"{part}"' for part in _launch_command())
    try:
        with _windows_run_key() as key:
            winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, command)
    except OSError as exc:
        raise AutostartError(str(exc)) from exc


def _windows_disable() -> None:
    import winreg

    try:
        with _windows_run_key() as key:
            winreg.DeleteValue(key, _APP_NAME)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise AutostartError(str(exc)) from exc


# --- macOS: ~/Library/LaunchAgents/com.deskai.app.plist ---------------------


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_MACOS_LABEL}.plist"


def _macos_is_enabled() -> bool:
    return _macos_plist_path().exists()


def _macos_enable() -> None:
    import plistlib

    path = _macos_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    plist: dict[str, object] = {
        "Label": _MACOS_LABEL,
        "ProgramArguments": _launch_command(),
        "RunAtLoad": True,
    }
    try:
        with path.open("wb") as f:
            plistlib.dump(plist, f)
    except OSError as exc:
        raise AutostartError(str(exc)) from exc


def _macos_disable() -> None:
    path = _macos_plist_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise AutostartError(str(exc)) from exc
