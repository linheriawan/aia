"""Optional global hotkey to show/hide the Chat window (ARCHITECTURE.md §4).

Qt's own `QShortcut` only fires while one of the app's own windows has
focus; a *global* hotkey needs an OS-level keyboard hook, which `pynput`
provides across Windows/macOS/Linux. The hook runs on its own thread, so the
callback crossing back onto the Qt thread (`on_activate`) must be something
the caller can safely invoke from another thread — e.g. a `Signal.emit`.

macOS requires the user to grant Accessibility permission for this to work
at all; Linux under Wayland (rather than X11) generally can't support a
global hook. Both show up as the listener failing to start, not a crash —
callers should treat `start()` raising as "hotkey unavailable" and carry on
without one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtGui import QKeySequence

logger = logging.getLogger(__name__)

_QT_MODIFIER_TOKENS = {
    "ctrl": "<ctrl>",
    "control": "<ctrl>",
    "alt": "<alt>",
    "option": "<alt>",
    "shift": "<shift>",
    "meta": "<cmd>",
    "cmd": "<cmd>",
    "command": "<cmd>",
    "super": "<cmd>",
}

# pynput's `Key` enum uses its own names for keys that aren't a single
# printable character; Qt's spelling differs for a few of them.
_SPECIAL_KEY_ALIASES = {
    "return": "enter",
    "escape": "esc",
    "pageup": "page_up",
    "pagedown": "page_down",
    "capslock": "caps_lock",
    "numlock": "num_lock",
    "scrolllock": "scroll_lock",
    "printscreen": "print_screen",
}


class HotkeyError(Exception):
    """The hotkey string couldn't be parsed, or the OS-level hook couldn't start."""


def parse_hotkey_string(qt_sequence: str) -> str:
    """Converts a Qt-style key sequence (as typed into Settings, e.g.
    "Ctrl+Alt+H") into pynput's `GlobalHotKeys` format (e.g. "<ctrl>+<alt>+h").
    """
    text = qt_sequence.strip()
    if not text:
        raise HotkeyError("empty hotkey")

    parts = [p.strip() for p in text.split("+") if p.strip()]
    if not parts:
        raise HotkeyError(f"could not parse hotkey: {qt_sequence!r}")

    *modifiers, key = parts
    tokens = []
    for modifier in modifiers:
        token = _QT_MODIFIER_TOKENS.get(modifier.lower())
        if token is None:
            raise HotkeyError(f"unknown modifier {modifier!r} in {qt_sequence!r}")
        tokens.append(token)

    tokens.append(_key_token(key))
    return "+".join(tokens)


def _key_token(key: str) -> str:
    """A single printable character is written bare (pynput's `KeyCode`);
    anything else — F-keys, Space, Tab, arrows — names a `Key` enum member
    and must be wrapped in angle brackets. Anything this doesn't recognize
    is passed through as a best-effort guess; pynput itself is the one that
    ultimately validates it, when the listener actually starts."""
    normalized = key.lower()
    if len(normalized) == 1:
        return normalized
    normalized = _SPECIAL_KEY_ALIASES.get(normalized, normalized)
    return f"<{normalized}>"


def is_valid_hotkey_string(qt_sequence: str) -> bool:
    if QKeySequence(qt_sequence).isEmpty():
        return False
    try:
        parse_hotkey_string(qt_sequence)
    except HotkeyError:
        return False
    return True


class HotkeyListener:
    """Owns the OS-level hook for exactly one hotkey. `start`/`stop` are
    plain sync calls — pynput runs its own background thread internally."""

    def __init__(self) -> None:
        self._hook: object | None = None

    def start(self, qt_sequence: str, on_activate: Callable[[], None]) -> None:
        self.stop()
        hotkey = parse_hotkey_string(qt_sequence)
        try:
            from pynput import keyboard
        except ImportError as exc:
            raise HotkeyError("pynput is not installed") from exc

        try:
            hook = keyboard.GlobalHotKeys({hotkey: on_activate})
            hook.start()
        except Exception as exc:  # platform permission/backend failures vary widely
            raise HotkeyError(f"could not install the global hotkey hook: {exc}") from exc
        self._hook = hook

    def stop(self) -> None:
        if self._hook is not None:
            self._hook.stop()  # type: ignore[attr-defined]
            self._hook = None
