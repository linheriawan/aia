"""AppController: owns windows, the DB connection, and app lifecycle.

Also implements the single-instance guard (ARCHITECTURE.md §4): a second
launch connects to the first instance's QLocalServer and asks it to raise its
chat window, instead of starting a second process.
"""

from __future__ import annotations

import logging
import sqlite3

from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from deskai import paths
from deskai.hotkey import HotkeyError, HotkeyListener
from deskai.mcp_client.manager import McpManager
from deskai.ocr.engine import OcrEngine
from deskai.providers import bedrock, openai_compat
from deskai.providers.base import Provider
from deskai.skills.manager import SkillsManager
from deskai.storage import db as storage_db
from deskai.storage import repositories
from deskai.tools.registry import ToolRegistry, build_default_registry
from deskai.tools.skill_tools import LoadSkillTool
from deskai.ui.chat_window import ChatWindow
from deskai.ui.settings_window import SettingsWindow
from deskai.ui.tray import TrayIcon
from deskai.ui.usage_window import UsageWindow

logger = logging.getLogger(__name__)

_SERVER_NAME = "deskai-single-instance"
_ACTIVATE_MESSAGE = b"activate"


class AppController:
    def __init__(self, app: QApplication) -> None:
        self._app = app
        self._server: QLocalServer | None = None
        self._conn: sqlite3.Connection | None = None
        self._registry: ToolRegistry = build_default_registry()
        self._ocr_engine: OcrEngine | None = None
        self._skills_manager: SkillsManager | None = None
        self._mcp_manager: McpManager | None = None
        self._hotkey_listener = HotkeyListener()
        self.tray: TrayIcon | None = None
        self.chat_window: ChatWindow | None = None
        self.settings_window: SettingsWindow | None = None
        self.usage_window: UsageWindow | None = None

    def acquire_single_instance(self) -> bool:
        """Return False (and notify the running instance) if one is already up."""
        probe = QLocalSocket()
        probe.connectToServer(_SERVER_NAME)
        if probe.waitForConnected(200):
            probe.write(_ACTIVATE_MESSAGE)
            probe.waitForBytesWritten(200)
            probe.disconnectFromServer()
            return False

        QLocalServer.removeServer(_SERVER_NAME)
        server = QLocalServer()
        server.newConnection.connect(self._on_instance_connection)
        server.listen(_SERVER_NAME)
        self._server = server
        return True

    def _on_instance_connection(self) -> None:
        server = self._server
        if server is None:
            return
        socket = server.nextPendingConnection()
        if socket is not None:
            socket.readyRead.connect(socket.readAll)
            socket.disconnected.connect(socket.deleteLater)
        self.show_chat_window()

    def start(self) -> None:
        logger.info("Starting deskai")
        self._conn = storage_db.connect(paths.db_path())
        self._ocr_engine = OcrEngine(self._conn)
        self._skills_manager = SkillsManager(self._conn)
        self._registry.register(LoadSkillTool(self._skills_manager))
        self._mcp_manager = McpManager(self._conn)

        self.tray = TrayIcon()
        self.tray.open_chat_requested.connect(self.show_chat_window)
        self.tray.usage_requested.connect(self.show_usage_window)
        self.tray.settings_requested.connect(self.show_settings_window)
        self.tray.quit_requested.connect(self.quit)
        self.tray.hotkey_triggered.connect(self._on_hotkey_triggered)
        self.tray.show()

        self.chat_window = ChatWindow(
            self._conn,
            self._build_providers(),
            self._registry,
            self._ocr_engine,
            self._skills_manager,
            self._mcp_manager,
        )
        self.settings_window = SettingsWindow(self._conn, self._skills_manager, self._mcp_manager)
        self.settings_window.providers_changed.connect(self._on_providers_changed)
        self.settings_window.hotkey_changed.connect(self._reload_hotkey)
        self.usage_window = UsageWindow(self._conn)
        self.usage_window.open_session_requested.connect(self._on_open_session_requested)

        self._reload_hotkey()
        self.show_chat_window()

    def _reload_hotkey(self) -> None:
        assert self._conn is not None
        assert self.tray is not None
        self._hotkey_listener.stop()
        if repositories.get_setting(self._conn, "hotkey.enabled", "false") != "true":
            return
        sequence = repositories.get_setting(self._conn, "hotkey.sequence", "") or ""
        if not sequence:
            return
        try:
            self._hotkey_listener.start(sequence, self.tray.hotkey_triggered.emit)
        except HotkeyError as exc:
            logger.warning("Could not start global hotkey %r: %s", sequence, exc)

    def _on_hotkey_triggered(self) -> None:
        assert self.chat_window is not None
        if self.chat_window.isVisible() and self.chat_window.isActiveWindow():
            self.chat_window.hide()
        else:
            self.show_chat_window()

    def _on_open_session_requested(self, session_id: str) -> None:
        assert self.chat_window is not None
        self.chat_window.select_session(session_id)
        self.show_chat_window()

    def _build_providers(self) -> dict[str, Provider]:
        assert self._conn is not None
        providers: dict[str, Provider] = {}
        bedrock_provider = bedrock.load_from_db(self._conn)
        if bedrock_provider is not None:
            providers[bedrock.PROVIDER_ID] = bedrock_provider
        providers.update(openai_compat.load_all_from_db(self._conn))
        return providers

    def _on_providers_changed(self) -> None:
        assert self.chat_window is not None
        self.chat_window.set_providers(self._build_providers())
        self.chat_window.refresh_models()

    def show_chat_window(self) -> None:
        assert self.chat_window is not None
        self.chat_window.show()
        self.chat_window.raise_()
        self.chat_window.activateWindow()

    def show_settings_window(self) -> None:
        assert self.settings_window is not None
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()

    def show_usage_window(self) -> None:
        assert self.usage_window is not None
        self.usage_window.show()
        self.usage_window.raise_()
        self.usage_window.activateWindow()

    def quit(self) -> None:
        logger.info("Quitting deskai")
        self._hotkey_listener.stop()
        if self._mcp_manager is not None:
            self._mcp_manager.stop_all()
            self._mcp_manager = None
        if self._ocr_engine is not None:
            self._ocr_engine.shutdown()
            self._ocr_engine = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._server is not None:
            self._server.close()
            self._server = None
        self._app.quit()
