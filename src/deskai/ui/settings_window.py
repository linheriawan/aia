"""Settings window (ARCHITECTURE.md §4): General, Providers (Bedrock/Cloudflare/
Local), Privacy, and Pricing are real; Tools/Skills/MCP still fill in later phases."""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from importlib.metadata import PackageNotFoundError, version
from shlex import split as shlex_split

from PySide6.QtCore import Signal
from PySide6.QtGui import QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QKeySequenceEdit,
    QLabel,
    QLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from deskai import autostart, paths
from deskai import hotkey as hotkey_lib
from deskai.core.errors import ProviderError
from deskai.core.session import DEFAULT_SYSTEM_PROMPT
from deskai.mcp_client.manager import (
    McpError,
    McpManager,
    McpServerConfig,
    config_from_row,
    config_to_dict,
    probe_server,
)
from deskai.providers import bedrock, openai_compat
from deskai.skills.manager import SkillsManager
from deskai.storage import repositories, secrets
from deskai.usage import pricing

_PLACEHOLDER_TABS = ("Tools",)


def _app_version() -> str:
    """Single source of truth is pyproject.toml's `[project] version`
    (CLAUDE.md: never hardcode); package metadata is how that reaches a
    running, installed app (ARCHITECTURE.md §14)."""
    try:
        return version("deskai")
    except PackageNotFoundError:
        return "unknown"


_MODEL_COLUMNS = ("Model ID", "Display name", "Tools", "Images", "Context tokens")
_PRICE_COLUMNS = ("Provider", "Model ID", "Input / 1M tok", "Output / 1M tok", "Unit")


class SettingsWindow(QTabWidget):
    providers_changed = Signal()
    hotkey_changed = Signal()

    def __init__(
        self,
        conn: sqlite3.Connection,
        skills_manager: SkillsManager | None = None,
        mcp_manager: McpManager | None = None,
    ) -> None:
        super().__init__()
        self._conn = conn
        self.setWindowTitle("deskai — Settings")
        self.resize(760, 600)

        self.addTab(self._build_general_tab(), "General")

        providers_tabs = QTabWidget()
        for tab, label in (
            (_BedrockProviderTab(conn), "Bedrock"),
            (_CustomProvidersTab(conn), "Custom (OpenAI-compatible)"),
        ):
            tab.saved.connect(self.providers_changed.emit)
            providers_tabs.addTab(tab, label)
        self.addTab(providers_tabs, "Providers")

        self.addTab(_PrivacyTab(conn), "Privacy")
        self.addTab(_PricingTab(conn), "Pricing")
        if skills_manager is not None:
            self.addTab(_SkillsTab(conn, skills_manager), "Skills")
        else:
            self.addTab(QWidget(), "Skills")
        if mcp_manager is not None:
            self.addTab(_McpTab(conn, mcp_manager), "MCP")
        else:
            self.addTab(QWidget(), "MCP")

        for name in _PLACEHOLDER_TABS:
            self.addTab(QWidget(), name)

    def _build_general_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        outer.addLayout(form)

        current_theme = repositories.get_setting(self._conn, "theme", "System") or "System"
        theme_box = QComboBox()
        theme_box.addItems(["System", "Light", "Dark"])
        theme_box.setCurrentText(current_theme)
        theme_box.currentTextChanged.connect(
            lambda value: repositories.set_setting(self._conn, "theme", value)
        )
        form.addRow("Theme", theme_box)
        form.addRow("Version", QLabel(_app_version()))
        form.addRow("Data location", QLabel(str(paths.data_dir())))

        outer.addWidget(QLabel("System prompt — sent to the model ahead of every message:"))
        current_prompt = (
            repositories.get_setting(self._conn, "chat.system_prompt", DEFAULT_SYSTEM_PROMPT)
            or DEFAULT_SYSTEM_PROMPT
        )
        prompt_edit = QPlainTextEdit(current_prompt)
        outer.addWidget(prompt_edit)

        prompt_row = QHBoxLayout()
        save_button = QPushButton("Save")
        save_button.clicked.connect(
            lambda: repositories.set_setting(
                self._conn, "chat.system_prompt", prompt_edit.toPlainText()
            )
        )
        reset_button = QPushButton("Reset to default")
        reset_button.clicked.connect(lambda: prompt_edit.setPlainText(DEFAULT_SYSTEM_PROMPT))
        prompt_row.addWidget(save_button)
        prompt_row.addWidget(reset_button)
        prompt_row.addStretch(1)
        outer.addLayout(prompt_row)

        outer.addWidget(QLabel("Global hotkey — show/hide the Chat window from anywhere:"))
        hotkey_row = QHBoxLayout()
        self._hotkey_enabled = QCheckBox("Enabled")
        self._hotkey_enabled.setChecked(
            repositories.get_setting(self._conn, "hotkey.enabled", "false") == "true"
        )
        hotkey_row.addWidget(self._hotkey_enabled)
        self._hotkey_sequence = QKeySequenceEdit(
            QKeySequence(
                repositories.get_setting(self._conn, "hotkey.sequence", "Ctrl+Alt+D")
                or "Ctrl+Alt+D"
            )
        )
        hotkey_row.addWidget(self._hotkey_sequence)
        hotkey_save = QPushButton("Save")
        hotkey_save.clicked.connect(self._on_save_hotkey_clicked)
        hotkey_row.addWidget(hotkey_save)
        self._hotkey_status = QLabel("")
        hotkey_row.addWidget(self._hotkey_status)
        hotkey_row.addStretch(1)
        outer.addLayout(hotkey_row)

        autostart_row = QHBoxLayout()
        self._launch_at_login = QCheckBox("Launch at login")
        self._launch_at_login.setChecked(autostart.is_enabled())
        self._launch_at_login.toggled.connect(self._on_launch_at_login_toggled)
        autostart_row.addWidget(self._launch_at_login)
        self._autostart_status = QLabel("")
        autostart_row.addWidget(self._autostart_status)
        autostart_row.addStretch(1)
        outer.addLayout(autostart_row)
        outer.addStretch(1)

        return tab

    def _on_save_hotkey_clicked(self) -> None:
        sequence_text = self._hotkey_sequence.keySequence().toString()
        if self._hotkey_enabled.isChecked() and not hotkey_lib.is_valid_hotkey_string(
            sequence_text
        ):
            self._hotkey_status.setText("Invalid or unsupported hotkey.")
            return
        repositories.set_setting(
            self._conn, "hotkey.enabled", "true" if self._hotkey_enabled.isChecked() else "false"
        )
        repositories.set_setting(self._conn, "hotkey.sequence", sequence_text)
        self._hotkey_status.setText("Saved.")
        self.hotkey_changed.emit()

    def _on_launch_at_login_toggled(self, checked: bool) -> None:
        try:
            if checked:
                autostart.enable()
            else:
                autostart.disable()
            self._autostart_status.setText("")
        except autostart.AutostartError as exc:
            self._autostart_status.setText(f"Could not update: {exc}")
            self._launch_at_login.blockSignals(True)
            self._launch_at_login.setChecked(not checked)
            self._launch_at_login.blockSignals(False)

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.hide()


class _ModelTableWidget(QWidget):
    """The model-entries editor shared by every provider panel."""

    def __init__(self, provider_id: str) -> None:
        super().__init__()
        self._provider_id = provider_id

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header_row = QHBoxLayout()
        header_row.addWidget(QLabel("Models"))
        header_row.addStretch(1)
        add_button = QPushButton("Add model")
        add_button.clicked.connect(lambda: self.add_row())
        remove_button = QPushButton("Remove selected")
        remove_button.clicked.connect(self._remove_selected)
        header_row.addWidget(add_button)
        header_row.addWidget(remove_button)
        layout.addLayout(header_row)

        self._table = QTableWidget(0, len(_MODEL_COLUMNS))
        self._table.setHorizontalHeaderLabels(_MODEL_COLUMNS)
        header = self._table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table)

    def set_provider_id(self, provider_id: str) -> None:
        self._provider_id = provider_id

    def clear_rows(self) -> None:
        self._table.setRowCount(0)

    def add_row(self, model: repositories.ModelRow | None = None) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        values = (
            (
                model.model_id,
                model.display_name,
                model.supports_tools,
                model.supports_images,
                str(model.context_tokens),
            )
            if model is not None
            else ("", "", False, False, "0")
        )
        model_id, display_name, supports_tools, supports_images, context_tokens = values
        self._table.setItem(row, 0, QTableWidgetItem(model_id))
        self._table.setItem(row, 1, QTableWidgetItem(display_name))

        tools_check = QCheckBox()
        tools_check.setChecked(bool(supports_tools))
        self._table.setCellWidget(row, 2, tools_check)

        images_check = QCheckBox()
        images_check.setChecked(bool(supports_images))
        self._table.setCellWidget(row, 3, images_check)

        self._table.setItem(row, 4, QTableWidgetItem(context_tokens))

    def _remove_selected(self) -> None:
        for index in sorted({i.row() for i in self._table.selectedIndexes()}, reverse=True):
            self._table.removeRow(index)

    def load(self, conn: sqlite3.Connection) -> None:
        self.clear_rows()
        for model in repositories.list_models(conn, self._provider_id):
            self.add_row(model)

    def collect(self) -> list[repositories.ModelRow]:
        models: list[repositories.ModelRow] = []
        for row in range(self._table.rowCount()):
            model_id_item = self._table.item(row, 0)
            display_name_item = self._table.item(row, 1)
            context_item = self._table.item(row, 4)
            tools_check = self._table.cellWidget(row, 2)
            images_check = self._table.cellWidget(row, 3)
            if model_id_item is None or not model_id_item.text().strip():
                continue
            try:
                context_tokens = int(context_item.text()) if context_item is not None else 0
            except ValueError:
                context_tokens = 0
            models.append(
                repositories.ModelRow(
                    id=str(uuid.uuid4()),
                    provider_id=self._provider_id,
                    model_id=model_id_item.text().strip(),
                    display_name=(display_name_item.text().strip() if display_name_item else "")
                    or model_id_item.text().strip(),
                    supports_tools=isinstance(tools_check, QCheckBox) and tools_check.isChecked(),
                    supports_images=isinstance(images_check, QCheckBox)
                    and images_check.isChecked(),
                    context_tokens=context_tokens,
                )
            )
        return models


class _ProviderTabBase(QWidget):
    """Shared scaffolding for a provider panel: enabled/display-name, a model
    table, and Save/Test buttons. Subclasses add their own fields to `self._form`
    and implement the four methods below."""

    saved = Signal()

    def __init__(
        self, conn: sqlite3.Connection, provider_id: str, kind: str, default_display_name: str
    ) -> None:
        super().__init__()
        self._conn = conn
        self._provider_id = provider_id
        self._kind = kind

        self._layout = QVBoxLayout(self)

        top_row = QHBoxLayout()
        self._enabled = QCheckBox("Enabled")
        self._enabled.setChecked(True)
        top_row.addWidget(self._enabled)
        top_row.addStretch(1)
        self._status_label = QLabel("")
        top_row.addWidget(self._status_label)
        save_button = QPushButton("Save")
        save_button.clicked.connect(self._on_save_clicked)
        test_button = QPushButton("Test connection")
        test_button.clicked.connect(self._on_test_clicked)
        top_row.addWidget(save_button)
        top_row.addWidget(test_button)
        self._layout.addLayout(top_row)

        self._form = QFormLayout()
        self._form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self._layout.addLayout(self._form)

        self._display_name = QLineEdit(default_display_name)
        self._form.addRow("Display name", self._display_name)

    def _finish_init(self) -> None:
        """Subclasses call this once, after adding their own fields to `self._form`."""
        self._models = _ModelTableWidget(self._provider_id)
        self._layout.addWidget(self._models)
        self._load()

    def _load(self) -> None:
        provider = repositories.get_provider(self._conn, self._provider_id)
        if provider is not None:
            self._enabled.setChecked(provider.enabled)
            self._display_name.setText(provider.display_name)
            self._load_config(provider.config)
        self._models.load(self._conn)

    def _load_config(self, config: dict[str, object]) -> None:
        raise NotImplementedError

    def _build_config(self) -> dict[str, object]:
        raise NotImplementedError

    def _save_secrets(self) -> None:
        raise NotImplementedError

    def _build_provider_for_test(self) -> object | None:
        raise NotImplementedError

    def _on_save_clicked(self) -> None:
        repositories.upsert_provider(
            self._conn,
            self._provider_id,
            kind=self._kind,
            display_name=self._display_name.text().strip() or self._provider_id,
            config=self._build_config(),
            enabled=self._enabled.isChecked(),
        )
        self._save_secrets()
        repositories.replace_models(self._conn, self._provider_id, self._models.collect())
        self._status_label.setText("Saved.")
        self.saved.emit()

    def _on_test_clicked(self) -> None:
        self._status_label.setText("Testing…")
        provider = self._build_provider_for_test()
        if provider is None:
            self._status_label.setText("Save the provider first.")
            return

        async def _run() -> None:
            try:
                await provider.test_connection()  # type: ignore[attr-defined]
            except ProviderError as exc:
                self._status_label.setText(f"Failed: {exc}")
            else:
                self._status_label.setText("Connection OK.")

        asyncio.ensure_future(_run())


class _BedrockProviderTab(_ProviderTabBase):
    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__(conn, bedrock.PROVIDER_ID, "bedrock", "Bedrock")

        self._region = QLineEdit("us-east-1")
        self._form.addRow("Region", self._region)

        self._auth_mode = QComboBox()
        self._auth_mode.addItems(["Access keys", "AWS profile"])
        self._auth_mode.currentTextChanged.connect(self._on_auth_mode_changed)
        self._form.addRow("Auth mode", self._auth_mode)

        self._access_key_id = QLineEdit()
        self._access_key_id.setPlaceholderText("Unchanged if left blank")
        self._form.addRow("Access key ID", self._access_key_id)

        self._secret_access_key = QLineEdit()
        self._secret_access_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._secret_access_key.setPlaceholderText("Unchanged if left blank")
        self._form.addRow("Secret access key", self._secret_access_key)

        self._profile_name = QLineEdit()
        self._form.addRow("AWS profile", self._profile_name)

        self._finish_init()
        self._on_auth_mode_changed(self._auth_mode.currentText())

    def _on_auth_mode_changed(self, mode: str) -> None:
        is_keys = mode == "Access keys"
        self._access_key_id.setEnabled(is_keys)
        self._secret_access_key.setEnabled(is_keys)
        self._profile_name.setEnabled(not is_keys)

    def _load_config(self, config: dict[str, object]) -> None:
        self._region.setText(str(config.get("region", "us-east-1")))
        auth_mode = config.get("auth_mode", "keys")
        self._auth_mode.setCurrentText("AWS profile" if auth_mode == "profile" else "Access keys")
        self._profile_name.setText(str(config.get("profile_name") or ""))

    def _build_config(self) -> dict[str, object]:
        auth_mode = "profile" if self._auth_mode.currentText() == "AWS profile" else "keys"
        return {
            "region": self._region.text().strip() or "us-east-1",
            "auth_mode": auth_mode,
            "profile_name": self._profile_name.text().strip() or None,
        }

    def _save_secrets(self) -> None:
        if self._auth_mode.currentText() != "Access keys":
            return
        access_key_id = self._access_key_id.text().strip()
        secret_access_key = self._secret_access_key.text().strip()
        if access_key_id:
            secrets.set_secret(f"provider:{self._provider_id}:access_key_id", access_key_id)
        if secret_access_key:
            secrets.set_secret(f"provider:{self._provider_id}:secret_access_key", secret_access_key)
        self._access_key_id.clear()
        self._secret_access_key.clear()

    def _build_provider_for_test(self) -> object | None:
        return bedrock.load_from_db(self._conn)


def _parse_header_lines(text: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        key = key.strip()
        if sep and key:
            headers[key] = value.strip()
    return headers


class _CustomProviderCard(QGroupBox):
    """One reusable editor for whichever custom OpenAI-compatible endpoint is

    selected above it — Cloudflare Workers AI, Ollama, LM Studio, OpenRouter,
    or anything else that speaks the same API. The user supplies the full
    base URL rather than provider-specific fields, so this one card covers
    any of them.
    """

    saved = Signal(str)
    removed = Signal()

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__("")
        self._conn = conn
        self._provider_id = ""

        layout = QVBoxLayout(self)

        top_row = QHBoxLayout()
        self._enabled = QCheckBox("Enabled")
        self._enabled.setChecked(True)
        top_row.addWidget(self._enabled)
        self._is_local = QCheckBox("Treat as local")
        self._is_local.setToolTip(
            "Required for this provider to be usable by a session with a Local-only "
            "folder attached (Settings → Privacy → Local-only folder rules)."
        )
        top_row.addWidget(self._is_local)
        top_row.addStretch(1)
        self._status_label = QLabel("")
        top_row.addWidget(self._status_label)
        save_button = QPushButton("Save")
        save_button.clicked.connect(self._on_save_clicked)
        test_button = QPushButton("Test connection")
        test_button.clicked.connect(self._on_test_clicked)
        remove_button = QPushButton("Remove")
        remove_button.clicked.connect(self._on_remove_clicked)
        top_row.addWidget(save_button)
        top_row.addWidget(test_button)
        top_row.addWidget(remove_button)
        layout.addLayout(top_row)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addLayout(form)

        self._display_name = QLineEdit("Custom provider")
        form.addRow("Display name", self._display_name)

        self._base_url = QLineEdit(openai_compat.DEFAULT_BASE_URL)
        self._base_url.setPlaceholderText(
            "e.g. https://api.cloudflare.com/client/v4/accounts/<account_id>/ai/v1"
        )
        form.addRow("Base URL", self._base_url)

        self._api_key = QLineEdit()
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key.setPlaceholderText("Optional — unchanged if left blank")
        form.addRow("API key", self._api_key)

        self._extra_headers = QPlainTextEdit()
        self._extra_headers.setPlaceholderText(
            "Optional, one per line — e.g. cf-aig-gateway-id: my-gateway"
        )
        self._extra_headers.setMaximumHeight(60)
        form.addRow("Extra headers", self._extra_headers)

        self._models = _ModelTableWidget("")
        layout.addWidget(self._models)

    def load_existing(self, provider_id: str) -> None:
        self._provider_id = provider_id
        self._models.set_provider_id(provider_id)
        self._status_label.setText("")

        row = repositories.get_provider(self._conn, provider_id)
        if row is None:
            return
        self.setTitle(row.display_name)
        self._enabled.setChecked(row.enabled)
        self._display_name.setText(row.display_name)
        self._base_url.setText(str(row.config.get("base_url", openai_compat.DEFAULT_BASE_URL)))
        self._is_local.setChecked(bool(row.config.get("is_local", False)))
        headers = row.config.get("extra_headers")
        self._extra_headers.setPlainText(
            "\n".join(f"{k}: {v}" for k, v in headers.items()) if isinstance(headers, dict) else ""
        )
        self._api_key.clear()
        self._models.load(self._conn)

    def start_new(self, provider_id: str) -> None:
        self._provider_id = provider_id
        self._models.set_provider_id(provider_id)
        self.setTitle("New custom provider")
        self._enabled.setChecked(True)
        self._display_name.setText("Custom provider")
        self._base_url.setText(openai_compat.DEFAULT_BASE_URL)
        self._is_local.setChecked(False)
        self._extra_headers.clear()
        self._api_key.clear()
        self._models.clear_rows()
        self._status_label.setText("")

    def _on_save_clicked(self) -> None:
        display_name = self._display_name.text().strip() or "Custom provider"
        config: dict[str, object] = {
            "base_url": self._base_url.text().strip() or openai_compat.DEFAULT_BASE_URL,
            "extra_headers": _parse_header_lines(self._extra_headers.toPlainText()),
            "is_local": self._is_local.isChecked(),
        }
        repositories.upsert_provider(
            self._conn,
            self._provider_id,
            kind=openai_compat.KIND,
            display_name=display_name,
            config=config,
            enabled=self._enabled.isChecked(),
        )
        api_key = self._api_key.text().strip()
        if api_key:
            secrets.set_secret(f"provider:{self._provider_id}:api_key", api_key)
            self._api_key.clear()
        repositories.replace_models(self._conn, self._provider_id, self._models.collect())
        self.setTitle(display_name)
        self._status_label.setText("Saved.")
        self.saved.emit(self._provider_id)

    def _on_test_clicked(self) -> None:
        self._status_label.setText("Testing…")
        provider = openai_compat.load_from_db(self._conn, self._provider_id)
        if provider is None:
            self._status_label.setText("Save the provider first.")
            return

        async def _run() -> None:
            try:
                await provider.test_connection()
            except ProviderError as exc:
                self._status_label.setText(f"Failed: {exc}")
            else:
                self._status_label.setText("Connection OK.")

        asyncio.ensure_future(_run())

    def _on_remove_clicked(self) -> None:
        if not self._provider_id:
            return
        repositories.delete_provider(self._conn, self._provider_id)
        secrets.delete_secret(f"provider:{self._provider_id}:api_key")
        self.removed.emit()


class _CustomProvidersTab(QWidget):
    """Custom OpenAI-compatible providers (ARCHITECTURE.md §5.4, §5.5): a selector

    picks which configured provider the single card below shows — Cloudflare
    Workers AI is just one of these, configured with its own URL, so any
    other OpenAI-compatible service can be added the same way.
    """

    saved = Signal()

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Any OpenAI-compatible endpoint — Cloudflare Workers AI, Ollama, LM Studio, "
                "OpenRouter, Together.ai, and so on."
            )
        )

        selector_row = QHBoxLayout()
        selector_row.addWidget(QLabel("Provider:"))
        self._selector = QComboBox()
        self._selector.currentIndexChanged.connect(self._on_selector_changed)
        selector_row.addWidget(self._selector)
        add_button = QPushButton("+ Add new")
        add_button.clicked.connect(self._on_add_new_clicked)
        selector_row.addWidget(add_button)
        selector_row.addStretch(1)
        layout.addLayout(selector_row)

        self._card = _CustomProviderCard(conn)
        self._card.saved.connect(self._on_card_saved)
        self._card.removed.connect(self._on_card_removed)
        layout.addWidget(self._card)
        layout.addStretch(1)

        self._reload_selector(select_first=True)

    def _reload_selector(self, *, select_first: bool = False, select_id: str | None = None) -> None:
        self._selector.blockSignals(True)
        self._selector.clear()
        for row in repositories.list_providers(self._conn):
            if row.kind == openai_compat.KIND:
                self._selector.addItem(row.display_name, row.id)
        self._selector.blockSignals(False)

        if select_id is not None and self._selector.findData(select_id) >= 0:
            self._selector.setCurrentIndex(self._selector.findData(select_id))
        elif select_first and self._selector.count() > 0:
            self._selector.setCurrentIndex(0)
        self._on_selector_changed(self._selector.currentIndex())

    def _on_selector_changed(self, index: int) -> None:
        if index < 0:
            self._card.start_new(str(uuid.uuid4()))
            return
        provider_id = str(self._selector.itemData(index))
        self._card.load_existing(provider_id)

    def _on_add_new_clicked(self) -> None:
        self._selector.blockSignals(True)
        self._selector.setCurrentIndex(-1)
        self._selector.blockSignals(False)
        self._card.start_new(str(uuid.uuid4()))

    def _on_card_saved(self, provider_id: str) -> None:
        self._reload_selector(select_id=provider_id)
        self.saved.emit()

    def _on_card_removed(self) -> None:
        self._reload_selector(select_first=True)
        self.saved.emit()


class _PrivacyTab(QWidget):
    """Default provider, the global deny-list, and Local-only folder rules (ARCHITECTURE.md §7)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn

        layout = QVBoxLayout(self)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self._default_provider = QComboBox()
        self._default_provider.addItem("(none)", "")
        for row in repositories.list_providers(conn):
            self._default_provider.addItem(row.display_name, row.id)
        current = repositories.get_setting(conn, "privacy.default_provider", "") or ""
        index = self._default_provider.findData(current)
        self._default_provider.setCurrentIndex(index if index >= 0 else 0)
        self._default_provider.currentIndexChanged.connect(self._on_default_provider_changed)
        form.addRow("Default provider for new sessions", self._default_provider)
        layout.addLayout(form)

        layout.addWidget(
            QLabel(
                "Deny-list: paths the AI can never access, in addition to the built-in "
                "~/.ssh, ~/.aws, ~/.gnupg and deskai's own data dir (one path per line)."
            )
        )
        self._deny_list = QPlainTextEdit(
            "\n".join(repositories.get_setting_list(conn, "privacy.extra_deny_paths"))
        )
        self._deny_list.setMaximumHeight(100)
        layout.addWidget(self._deny_list)

        layout.addWidget(
            QLabel(
                "Local-only folder rules: a session that attaches a folder under one of "
                "these path prefixes is switched to Local only automatically (one per line)."
            )
        )
        self._local_only_prefixes = QPlainTextEdit(
            "\n".join(repositories.get_setting_list(conn, "privacy.local_only_folder_prefixes"))
        )
        self._local_only_prefixes.setMaximumHeight(100)
        layout.addWidget(self._local_only_prefixes)

        save_button = QPushButton("Save")
        save_button.clicked.connect(self._on_save_clicked)
        self._status_label = QLabel("")
        button_row = QHBoxLayout()
        button_row.addWidget(save_button)
        button_row.addWidget(self._status_label)
        button_row.addStretch(1)
        layout.addLayout(button_row)
        layout.addStretch(1)

    def _on_default_provider_changed(self, index: int) -> None:
        provider_id = str(self._default_provider.itemData(index) or "")
        repositories.set_setting(self._conn, "privacy.default_provider", provider_id)

    def _on_save_clicked(self) -> None:
        deny_paths = [
            line.strip() for line in self._deny_list.toPlainText().splitlines() if line.strip()
        ]
        local_prefixes = [
            line.strip()
            for line in self._local_only_prefixes.toPlainText().splitlines()
            if line.strip()
        ]
        repositories.set_setting_list(self._conn, "privacy.extra_deny_paths", deny_paths)
        repositories.set_setting_list(
            self._conn, "privacy.local_only_folder_prefixes", local_prefixes
        )
        self._status_label.setText("Saved.")


class _PricingTab(QWidget):
    """The editable price table (ARCHITECTURE.md §11). No prices are hardcoded —
    "Load sample prices" only fills the table from the packaged sample file,
    for the user to review and edit before saving (CLAUDE.md hard rule)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Estimated on this device only — not authoritative billing. "
                "Check your provider's own billing console for real charges."
            )
        )

        self._table = QTableWidget(0, len(_PRICE_COLUMNS))
        self._table.setHorizontalHeaderLabels(_PRICE_COLUMNS)
        header = self._table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table)

        buttons = QHBoxLayout()
        add_button = QPushButton("Add row")
        add_button.clicked.connect(lambda: self._add_row())
        remove_button = QPushButton("Remove selected")
        remove_button.clicked.connect(self._remove_selected)
        sample_button = QPushButton("Load sample prices")
        sample_button.clicked.connect(self._on_load_sample_clicked)
        save_button = QPushButton("Save")
        save_button.clicked.connect(self._on_save_clicked)
        self._status_label = QLabel("")
        buttons.addWidget(add_button)
        buttons.addWidget(remove_button)
        buttons.addWidget(sample_button)
        buttons.addWidget(save_button)
        buttons.addWidget(self._status_label)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self._known_keys: set[tuple[str, str]] = set()
        self._load()

    def _add_row(self, price: repositories.PriceRow | None = None) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        values = (
            (
                price.provider,
                price.model_id,
                str(price.input_per_mtok),
                str(price.output_per_mtok),
                price.unit,
            )
            if price is not None
            else ("", "", "0.0", "0.0", "USD")
        )
        for column, value in enumerate(values):
            self._table.setItem(row, column, QTableWidgetItem(value))

    def _remove_selected(self) -> None:
        for index in sorted({i.row() for i in self._table.selectedIndexes()}, reverse=True):
            self._table.removeRow(index)

    def _load(self) -> None:
        for price in repositories.list_prices(self._conn):
            self._add_row(price)
            self._known_keys.add((price.provider, price.model_id))

    def _on_load_sample_clicked(self) -> None:
        for price in pricing.load_sample_prices():
            self._add_row(price)

    def _collect(self) -> list[repositories.PriceRow]:
        rows: list[repositories.PriceRow] = []
        for row in range(self._table.rowCount()):
            provider_item = self._table.item(row, 0)
            model_item = self._table.item(row, 1)
            if provider_item is None or model_item is None:
                continue
            provider = provider_item.text().strip()
            model_id = model_item.text().strip()
            if not provider or not model_id:
                continue
            input_item = self._table.item(row, 2)
            output_item = self._table.item(row, 3)
            unit_item = self._table.item(row, 4)
            try:
                input_price = float(input_item.text()) if input_item else 0.0
                output_price = float(output_item.text()) if output_item else 0.0
            except ValueError:
                continue
            unit = (unit_item.text().strip() if unit_item else "") or "USD"
            rows.append(repositories.PriceRow(provider, model_id, input_price, output_price, unit))
        return rows

    def _on_save_clicked(self) -> None:
        current_rows = self._collect()
        current_keys = {(r.provider, r.model_id) for r in current_rows}
        for provider, model_id in self._known_keys - current_keys:
            repositories.delete_price(self._conn, provider, model_id)
        for row in current_rows:
            repositories.set_price(self._conn, row)
        self._known_keys = current_keys
        self._status_label.setText("Saved.")


def _clear_layout(layout: QLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item is None:
            continue
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
        child_layout = item.layout()
        if child_layout is not None:
            _clear_layout(child_layout)


class _SkillsTab(QWidget):
    """Invoice-extraction settings, and the enable/disable + Reload list for
    installed skills (ARCHITECTURE.md §10.1: "Settings → Skills tab")."""

    def __init__(self, conn: sqlite3.Connection, skills_manager: SkillsManager) -> None:
        super().__init__()
        self._conn = conn
        self._skills_manager = skills_manager

        layout = QVBoxLayout(self)

        extraction_group = QGroupBox("Invoice extraction")
        extraction_form = QFormLayout(extraction_group)
        extraction_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self._npwp_pattern = QLineEdit(
            repositories.get_setting(conn, "extraction.npwp_pattern", "") or ""
        )
        self._npwp_pattern.setPlaceholderText(r"e.g. \d{2}\.\d{3}\.\d{3}\.\d-\d{3}\.\d{3}")
        extraction_form.addRow("Vendor NPWP pattern (regex, optional)", self._npwp_pattern)

        self._document_number_pattern = QLineEdit(
            repositories.get_setting(conn, "extraction.document_number_pattern", "") or ""
        )
        extraction_form.addRow(
            "Invoice/receipt number pattern (regex, optional)", self._document_number_pattern
        )

        self._day_first = QCheckBox("Numeric dates are day-first (dd/mm/yyyy) by default")
        self._day_first.setChecked(
            (repositories.get_setting(conn, "extraction.day_first_dates", "true") or "true")
            == "true"
        )
        extraction_form.addRow(self._day_first)

        extraction_buttons = QHBoxLayout()
        extraction_save = QPushButton("Save")
        extraction_save.clicked.connect(self._on_save_extraction_clicked)
        self._extraction_status = QLabel("")
        extraction_buttons.addWidget(extraction_save)
        extraction_buttons.addWidget(self._extraction_status)
        extraction_buttons.addStretch(1)
        extraction_form.addRow(extraction_buttons)

        layout.addWidget(extraction_group)

        skills_group = QGroupBox("Installed skills")
        skills_layout = QVBoxLayout(skills_group)
        self._skills_list_layout = QVBoxLayout()
        skills_layout.addLayout(self._skills_list_layout)

        self._errors_label = QLabel("")
        self._errors_label.setWordWrap(True)
        skills_layout.addWidget(self._errors_label)

        reload_row = QHBoxLayout()
        reload_button = QPushButton("Reload")
        reload_button.clicked.connect(self._on_reload_clicked)
        reload_row.addWidget(reload_button)
        reload_row.addStretch(1)
        skills_layout.addLayout(reload_row)

        layout.addWidget(skills_group)
        layout.addStretch(1)

        self._refresh_skills_list()

    def _on_save_extraction_clicked(self) -> None:
        repositories.set_setting(
            self._conn, "extraction.npwp_pattern", self._npwp_pattern.text().strip()
        )
        repositories.set_setting(
            self._conn,
            "extraction.document_number_pattern",
            self._document_number_pattern.text().strip(),
        )
        repositories.set_setting(
            self._conn,
            "extraction.day_first_dates",
            "true" if self._day_first.isChecked() else "false",
        )
        self._extraction_status.setText("Saved.")

    def _on_reload_clicked(self) -> None:
        self._skills_manager.reload()
        self._refresh_skills_list()

    def _on_skill_toggled(self, name: str, checked: bool) -> None:
        self._skills_manager.set_disabled(name, not checked)
        self._refresh_skills_list()

    def _refresh_skills_list(self) -> None:
        _clear_layout(self._skills_list_layout)
        disabled = self._skills_manager.disabled_names()
        all_skills = self._skills_manager.all_skills()

        if not all_skills:
            self._skills_list_layout.addWidget(QLabel("(no skills installed)"))

        for skill in all_skills:
            row = QHBoxLayout()
            checkbox = QCheckBox(skill.name)
            checkbox.setChecked(skill.name not in disabled)
            checkbox.toggled.connect(
                lambda checked, name=skill.name: self._on_skill_toggled(name, checked)
            )
            row.addWidget(checkbox)
            description = QLabel(skill.description)
            description.setWordWrap(True)
            row.addWidget(description, stretch=1)
            self._skills_list_layout.addLayout(row)

        errors = self._skills_manager.errors()
        self._errors_label.setText(
            "\n".join(f"{e.path.name}: {e.message}" for e in errors) if errors else ""
        )


def _parse_env_lines(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key:
            env[key] = value.strip()
    return env


class _McpServerCard(QGroupBox):
    """One reusable editor for whichever configured MCP server is selected
    above it (ARCHITECTURE.md §8.3). MCP servers run with the user's own OS
    permissions — unlike the sandboxed file tools, nothing here restricts
    what a server can do, which is why that's spelled out up front rather
    than left as a surprise."""

    saved = Signal(str)
    removed = Signal()

    def __init__(self, conn: sqlite3.Connection, mcp_manager: McpManager) -> None:
        super().__init__("")
        self._conn = conn
        self._mcp_manager = mcp_manager
        self._server_id = ""
        self._discovered_tool_names: list[str] = []

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "⚠ MCP servers run with your full account permissions — they are not "
                "sandboxed to a workspace folder the way the built-in file tools are."
            )
        )

        top_row = QHBoxLayout()
        self._enabled = QCheckBox("Enabled")
        self._enabled.setChecked(True)
        top_row.addWidget(self._enabled)
        top_row.addStretch(1)
        self._status_label = QLabel("")
        top_row.addWidget(self._status_label)
        save_button = QPushButton("Save")
        save_button.clicked.connect(self._on_save_clicked)
        test_button = QPushButton("Test")
        test_button.clicked.connect(self._on_test_clicked)
        remove_button = QPushButton("Remove")
        remove_button.clicked.connect(self._on_remove_clicked)
        top_row.addWidget(save_button)
        top_row.addWidget(test_button)
        top_row.addWidget(remove_button)
        layout.addLayout(top_row)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addLayout(form)

        self._display_name = QLineEdit("New MCP server")
        form.addRow("Display name", self._display_name)

        self._transport = QComboBox()
        self._transport.addItem("Local command (stdio)", "stdio")
        self._transport.addItem("Remote URL (HTTP)", "http")
        form.addRow("Transport", self._transport)

        self._command = QLineEdit()
        self._command.setPlaceholderText("Local command (stdio) — e.g. npx")
        form.addRow("Command", self._command)

        self._args = QLineEdit()
        self._args.setPlaceholderText("Arguments, e.g. -y @some/mcp-server")
        form.addRow("Args", self._args)

        self._env = QPlainTextEdit()
        self._env.setPlaceholderText("Optional, one per line — e.g. API_KEY=abc123")
        self._env.setMaximumHeight(50)
        form.addRow("Env", self._env)

        self._url = QLineEdit()
        self._url.setPlaceholderText("Remote URL (HTTP) — e.g. https://example.com/mcp")
        form.addRow("URL", self._url)

        self._auth_header = QLineEdit()
        self._auth_header.setEchoMode(QLineEdit.EchoMode.Password)
        self._auth_header.setPlaceholderText(
            "Optional Authorization header value — unchanged if left blank"
        )
        form.addRow("Auth header", self._auth_header)

        layout.addWidget(QLabel("Tools (checked = read-only, no confirmation needed):"))
        self._tools_layout = QVBoxLayout()
        layout.addLayout(self._tools_layout)

    def load_existing(self, server_id: str) -> None:
        self._server_id = server_id
        self._status_label.setText("")

        row = repositories.get_mcp_server(self._conn, server_id)
        if row is None:
            return
        config = config_from_row(row)
        self.setTitle(row.name)
        self._enabled.setChecked(row.enabled)
        self._display_name.setText(row.name)
        self._transport.setCurrentIndex(1 if config.is_remote else 0)
        self._command.setText(config.command or "")
        self._args.setText(" ".join(config.args))
        self._env.setPlainText("\n".join(f"{k}={v}" for k, v in config.env.items()))
        self._url.setText(config.url or "")
        self._auth_header.clear()
        self._auth_header.setPlaceholderText(
            "Already set — unchanged if left blank"
            if config.has_auth_header
            else "Optional Authorization header value — unchanged if left blank"
        )
        self._discovered_tool_names = sorted(config.read_only_tools)
        self._rebuild_tools_checklist(self._discovered_tool_names, config.read_only_tools)

        error = self._mcp_manager.last_error(server_id)
        is_running = any(c.config.id == server_id for c in self._mcp_manager.connections())
        if error:
            self._status_label.setText(f"Not running — last error: {error}")
        elif is_running:
            self._status_label.setText("Running in this session.")

    def start_new(self, server_id: str) -> None:
        self._server_id = server_id
        self.setTitle("New MCP server")
        self._enabled.setChecked(True)
        self._display_name.setText("New MCP server")
        self._transport.setCurrentIndex(0)
        self._command.clear()
        self._args.clear()
        self._env.clear()
        self._url.clear()
        self._auth_header.clear()
        self._auth_header.setPlaceholderText(
            "Optional Authorization header value — unchanged if left blank"
        )
        self._discovered_tool_names = []
        self._rebuild_tools_checklist([], frozenset())
        self._status_label.setText("")

    def _rebuild_tools_checklist(self, names: list[str], read_only: frozenset[str]) -> None:
        _clear_layout(self._tools_layout)
        if not names:
            self._tools_layout.addWidget(QLabel("(none discovered yet — click Test)"))
            return
        for name in names:
            checkbox = QCheckBox(name)
            checkbox.setChecked(name in read_only)
            self._tools_layout.addWidget(checkbox)

    def _checked_read_only_tools(self) -> frozenset[str]:
        names = set()
        for i in range(self._tools_layout.count()):
            item = self._tools_layout.itemAt(i)
            widget = item.widget() if item is not None else None
            if isinstance(widget, QCheckBox) and widget.isChecked():
                names.add(widget.text())
        return frozenset(names)

    def _build_config(self) -> McpServerConfig:
        is_remote = self._transport.currentData() == "http"
        return McpServerConfig(
            id=self._server_id,
            name=self._display_name.text().strip() or "New MCP server",
            enabled=self._enabled.isChecked(),
            command=self._command.text().strip() or None if not is_remote else None,
            args=shlex_split(self._args.text()) if not is_remote else [],
            env=_parse_env_lines(self._env.toPlainText()) if not is_remote else {},
            url=self._url.text().strip() or None if is_remote else None,
            has_auth_header=bool(self._auth_header.text().strip())
            or (
                bool(secrets.get_secret(f"mcp:{self._server_id}:auth_header"))
                if is_remote
                else False
            ),
            read_only_tools=self._checked_read_only_tools(),
        )

    def _on_save_clicked(self) -> None:
        config = self._build_config()
        repositories.upsert_mcp_server(
            self._conn, self._server_id, config.name, config_to_dict(config), config.enabled
        )
        auth_header = self._auth_header.text().strip()
        if auth_header:
            secrets.set_secret(f"mcp:{self._server_id}:auth_header", auth_header)
            self._auth_header.clear()
        self.setTitle(config.name)
        self._status_label.setText("Saved.")
        self.saved.emit(self._server_id)

    def _on_test_clicked(self) -> None:
        self._status_label.setText("Testing…")
        config = self._build_config()
        auth_header = self._auth_header.text().strip() or (
            secrets.get_secret(f"mcp:{self._server_id}:auth_header")
            if config.has_auth_header
            else None
        )
        previously_checked = self._checked_read_only_tools()

        async def _run() -> None:
            try:
                tools = await probe_server(config, auth_header)
            except McpError as exc:
                self._status_label.setText(f"Failed: {exc}")
                return
            names = sorted(t.name for t in tools)
            self._discovered_tool_names = names
            self._rebuild_tools_checklist(names, previously_checked)
            self._status_label.setText(
                f"OK — {len(names)} tool(s): {', '.join(names)}" if names else "OK — no tools"
            )

        asyncio.ensure_future(_run())

    def _on_remove_clicked(self) -> None:
        if not self._server_id:
            return
        repositories.delete_mcp_server(self._conn, self._server_id)
        secrets.delete_secret(f"mcp:{self._server_id}:auth_header")
        self.removed.emit()


class _McpTab(QWidget):
    """Configured MCP servers (ARCHITECTURE.md §8.3): a selector picks which
    one the single card below shows, the same pattern as Custom providers."""

    def __init__(self, conn: sqlite3.Connection, mcp_manager: McpManager) -> None:
        super().__init__()
        self._conn = conn
        self._mcp_manager = mcp_manager

        layout = QVBoxLayout(self)

        selector_row = QHBoxLayout()
        selector_row.addWidget(QLabel("Server:"))
        self._selector = QComboBox()
        self._selector.currentIndexChanged.connect(self._on_selector_changed)
        selector_row.addWidget(self._selector)
        add_button = QPushButton("+ Add new")
        add_button.clicked.connect(self._on_add_new_clicked)
        selector_row.addWidget(add_button)
        selector_row.addStretch(1)
        layout.addLayout(selector_row)

        self._card = _McpServerCard(conn, mcp_manager)
        self._card.saved.connect(self._on_card_saved)
        self._card.removed.connect(self._on_card_removed)
        layout.addWidget(self._card)
        layout.addStretch(1)

        self._reload_selector(select_first=True)

    def _reload_selector(self, *, select_first: bool = False, select_id: str | None = None) -> None:
        self._selector.blockSignals(True)
        self._selector.clear()
        for row in repositories.list_mcp_servers(self._conn):
            self._selector.addItem(row.name, row.id)
        self._selector.blockSignals(False)

        if select_id is not None and self._selector.findData(select_id) >= 0:
            self._selector.setCurrentIndex(self._selector.findData(select_id))
        elif select_first and self._selector.count() > 0:
            self._selector.setCurrentIndex(0)
        self._on_selector_changed(self._selector.currentIndex())

    def _on_selector_changed(self, index: int) -> None:
        if index < 0:
            self._card.start_new(str(uuid.uuid4()))
            return
        server_id = str(self._selector.itemData(index))
        self._card.load_existing(server_id)

    def _on_add_new_clicked(self) -> None:
        self._selector.blockSignals(True)
        self._selector.setCurrentIndex(-1)
        self._selector.blockSignals(False)
        self._card.start_new(str(uuid.uuid4()))

    def _on_card_saved(self, server_id: str) -> None:
        self._reload_selector(select_id=server_id)

    def _on_card_removed(self) -> None:
        self._reload_selector(select_first=True)
