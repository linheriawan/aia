"""Tests for the hotkey string parsing (ARCHITECTURE.md §4) — pure logic, no
real OS-level keyboard hook installed (that needs Accessibility permission
on macOS and varies by platform, so it's exercised with a fake `GlobalHotKeys`
instead, never a live hook)."""

from __future__ import annotations

from typing import Any

import pytest

from deskai.hotkey import HotkeyError, HotkeyListener, is_valid_hotkey_string, parse_hotkey_string


@pytest.mark.parametrize(
    ("qt_sequence", "expected"),
    [
        ("Ctrl+Alt+D", "<ctrl>+<alt>+d"),
        ("Ctrl+Shift+H", "<ctrl>+<shift>+h"),
        ("Meta+Space", "<cmd>+<space>"),
        ("Cmd+Alt+D", "<cmd>+<alt>+d"),
        ("F9", "<f9>"),
        ("Ctrl+Return", "<ctrl>+<enter>"),
    ],
)
def test_parse_hotkey_string_translates_qt_modifiers(qt_sequence: str, expected: str) -> None:
    assert parse_hotkey_string(qt_sequence) == expected


@pytest.mark.parametrize("bad", ["", "   ", "Ctrl+Unknown+D"])
def test_parse_hotkey_string_rejects_garbage(bad: str) -> None:
    with pytest.raises(HotkeyError):
        parse_hotkey_string(bad)


def test_is_valid_hotkey_string_true_for_well_formed_sequence() -> None:
    assert is_valid_hotkey_string("Ctrl+Alt+D") is True


def test_is_valid_hotkey_string_false_for_empty() -> None:
    assert is_valid_hotkey_string("") is False


class _FakeGlobalHotKeys:
    started: list[dict[str, Any]] = []
    stopped = 0

    def __init__(self, mapping: dict[str, Any]) -> None:
        self.mapping = mapping

    def start(self) -> None:
        _FakeGlobalHotKeys.started.append(self.mapping)

    def stop(self) -> None:
        _FakeGlobalHotKeys.stopped += 1


def test_hotkey_listener_starts_and_stops_the_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    import pynput.keyboard

    _FakeGlobalHotKeys.started = []
    _FakeGlobalHotKeys.stopped = 0
    monkeypatch.setattr(pynput.keyboard, "GlobalHotKeys", _FakeGlobalHotKeys)

    listener = HotkeyListener()
    callback_calls = []
    listener.start("Ctrl+Alt+D", lambda: callback_calls.append(1))

    assert len(_FakeGlobalHotKeys.started) == 1
    assert "<ctrl>+<alt>+d" in _FakeGlobalHotKeys.started[0]

    listener.stop()
    assert _FakeGlobalHotKeys.stopped == 1


def test_hotkey_listener_start_restarts_a_running_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    import pynput.keyboard

    _FakeGlobalHotKeys.started = []
    _FakeGlobalHotKeys.stopped = 0
    monkeypatch.setattr(pynput.keyboard, "GlobalHotKeys", _FakeGlobalHotKeys)

    listener = HotkeyListener()
    listener.start("Ctrl+Alt+D", lambda: None)
    listener.start("Ctrl+Alt+E", lambda: None)

    assert _FakeGlobalHotKeys.stopped == 1  # the first hook was stopped before the second started
    assert len(_FakeGlobalHotKeys.started) == 2


def test_hotkey_listener_wraps_backend_failure_as_hotkey_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pynput.keyboard

    class _ExplodingHotKeys:
        def __init__(self, mapping: dict[str, Any]) -> None:
            raise RuntimeError("no display available")

    monkeypatch.setattr(pynput.keyboard, "GlobalHotKeys", _ExplodingHotKeys)

    listener = HotkeyListener()
    with pytest.raises(HotkeyError):
        listener.start("Ctrl+Alt+D", lambda: None)
