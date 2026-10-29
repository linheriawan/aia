"""Chat window (ARCHITECTURE.md §4): sessions, model picker, workspace folder
chips, and streamed send/stop with tool-call cards."""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QColor, QKeyEvent, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from deskai.core.errors import ProviderError
from deskai.core.events import Done, Error, TextDelta, ToolCallFinished, ToolCallStarted
from deskai.core.messages import Message, Role, TextPart
from deskai.core.session import ChatSession
from deskai.mcp_client.manager import McpManager
from deskai.ocr.engine import OcrEngine
from deskai.providers.base import ModelInfo, Provider
from deskai.skills.manager import SkillsManager
from deskai.storage import repositories
from deskai.tools.base import ConfirmDecision, RiskLevel
from deskai.tools.registry import ToolRegistry
from deskai.tools.sandbox import (
    Sandbox,
    WorkspaceFolder,
    effective_deny_list,
    matches_local_only_rule,
)
from deskai.ui.confirm_dialog import ConfirmWriteDialog

_SESSION_ROLE = int(Qt.ItemDataRole.UserRole)

_EMPTY_MODEL = ModelInfo(
    id="", display_name="", supports_tools=False, supports_images=False, context_tokens=0
)


def _model_info_from_row(row: repositories.ModelRow) -> ModelInfo:
    return ModelInfo(
        id=row.model_id,
        display_name=row.display_name,
        supports_tools=row.supports_tools,
        supports_images=row.supports_images,
        context_tokens=row.context_tokens,
    )


def _is_local_provider_row(row: repositories.ProviderRow | None) -> bool:
    return bool(row is not None and row.config.get("is_local"))


def _clear_layout(layout: QLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item is None:
            continue
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()


def _build_sandbox(conn: sqlite3.Connection, session_id: str) -> Sandbox:
    folders = [
        WorkspaceFolder(Path(row.path), read_only=row.read_only)
        for row in repositories.list_session_folders(conn, session_id)
    ]
    return Sandbox(folders, deny_list=effective_deny_list(conn))


class _ChatInput(QPlainTextEdit):
    """Return sends; Shift+Return inserts a newline (the reverse of QPlainTextEdit's default)."""

    send_requested = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        is_return = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if is_return and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.send_requested.emit()
            return
        super().keyPressEvent(event)


class ChatWindow(QMainWindow):
    def __init__(
        self,
        conn: sqlite3.Connection,
        providers: dict[str, Provider],
        registry: ToolRegistry,
        ocr: OcrEngine,
        skills_manager: SkillsManager | None = None,
        mcp_manager: McpManager | None = None,
    ) -> None:
        super().__init__()
        self._conn = conn
        self._providers = providers
        self._registry = registry
        self._ocr = ocr
        self._skills_manager = skills_manager
        self._mcp_manager = mcp_manager
        self._active_session: ChatSession | None = None
        self._pending_task: asyncio.Task[None] | None = None
        self._assistant_line_open = False
        self._awaiting_first_event = False

        self.setWindowTitle("deskai — Chat")
        self.resize(1000, 700)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setContentsMargins(0, 0, 0, 0)
        self._splitter.setHandleWidth(1)

        self._sidebar = QWidget()
        sidebar_layout = QVBoxLayout(self._sidebar)
        sidebar_layout.setContentsMargins(4, 4, 4, 4)
        sidebar_layout.setSpacing(4)
        sidebar_header = QHBoxLayout()
        sidebar_header.addWidget(QLabel("Chat Session"))
        sidebar_header.addStretch(1)
        new_chat_button = QPushButton("New chat")
        new_chat_button.clicked.connect(self._on_new_chat_clicked)
        sidebar_header.addWidget(new_chat_button)
        sidebar_layout.addLayout(sidebar_header)

        sidebar_splitter = QSplitter(Qt.Orientation.Vertical)
        sidebar_splitter.setHandleWidth(1)

        self._session_list = QListWidget()
        self._session_list.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self._session_list.currentItemChanged.connect(self._on_session_selected)
        self._session_list.itemChanged.connect(self._on_session_renamed)
        sidebar_splitter.addWidget(self._session_list)

        workspace_panel = QFrame()
        workspace_panel.setFrameShape(QFrame.Shape.StyledPanel)
        workspace_layout = QVBoxLayout(workspace_panel)
        workspace_layout.setContentsMargins(6, 6, 6, 6)
        workspace_layout.setSpacing(4)

        workspace_header = QHBoxLayout()
        workspace_header.addWidget(QLabel("Workspace:"))
        workspace_header.addStretch(1)
        add_folder_button = QPushButton("+ Add folder")
        add_folder_button.clicked.connect(self._on_add_folder_clicked)
        workspace_header.addWidget(add_folder_button)
        workspace_layout.addLayout(workspace_header)

        self._folder_chips_layout = QVBoxLayout()
        self._folder_chips_layout.setSpacing(4)
        workspace_layout.addLayout(self._folder_chips_layout)
        workspace_layout.addStretch(1)

        sidebar_splitter.addWidget(workspace_panel)
        sidebar_splitter.setStretchFactor(0, 1)  # the session list gets most of the extra space
        sidebar_layout.addWidget(sidebar_splitter)

        self._splitter.addWidget(self._sidebar)

        chat_pane = QWidget()
        chat_layout = QVBoxLayout(chat_pane)
        chat_layout.setContentsMargins(4, 4, 4, 4)
        chat_layout.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        self._sidebar_toggle = QPushButton("◀")
        self._sidebar_toggle.setFixedSize(28, 22)
        self._sidebar_toggle.setToolTip("Show/hide the session list")
        self._sidebar_toggle.clicked.connect(self._toggle_sidebar)
        header.addWidget(self._sidebar_toggle)
        self._provider_box = QComboBox()
        self._provider_box.setMaximumHeight(22)
        self._provider_box.currentIndexChanged.connect(self._on_provider_selected)
        header.addWidget(QLabel("Provider:"))
        header.addWidget(self._provider_box)
        self._model_box = QComboBox()
        self._model_box.setMaximumHeight(22)
        self._model_box.currentIndexChanged.connect(self._on_model_selected)
        header.addWidget(QLabel("Model:"))
        header.addWidget(self._model_box)
        header.addStretch(1)
        chat_layout.addLayout(header)

        self._messages = QPlainTextEdit()
        self._messages.setReadOnly(True)
        self._messages.setPlaceholderText("Conversation will appear here.")
        chat_layout.addWidget(self._messages)

        input_height = 80
        input_margin = 6

        input_container = QFrame()
        input_container.setObjectName("chatInputContainer")
        container_bg = input_container.palette().color(input_container.backgroundRole())
        darker = QColor(container_bg).darker(135).name()
        input_container.setStyleSheet(
            f"QFrame#chatInputContainer {{ background-color: {darker}; border-radius: 8px; }}"
        )
        input_container.setMaximumHeight(input_height + input_margin * 2)
        input_layout = QHBoxLayout(input_container)
        input_layout.setContentsMargins(8, input_margin, 8, input_margin)
        input_layout.setSpacing(6)

        self._input = _ChatInput()
        self._input.setPlaceholderText(
            "Type a message… (Return to send, Shift+Return for a newline)"
        )
        self._input.setFixedHeight(input_height)
        self._input.setFrameShape(QFrame.Shape.NoFrame)
        self._input.setStyleSheet(f"background-color: {darker}; border: none;")
        self._input.send_requested.connect(self._on_send_clicked)
        input_layout.addWidget(self._input)

        self._send_stop_button = QPushButton("Send")
        self._send_stop_button.setEnabled(False)
        self._send_stop_button.clicked.connect(self._on_send_stop_clicked)
        input_layout.addWidget(self._send_stop_button, alignment=Qt.AlignmentFlag.AlignVCenter)

        chat_layout.addWidget(input_container)

        self._splitter.addWidget(chat_pane)
        self._splitter.setStretchFactor(1, 1)

        self.setCentralWidget(self._splitter)

        self.refresh_models()
        self.refresh_sessions()

    def _toggle_sidebar(self) -> None:
        visible = not self._sidebar.isVisible()
        self._sidebar.setVisible(visible)
        self._sidebar_toggle.setText("◀" if visible else "▶")

    # --- providers / models -------------------------------------------------

    def set_providers(self, providers: dict[str, Provider]) -> None:
        self._providers = providers
        if self._active_session is not None:
            provider = self._providers.get(self._active_session.provider_id)
            if provider is not None:
                self._active_session.provider = provider

    def refresh_models(self) -> None:
        """Repopulates the Provider list, and the Model list for whichever is selected."""
        self._provider_box.blockSignals(True)
        self._provider_box.clear()
        for row in repositories.list_providers(self._conn):
            if not row.enabled:
                continue
            label = row.display_name + (" (Local)" if _is_local_provider_row(row) else "")
            self._provider_box.addItem(label, row.id)
        self._provider_box.blockSignals(False)
        self._sync_provider_box_to_session()

    def _refresh_model_box_for_provider(self, provider_id: str | None) -> None:
        self._model_box.blockSignals(True)
        self._model_box.clear()
        if provider_id:
            for row in repositories.list_models(self._conn, provider_id):
                self._model_box.addItem(row.display_name, row.id)
        self._model_box.blockSignals(False)

    # --- sessions ------------------------------------------------------------

    def refresh_sessions(self) -> None:
        self._session_list.blockSignals(True)
        self._session_list.clear()
        for row in repositories.list_sessions(self._conn):
            item = QListWidgetItem(row.title)
            item.setData(_SESSION_ROLE, row.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self._session_list.addItem(item)
        self._session_list.blockSignals(False)
        if self._session_list.count() > 0:
            self._session_list.setCurrentRow(0)
        else:
            self._create_session()

    def _on_session_renamed(self, item: QListWidgetItem) -> None:
        session_id = item.data(_SESSION_ROLE)
        title = item.text().strip()
        if not title:
            item.setText("New chat")
            title = "New chat"
        repositories.set_session_title(self._conn, session_id, title)
        if self._active_session is not None and self._active_session.id == session_id:
            self._active_session.title = title

    def _on_new_chat_clicked(self) -> None:
        self._create_session()

    def _create_session(self) -> None:
        session_id = str(uuid.uuid4())
        all_models = repositories.list_all_models(self._conn)

        default_provider_id = repositories.get_setting(self._conn, "privacy.default_provider")
        preferred = [m for m in all_models if m.provider_id == default_provider_id]
        candidates = preferred or all_models
        default_model = candidates[0] if candidates else None

        provider_id = default_model.provider_id if default_model else None
        model_row_id = default_model.id if default_model else None
        repositories.create_session(self._conn, session_id, "New chat", provider_id, model_row_id)
        self.refresh_sessions()
        self.select_session(session_id)

    def select_session(self, session_id: str) -> None:
        for row in range(self._session_list.count()):
            item = self._session_list.item(row)
            if item is not None and item.data(_SESSION_ROLE) == session_id:
                self._session_list.setCurrentRow(row)
                return

    def _on_session_selected(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        if current is None:
            self._active_session = None
            self._messages.clear()
            self._rebuild_folder_chips()
            self._update_send_enabled()
            return

        session_id = current.data(_SESSION_ROLE)
        session_row = next(
            (s for s in repositories.list_sessions(self._conn) if s.id == session_id), None
        )
        if session_row is None:
            return

        model_row = (
            repositories.get_model(self._conn, session_row.model_row_id)
            if session_row.model_row_id
            else None
        )
        provider = self._providers.get(model_row.provider_id) if model_row is not None else None

        self._active_session = ChatSession(
            id=session_row.id,
            title=session_row.title,
            provider=provider,
            provider_id=model_row.provider_id if model_row is not None else "",
            model=_model_info_from_row(model_row) if model_row is not None else _EMPTY_MODEL,
            model_row_id=model_row.id if model_row is not None else "",
            conn=self._conn,
            sandbox=_build_sandbox(self._conn, session_id),
            registry=self._registry,
            ocr=self._ocr,
            history=repositories.list_messages(self._conn, session_id),
            skills_manager=self._skills_manager,
            mcp_manager=self._mcp_manager,
        )

        self._render_history()
        self._sync_provider_box_to_session()
        self._rebuild_folder_chips()
        self._update_send_enabled()

    def _render_history(self) -> None:
        self._messages.clear()
        if self._active_session is None:
            return
        for message in self._active_session.history:
            text = "".join(getattr(part, "text", "") for part in message.parts)
            if not text:
                continue
            speaker = "You" if message.role.value == "user" else "deskai"
            self._messages.appendPlainText(f"{speaker}: {text}")

    def _sync_provider_box_to_session(self) -> None:
        provider_id = self._active_session.provider_id if self._active_session else None
        index = self._provider_box.findData(provider_id) if provider_id else -1
        self._provider_box.blockSignals(True)
        self._provider_box.setCurrentIndex(index)
        self._provider_box.blockSignals(False)
        self._refresh_model_box_for_provider(provider_id)
        self._sync_model_box_to_session()

    def _sync_model_box_to_session(self) -> None:
        if self._active_session is None or not self._active_session.model_row_id:
            self._model_box.setCurrentIndex(-1)
            return
        index = self._model_box.findData(self._active_session.model_row_id)
        self._model_box.blockSignals(True)
        self._model_box.setCurrentIndex(index)
        self._model_box.blockSignals(False)

    def _on_provider_selected(self, index: int) -> None:
        """The user explicitly switched providers — pick that provider's first model for them."""
        if self._active_session is None or index < 0:
            return
        provider_id = str(self._provider_box.itemData(index))
        self._refresh_model_box_for_provider(provider_id)
        if self._model_box.count() > 0:
            self._model_box.setCurrentIndex(0)  # fires _on_model_selected, which persists it
        else:
            self._active_session.provider_id = provider_id
            self._active_session.provider = self._providers.get(provider_id)
            self._active_session.model_row_id = ""
            self._active_session.model = _EMPTY_MODEL
            repositories.set_session_model(self._conn, self._active_session.id, provider_id, "")
        self._update_send_enabled()

    def _on_model_selected(self, index: int) -> None:
        if self._active_session is None or index < 0:
            return
        model_row_id = self._model_box.itemData(index)
        model_row = repositories.get_model(self._conn, model_row_id)
        if model_row is None:
            return
        repositories.set_session_model(
            self._conn, self._active_session.id, model_row.provider_id, model_row.id
        )
        self._active_session.provider_id = model_row.provider_id
        self._active_session.model = _model_info_from_row(model_row)
        self._active_session.model_row_id = model_row.id
        self._active_session.provider = self._providers.get(model_row.provider_id)
        self._update_send_enabled()

    # --- workspace folder chips (ARCHITECTURE.md §4, §7) ----------------------

    def _rebuild_folder_chips(self) -> None:
        _clear_layout(self._folder_chips_layout)
        if self._active_session is None:
            return
        for row in repositories.list_session_folders(self._conn, self._active_session.id):
            self._folder_chips_layout.addWidget(self._make_folder_chip(row))

    def _make_folder_chip(self, row: repositories.SessionFolderRow) -> QWidget:
        chip = QFrame()
        chip.setFrameShape(QFrame.Shape.Box)
        chip.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(chip)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(6)

        layout.addWidget(QLabel(Path(row.path).name or row.path))

        read_only_box = QCheckBox("read-only")
        read_only_box.setChecked(row.read_only)
        read_only_box.toggled.connect(
            lambda checked, path=row.path: self._on_folder_read_only_toggled(path, checked)
        )
        layout.addWidget(read_only_box)

        remove_button = QPushButton("✕")
        remove_button.setFixedWidth(24)
        remove_button.clicked.connect(
            lambda _checked=False, path=row.path: self._on_remove_folder_clicked(path)
        )
        layout.addWidget(remove_button)

        return chip

    def _on_add_folder_clicked(self) -> None:
        if self._active_session is None:
            return
        chosen = QFileDialog.getExistingDirectory(self, "Add workspace folder")
        if not chosen:
            return
        repositories.add_session_folder(self._conn, self._active_session.id, chosen)
        self._note_workspace_changed(f"added {chosen}")
        self._refresh_active_sandbox()

        if matches_local_only_rule(self._conn, chosen):
            self._enforce_local_only_provider()

    def _enforce_local_only_provider(self) -> None:
        """Forces the session onto a local provider (ARCHITECTURE.md §7) when a folder
        requiring one is attached — the privacy badge is always just a readout of
        whichever provider is selected, so enforcement means actually switching it."""
        if self._active_session is None:
            return
        current_row = repositories.get_provider(self._conn, self._active_session.provider_id)
        if _is_local_provider_row(current_row):
            return

        local_provider_id = next(
            (
                row.id
                for row in repositories.list_providers(self._conn)
                if row.enabled and _is_local_provider_row(row)
            ),
            None,
        )
        if local_provider_id is None:
            self._messages.appendPlainText(
                "[workspace changed: this folder requires a local provider, but none is "
                'configured — add one in Settings → Providers and check "Treat as local"]'
            )
            return

        index = self._provider_box.findData(local_provider_id)
        if index >= 0:
            self._provider_box.setCurrentIndex(index)  # fires _on_provider_selected
        self._messages.appendPlainText(
            "[workspace changed: switched to a local provider — this folder matches a "
            "Local-only rule in Settings → Privacy]"
        )

    def _on_remove_folder_clicked(self, path: str) -> None:
        if self._active_session is None:
            return
        repositories.remove_session_folder(self._conn, self._active_session.id, path)
        self._note_workspace_changed(f"removed {path}")
        self._refresh_active_sandbox()

    def _on_folder_read_only_toggled(self, path: str, read_only: bool) -> None:
        if self._active_session is None:
            return
        repositories.set_session_folder_read_only(
            self._conn, self._active_session.id, path, read_only
        )
        self._refresh_active_sandbox()

    def _refresh_active_sandbox(self) -> None:
        if self._active_session is None:
            return
        self._active_session.sandbox = _build_sandbox(self._conn, self._active_session.id)
        self._rebuild_folder_chips()

    def _note_workspace_changed(self, what: str) -> None:
        """Changing folders takes effect next turn; log a visible note (ARCHITECTURE.md §7)."""
        if self._active_session is None:
            return
        note = Message(Role.SYSTEM, [TextPart(f"Workspace changed: {what}")])
        self._active_session.history.append(note)
        repositories.append_message(self._conn, self._active_session.id, note)
        self._messages.appendPlainText(f"[workspace changed: {what}]")

    # --- sending --------------------------------------------------------------

    def _update_send_enabled(self) -> None:
        if self._pending_task is not None:
            self._send_stop_button.setText("Stop")
            self._send_stop_button.setEnabled(True)
            return
        self._send_stop_button.setText("Send")
        ready = (
            self._active_session is not None
            and self._active_session.provider is not None
            and bool(self._active_session.model_row_id)
        )
        self._send_stop_button.setEnabled(bool(ready))

    def _on_send_stop_clicked(self) -> None:
        if self._pending_task is not None:
            self._pending_task.cancel()
            return
        self._on_send_clicked()

    def _on_send_clicked(self) -> None:
        if self._pending_task is not None:
            return  # already streaming — Return in the input must not double-send
        if self._active_session is None or self._active_session.provider is None:
            return
        if not self._active_session.model_row_id:
            return
        text = self._input.toPlainText().strip()
        if not text:
            return
        self._input.clear()
        self._messages.appendPlainText(f"You: {text}")
        self._messages.appendPlainText("deskai: …")
        self._awaiting_first_event = True
        self._assistant_line_open = False

        self._pending_task = asyncio.ensure_future(self._stream_reply(text))
        self._update_send_enabled()

    async def _stream_reply(self, text: str) -> None:
        session = self._active_session
        assert session is not None
        try:
            async for event in session.send_message(text, confirm=self._confirm_write):
                self._clear_waiting_placeholder()
                if isinstance(event, TextDelta):
                    self._ensure_assistant_line()
                    self._append_to_last_line(event.text)
                elif isinstance(event, ToolCallStarted):
                    args_preview = ", ".join(f"{k}={v!r}" for k, v in event.arguments.items())
                    self._messages.appendPlainText(f"  ⚙ {event.name}({args_preview})")
                    self._assistant_line_open = False
                elif isinstance(event, ToolCallFinished):
                    marker = "✗" if event.is_error else "✓"
                    self._append_to_last_line(
                        f"  {marker} {event.result_summary} ({event.duration_ms}ms)"
                    )
                elif isinstance(event, Error):
                    self._messages.appendPlainText(f"[error: {event.message}]")
                    self._assistant_line_open = False
                elif isinstance(event, Done):
                    pass
        except asyncio.CancelledError:
            self._clear_waiting_placeholder()
            self._messages.appendPlainText("[stopped]")
        except ProviderError as exc:
            self._clear_waiting_placeholder()
            self._messages.appendPlainText(f"[error: {exc}]")
        finally:
            self._messages.appendPlainText("")
            self._pending_task = None
            self._update_send_enabled()

    def _clear_waiting_placeholder(self) -> None:
        """Removes the "deskai: …" placeholder shown while waiting for the first event."""
        if not self._awaiting_first_event:
            return
        self._awaiting_first_event = False
        cursor = self._messages.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        cursor.deletePreviousChar()  # also remove the newline that started this line

    def _confirm_write(
        self, tool_name: str, arguments: dict[str, object], _risk: RiskLevel, preview_text: str
    ) -> ConfirmDecision:
        return ConfirmWriteDialog.ask(tool_name, arguments, preview_text)

    def _ensure_assistant_line(self) -> None:
        if not self._assistant_line_open:
            self._messages.appendPlainText("deskai: ")
            self._assistant_line_open = True

    def _append_to_last_line(self, text: str) -> None:
        cursor = self._messages.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self._messages.setTextCursor(cursor)

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.hide()
