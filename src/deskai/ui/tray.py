"""The always-present tray icon and its menu (ARCHITECTURE.md §4)."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon


class TrayIcon(QSystemTrayIcon):
    open_chat_requested = Signal()
    usage_requested = Signal()
    settings_requested = Signal()
    quit_requested = Signal()
    # Emitted by the global hotkey's OS-level hook, which runs on its own
    # thread (`hotkey.py`) — safe to `.emit()` cross-thread since this
    # object's own affinity is the Qt main thread, so Qt queues the delivery.
    hotkey_triggered = Signal()

    def __init__(self) -> None:
        icon = QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        super().__init__(icon)
        self.setToolTip("deskai")

        # "New chat" isn't its own menu item: the chat window already has that
        # button, and from the tray it would just do the same thing as "Open
        # chat" (bring the window to front) without a session picker visible
        # yet to act on.
        menu = QMenu()
        menu.addAction("Open chat", self.open_chat_requested.emit)
        menu.addAction("Usage", self.usage_requested.emit)
        menu.addAction("Settings", self.settings_requested.emit)
        menu.addSeparator()
        menu.addAction("Quit", self.quit_requested.emit)
        self.setContextMenu(menu)

        self.activated.connect(self._on_activated)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.open_chat_requested.emit()
