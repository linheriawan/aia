"""Approve/deny WRITE+ tool calls, showing the diff or structured summary (ARCHITECTURE.md §8.1)."""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout

from deskai.tools.base import ConfirmDecision


class ConfirmWriteDialog(QDialog):
    def __init__(self, tool_name: str, arguments: dict[str, object], preview_text: str) -> None:
        super().__init__()
        self.setWindowTitle("Confirm action")
        self.decision = ConfirmDecision.DENY

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"deskai wants to run <b>{tool_name}</b>:"))

        args_label = QLabel(f"Arguments: {arguments!r}")
        args_label.setWordWrap(True)
        layout.addWidget(args_label)

        preview = QPlainTextEdit(preview_text)
        preview.setReadOnly(True)
        layout.addWidget(preview)

        buttons = QHBoxLayout()
        deny_button = QPushButton("Deny")
        deny_button.clicked.connect(self._deny)
        once_button = QPushButton("Allow once")
        once_button.setDefault(True)
        once_button.clicked.connect(self._allow_once)
        session_button = QPushButton("Allow for this session")
        session_button.clicked.connect(self._allow_session)
        buttons.addWidget(deny_button)
        buttons.addWidget(once_button)
        buttons.addWidget(session_button)
        layout.addLayout(buttons)

        self.resize(600, 400)

    def _deny(self) -> None:
        self.decision = ConfirmDecision.DENY
        self.accept()

    def _allow_once(self) -> None:
        self.decision = ConfirmDecision.ALLOW_ONCE
        self.accept()

    def _allow_session(self) -> None:
        self.decision = ConfirmDecision.ALLOW_SESSION
        self.accept()

    @staticmethod
    def ask(tool_name: str, arguments: dict[str, object], preview_text: str) -> ConfirmDecision:
        dialog = ConfirmWriteDialog(tool_name, arguments, preview_text)
        dialog.exec()
        return dialog.decision
