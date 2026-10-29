"""Tests for launch-at-login (ARCHITECTURE.md §4, §15 Phase 7).

macOS is exercised for real (writing/removing a plist), but pointed at a
temporary HOME so it never touches the real user's LaunchAgents folder.
Windows is exercised against a fake in-memory `winreg` module injected via
`sys.modules`, since the real one doesn't exist on this dev machine at all.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from deskai import autostart


def test_launch_command_in_dev_uses_module_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert autostart._launch_command() == [sys.executable, "-m", "deskai"]


def test_launch_command_when_frozen_uses_the_executable_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert autostart._launch_command() == [sys.executable]


def test_unsupported_platform_reports_disabled_and_raises_on_toggle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert autostart.is_enabled() is False
    with pytest.raises(autostart.AutostartError):
        autostart.enable()
    with pytest.raises(autostart.AutostartError):
        autostart.disable()


# --- macOS: real plist read/write, against a fake HOME -----------------------


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def test_macos_enable_writes_a_plist(fake_home: Path) -> None:
    assert autostart.is_enabled() is False
    autostart.enable()
    assert autostart.is_enabled() is True

    plist_path = fake_home / "Library" / "LaunchAgents" / "com.deskai.app.plist"
    assert plist_path.exists()


def test_macos_disable_removes_the_plist(fake_home: Path) -> None:
    autostart.enable()
    autostart.disable()
    assert autostart.is_enabled() is False


def test_macos_disable_when_never_enabled_does_not_raise(fake_home: Path) -> None:
    autostart.disable()  # no plist exists yet — must be a no-op, not an error


# --- Windows: a fake in-memory winreg, injected via sys.modules --------------


class _FakeWinReg(types.ModuleType):
    HKEY_CURRENT_USER = object()
    KEY_ALL_ACCESS = object()
    REG_SZ = object()

    def __init__(self) -> None:
        super().__init__("winreg")
        self.values: dict[str, str] = {}

    def OpenKey(self, hkey: object, path: str, reserved: int, access: object) -> _FakeWinReg:
        return self

    def __enter__(self) -> _FakeWinReg:
        return self

    def __exit__(self, *exc_info: object) -> None:
        pass

    def QueryValueEx(self, key: Any, name: str) -> tuple[str, object]:
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(
        self, key: Any, name: str, reserved: int, value_type: object, value: str
    ) -> None:
        self.values[name] = value

    def DeleteValue(self, key: Any, name: str) -> None:
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


@pytest.fixture
def fake_winreg(monkeypatch: pytest.MonkeyPatch) -> _FakeWinReg:
    monkeypatch.setattr(sys, "platform", "win32")
    fake = _FakeWinReg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    return fake


def test_windows_enable_sets_the_run_key(fake_winreg: _FakeWinReg) -> None:
    assert autostart.is_enabled() is False
    autostart.enable()
    assert autostart.is_enabled() is True
    assert "deskai" in fake_winreg.values


def test_windows_disable_removes_the_run_key(fake_winreg: _FakeWinReg) -> None:
    autostart.enable()
    autostart.disable()
    assert autostart.is_enabled() is False


def test_windows_disable_when_never_enabled_does_not_raise(fake_winreg: _FakeWinReg) -> None:
    autostart.disable()
