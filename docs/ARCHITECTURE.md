# Architecture — Desktop AI Assistant (working name: `deskai`)

A cross-platform (Windows + macOS) desktop AI chat app that lives in the system tray, talks to cloud or local LLMs, and can work on local files (Office, PDF, Markdown, scanned documents) through tools.

> Rename `deskai` to the final product name before the first release.

---

## 1. Goals and non-goals

**Goals**
- Tray-resident app with Chat, Settings and Usage windows.
- Pluggable model providers: AWS Bedrock, Cloudflare Workers AI, and any OpenAI-compatible endpoint (Ollama, LM Studio, llama.cpp server) for offline use.
- Local tools: list directories; read/write/update `.md`, `.txt`, `.docx`, `.xlsx`, `.pptx`; read PDFs; OCR scanned PDFs and images.
- Per-chat-session workspace folders: the AI can only touch folders attached to the current chat.
- Token usage tracking with estimated cost.
- Skills: user-editable markdown instruction files that teach the AI repeatable workflows (e.g. "list invoices in a folder into Excel sorted by due date"), with no new code.
- MCP client support so external MCP servers can add tools.
- Shipped as one self-contained installer per OS via GitHub Releases. Users install nothing else.

**Non-goals (v1)**
- No App Store distribution.
- No multi-user server or backend. Everything runs on the user's machine; only model calls leave it.
- No driving the Office applications themselves (COM / AppleScript). Files are edited directly.
- No dedicated invoice application or database. Invoice/receipt work is a **skill** (§10) on top of generic tools. Results always carry review flags, and nothing is booked automatically.

---

## 2. Tech stack

| Concern | Choice |
|---|---|
| Language | Python 3.12 |
| Env / deps | `uv` (pyproject.toml), `ruff`, `mypy` (strict on `core/`), `pytest` |
| UI | PySide6 (Qt 6). Chat rendering via `QWebEngineView` (HTML + markdown) or `QTextBrowser` if WebEngine is too heavy. Decide in Phase 1 (see §15). |
| Async | `asyncio` integrated with Qt via `qasync` |
| HTTP | `httpx` (async) |
| Bedrock | `boto3` `bedrock-runtime` Converse / ConverseStream (sync, run in a thread executor) |
| Secrets | `keyring` (Windows Credential Manager / macOS Keychain) |
| Storage | SQLite via `sqlite3` (or `SQLAlchemy Core`). Single DB file in the user data dir. |
| Paths | `platformdirs` |
| docx | `python-docx` |
| xlsx | `openpyxl` |
| pptx | `python-pptx` |
| PDF | `PyMuPDF` (`fitz`) for text, page render and metadata |
| OCR | `rapidocr-onnxruntime` (+ `opencv-python-headless` for preprocessing) |
| Markdown render | `markdown-it-py` + `pygments` |
| Charts | `pyqtgraph` |
| MCP | official `mcp` Python SDK (client side) |
| Packaging | PyInstaller (one-folder mode) → Inno Setup (`.exe` installer) on Windows, `.dmg` on macOS; built per-OS in GitHub Actions |

---

## 3. Repository layout

```
deskai/
├─ pyproject.toml
├─ CLAUDE.md
├─ docs/ARCHITECTURE.md
├─ src/deskai/
│  ├─ __main__.py            # entry point: single-instance check, qasync loop, tray
│  ├─ app.py                 # AppController: owns windows, services, lifecycle
│  ├─ ui/
│  │  ├─ tray.py
│  │  ├─ chat_window.py
│  │  ├─ chat_view/          # HTML template, CSS, JS bridge (QWebChannel)
│  │  ├─ settings_window.py  # tabs: Providers, Tools, Privacy, MCP, Pricing
│  │  ├─ usage_window.py
│  │  ├─ folder_picker.py    # per-session workspace folder chips
│  │  └─ confirm_dialog.py   # approve/deny write actions, shows diff/summary
│  ├─ core/
│  │  ├─ messages.py         # normalized message / content-part / tool-call types
│  │  ├─ agent.py            # agent loop
│  │  ├─ session.py          # ChatSession: history, workspace folders, provider, privacy
│  │  ├─ events.py           # typed events streamed to the UI
│  │  └─ errors.py
│  ├─ providers/
│  │  ├─ base.py             # Provider protocol + ModelInfo capabilities
│  │  ├─ bedrock.py
│  │  ├─ cloudflare.py
│  │  └─ openai_compat.py
│  ├─ tools/
│  │  ├─ base.py             # Tool base class, RiskLevel, ToolContext
│  │  ├─ registry.py
│  │  ├─ sandbox.py          # path resolution + workspace checks
│  │  ├─ fs_tools.py         # list_dir, file_info, search_files
│  │  ├─ text_tools.py       # read/write/update md & txt
│  │  ├─ docx_tools.py
│  │  ├─ xlsx_tools.py
│  │  ├─ pptx_tools.py
│  │  ├─ pdf_tools.py
│  │  ├─ ocr_tools.py
│  │  ├─ doc_extract_tools.py # extract_invoice, validate_invoice, parse_amount, parse_date
│  │  ├─ skill_tools.py      # load_skill
│  │  └─ mcp_bridge.py       # exposes MCP server tools through the registry
│  ├─ ocr/
│  │  ├─ engine.py           # RapidOCR wrapper, runs in a process pool
│  │  └─ preprocess.py       # deskew, contrast, upscale
│  ├─ extraction/            # deterministic helpers used by doc_extract_tools
│  │  ├─ invoice_schema.py   # field schema (pydantic) for invoices/receipts
│  │  ├─ layout.py           # group OCR boxes into rows
│  │  ├─ llm_extract.py      # one-file LLM call -> JSON (isolated sub-request)
│  │  ├─ validate.py         # arithmetic / format / duplicate checks
│  │  ├─ numbers.py          # Indonesian / international amount parsing
│  │  └─ dates.py            # date parsing incl. Indonesian month names
│  ├─ skills/
│  │  ├─ loader.py           # discover, parse frontmatter, build skill index
│  │  └─ builtin/            # skills shipped with the app (copied to user dir on first run)
│  │     └─ invoice-list/SKILL.md
│  ├─ mcp_client/
│  │  └─ manager.py          # start/stop MCP servers, list tools, call tools
│  ├─ storage/
│  │  ├─ db.py               # connection, migrations
│  │  ├─ repositories.py     # sessions, messages, usage, settings
│  │  └─ secrets.py          # keyring wrapper
│  └─ usage/
│     ├─ tracker.py          # records usage rows
│     └─ pricing.py          # editable price table, cost estimation
├─ tests/
└─ packaging/
   ├─ deskai.spec            # PyInstaller
   ├─ windows/installer.iss  # Inno Setup
   └─ macos/                 # dmg config, entitlements, icon
```

---

## 4. Windows and tray

**Tray (always present)** menu:
- New chat
- Open chat
- Usage
- Settings
- Quit

**Lifecycle**
- Closing any window hides it. The app keeps running in the tray.
- Quit from the tray stops MCP servers, cancels running agent tasks and closes the DB.
- Single instance: a second launch focuses the existing Chat window (`QLocalServer` / `QLocalSocket`).
- Optional global hotkey to show/hide the Chat window (Phase 6).
- Optional launch at login (Phase 6).

**Chat window**
- **Left sidebar:** session list with search, new chat, rename and delete.
- **Header bar (per session):**
  - **Workspace folder chips** (see §7): "+ Add folder" opens the OS folder picker, and each chip has a remove ✕ and a read-only toggle.
  - **Provider/model dropdown**, per session.
  - **Privacy indicator:** "Cloud" or "Local only".
- **Message area:**
  - Streaming markdown.
  - Collapsible tool-call cards showing tool name, arguments, result summary, duration, and exactly what was sent to the model.
- **Input area:**
  - Multiline text input.
  - Attach file button: copies a reference only and adds the file's folder to the workspace if the user confirms.
  - Send/Stop button.

**Settings window** tabs:
- **Providers:** credentials, model lists, test connection (see §5).
- **Tools:** enable/disable each built-in tool, and set the default confirmation policy.
- **Skills:** list of installed skills (built-in + user), enable/disable each, "Open skills folder", "New skill" (creates a template file), "Reload".
- **Privacy:**
  - Default provider for new sessions.
  - Global deny-list of paths, which can never be added as workspace (system dirs, `~/.ssh`, keychain dirs, the app's own data dir).
  - "Local only" folder rules.
- **MCP:** list of configured MCP servers (command, args, env, enabled).
- **Pricing:** editable per-model price table used for cost estimates.
- **General:** theme, launch at login, hotkey, data location, export/delete data.

**Usage window** — see §11.

---

## 5. Providers

### 5.1 Normalized message model (`core/messages.py`)

```python
class Role(StrEnum):
    SYSTEM, USER, ASSISTANT, TOOL


@dataclass
class TextPart:
    text: str


@dataclass
class ImagePart:
    data: bytes
    media_type: str  # png / jpeg


@dataclass
class ToolCallPart:
    id: str
    name: str
    arguments: dict


@dataclass
class ToolResultPart:
    tool_call_id: str
    content: list[TextPart | ImagePart]
    is_error: bool = False


@dataclass
class Message:
    role: Role
    parts: list[TextPart | ImagePart | ToolCallPart | ToolResultPart]
```

Providers convert to and from their wire formats. Nothing outside `providers/` knows a wire format.

### 5.2 Provider protocol (`providers/base.py`)

```python
@dataclass
class ModelInfo:
    id: str                 # provider model id / inference profile ARN
    display_name: str
    supports_tools: bool
    supports_images: bool
    context_tokens: int

class StreamEvent: ...      # TextDelta | ToolCallComplete | Usage(input, output) | Done(stop_reason) | Error

class Provider(Protocol):
    name: str
    async def list_models(self) -> list[ModelInfo]
    async def test_connection(self) -> None            # raises on failure
    def stream_chat(self, model: ModelInfo, system: str,
                    messages: list[Message], tools: list[ToolSpec]
                    ) -> AsyncIterator[StreamEvent]
```

Rules:
- If `supports_tools` is false, the agent sends no tools and warns the user in the UI.
- If `supports_images` is false, image parts are replaced by OCR text (see §9).
- Every stream must end with a `Usage` event when the provider reports usage.

### 5.3 Bedrock (`providers/bedrock.py`)

- `boto3.client("bedrock-runtime", region_name=...)`, using `converse_stream`. Run it in a thread executor and bridge to an async iterator.
- **Credentials:**
  - Access key ID + secret, or a Bedrock API key, stored in keyring.
  - Optionally a named AWS profile (SSO) instead of keys.
- **Settings fields:** region; auth mode; model entries (model ID **or** application inference profile ARN, plus capability flags).
- Per-department cost tracking happens on the AWS side through IAM principal tags or application inference profiles. The app only needs to allow either form of model ID.
- Map Converse `toolUse` / `toolResult` / `image` blocks to the normalized types, and `metadata.usage` to the `Usage` event.

### 5.4 Cloudflare Workers AI (`providers/cloudflare.py`)

- Built on top of the OpenAI-compatible adapter, using httpx.
- **Settings fields:**
  - Account ID.
  - API token (Workers AI scope), stored in keyring.
  - Optional AI Gateway ID. When set, requests go through the gateway URL so per-department analytics and limits apply. Verify the current gateway URL format in Cloudflare docs.
  - Model entries with capability flags.
- Default base URL: `https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1` (OpenAI-compatible `chat/completions`).
- Cost unit is Neurons; pricing table entries for Cloudflare models are expressed in that unit (see §11).

### 5.5 OpenAI-compatible / local (`providers/openai_compat.py`)

- **Settings fields:** base URL (default `http://localhost:11434/v1` for Ollama), optional API key, model entries.
- Used for offline "Local only" sessions.
- Small local models misfire on tool calls. Validate arguments strictly (§6) and allow 1 automatic retry with the validation error fed back.

---

## 6. Agent loop (`core/agent.py`)

```
run(session, user_message):
  append user message
  for step in range(MAX_STEPS = 12):
      stream = provider.stream_chat(model, system_prompt, history, enabled_tools)
      forward TextDelta events to UI; collect tool calls; record Usage
      if no tool calls: done
      for each tool call (sequentially in v1):
          validate args against JSON schema   → on failure return error result to model
          resolve paths through sandbox       → on violation return error result
          if tool.risk >= WRITE: await user confirmation (UI dialog); denial → error result "user denied"
          execute (async; CPU-heavy work in executor/process pool) with timeout
          truncate oversized results (see below), append ToolResultPart
  if MAX_STEPS reached: stop and tell the user
```

- **Cancellation:** the Stop button cancels the asyncio task. Running tools receive cancellation, and OCR jobs in the process pool are abandoned.
- **Result size cap:** tool results over ~20k characters are truncated, with a note telling the model how to request a specific page, range or sheet. Large documents are read in pages or ranges, never all at once.
- **System prompt:** a static base prompt plus the current workspace folder list (paths only), today's date, which tools are available, and the **skill index** (name + one-line description of each enabled skill, see §10). Never include file contents or full skill bodies in the system prompt.
- **Events to UI** (`core/events.py`):
  - `TextDelta`
  - `ToolCallStarted`
  - `ToolCallFinished`
  - `ConfirmationRequired`
  - `UsageRecorded`
  - `Error`
  - `Done`

---

## 7. Workspace folders per chat session (sandbox)

Folders are attached to a **chat session**, not set globally.

- Each session has 0..n workspace folders, stored in the `session_folders` table with a `read_only` flag.
- A new session starts with no folders. A setting allows "start with last-used folder" and offers a "recent folders" list in the picker.
- With no folders attached, only non-file tools are available, and the model is told to ask the user to attach a folder.
- **Path resolution** (`tools/sandbox.py`):
  - Relative paths resolve against the first workspace folder.
  - Absolute paths must fall inside one of the session's folders.
  - Resolve with `Path.resolve(strict=False)` so symlinks and `..` are followed, then check `is_relative_to` against each folder's resolved path.
  - Reject anything matching the global deny-list.
- Write tools are refused on read-only folders.
- Changing folders mid-session takes effect on the next agent step, and a system note is appended to history ("Workspace changed: …").
- **Privacy flag:** folders that match a "Local only" rule in Settings force the session to a local provider, or block cloud sending with a clear message.

---

## 8. Tools

### 8.1 Base (`tools/base.py`)

```python
class RiskLevel(IntEnum): READ = 0, WRITE = 1, DESTRUCTIVE = 2

class Tool(ABC):
    name: str
    description: str
    input_schema: dict            # JSON Schema
    risk: RiskLevel
    async def run(self, args: dict, ctx: ToolContext) -> ToolResult

@dataclass
class ToolContext:
    session_id: str
    workspace: Sandbox
    ocr: OcrEngine
    cancel: asyncio.Event
```

- Registry merges built-in tools and MCP tools. Disabled tools are not sent to the model.
- **Confirmation policy:**
  - READ: never asks.
  - WRITE: asks by default. The user can choose "allow for this session".
  - DESTRUCTIVE: always asks.
- **Backups:** before any write to an existing file, copy it to `<file>.bak-<timestamp>` (configurable), or keep backups in the app data dir.
- **Confirmation dialog:**
  - For text files, it shows a unified diff.
  - For Office files, it shows a structured summary of changes (e.g. "Sheet1!B2: 100 → 120", "Paragraph 4 replaced").

### 8.2 v1 tool list

| Tool | Risk | Notes |
|---|---|---|
| `list_dir(path, depth=1, pattern?)` | READ | Names, sizes, modified time. Cap entries. |
| `file_info(path)` | READ | Type, size, page/sheet/slide count |
| `search_files(query, path?, glob?)` | READ | Filename search; optional content search for text files |
| `read_text(path, start_line?, end_line?)` | READ | md, txt, csv |
| `write_text(path, content, overwrite=false)` | WRITE | Create or overwrite |
| `edit_text(path, old, new)` | WRITE | Exact-match replace, must match once |
| `read_docx(path, paragraph_range?)` | READ | Paragraphs with index and style, tables as markdown |
| `create_docx(path, blocks)` | WRITE | Blocks: heading / paragraph / bullet / table |
| `update_docx(path, ops)` | WRITE | Ops: replace_text, append_paragraph, insert_paragraph_after(index), set_table_cell. Preserve run formatting where possible. |
| `read_xlsx(path, sheet?, range?)` | READ | Values (and formulas on request), sheet list |
| `write_xlsx(path, sheet, range, values)` | WRITE | Create or update cells; keep other content |
| `read_pptx(path, slide_range?)` | READ | Slide text, notes, table text |
| `update_pptx(path, ops)` | WRITE | Ops: replace_text, set_notes, add_text_slide(title, bullets) |
| `read_pdf(path, page_range?)` | READ | Text layer per page; auto-OCR pages with no text layer (flag `ocr_used`) |
| `ocr_image(path)` / `ocr_pdf(path, page_range?)` | READ | Returns text with per-line confidence |
| `view_image(path or pdf page)` | READ | Returns an ImagePart for vision-capable models only |
| `load_skill(name)` | READ | Returns the full instructions of a skill (§10) |
| `extract_invoice(path, doc_type="auto")` | READ | One file → structured JSON + validation + review flags (§10.3). Runs its own isolated LLM sub-request; does not put the full document into the chat. |
| `validate_invoice(data)` | READ | Runs the deterministic checks on given JSON; returns per-check pass/fail |
| `parse_amount(text)` / `parse_date(text)` | READ | Deterministic parsing; returns value + interpretation note or an ambiguity error |

**Excluded in v1:** delete, move and rename. If added later, they are DESTRUCTIVE.

### 8.3 MCP (`mcp_client/manager.py`, `tools/mcp_bridge.py`)

- **Configuration:** in Settings → MCP, each server has command, args, env and enabled. Remote HTTP servers have URL plus optional auth header, with secrets in keyring.
- **Startup:** enabled servers start lazily the first time a session needs tools, and stop on Quit.
- **Tool naming:** tools are namespaced as `mcp__<server>__<tool>`.
- **Default risk:** WRITE, so calls ask for confirmation. The user can mark individual MCP tools as READ.
- **Limitations:**
  - MCP servers do not get the session sandbox. Show a warning in Settings that MCP servers run with the user's full permissions.
  - Local stdio servers may require Node or Python on the machine. Show a clear error if the command is not found.

---

## 9. PDF and OCR pipeline

1. `read_pdf` extracts the text layer per page with PyMuPDF.
2. A page is considered scanned if its extracted text has fewer than ~20 non-whitespace characters while the page contains images.
3. Scanned pages are rendered at 200–300 DPI, then preprocessed (`ocr/preprocess.py`: grayscale, deskew, contrast/CLAHE, upscale if small).
4. RapidOCR runs in a `ProcessPoolExecutor` (max workers = min(2, cpu_count−1)) so the UI never blocks.
5. The result is text lines with bounding boxes and confidence. The tool returns the text plus a low-confidence marker on lines under a threshold (e.g. 0.6).
6. **Vision fallback:** if the session model supports images and the user asks something visual (or confidence is low), the model may call `view_image` to get the page image.
7. **Cache:** OCR results are cached by (file hash, page, settings) in SQLite so re-reading is free.

**Packaging note:** bundle the RapidOCR ONNX model files explicitly in the PyInstaller spec (`datas`).

---

## 10. Skills, and the invoice/receipt skill

### 10.1 Skills system

A **skill** is a markdown file with YAML frontmatter that teaches the AI a repeatable workflow using existing tools. Skills contain instructions only, never code.

**Location**
- User skills: `<user_data_dir>/skills/<skill-name>/SKILL.md`.
- Built-in skills ship in `src/deskai/skills/builtin/` and are copied to the user dir on first run. They are never overwritten if the user edited them; compare against a hash stored in settings.

**Format**

```markdown
---
name: invoice-list
description: List invoices or receipts from files in a folder into an Excel sheet (sorting, filtering, totals).
version: 1
tools: [list_dir, extract_invoice, write_xlsx]   # tools the skill expects; warn if any are disabled
---
# Instructions
...steps, rules, output format...
```

**Runtime**
1. `skills/loader.py` scans the skills dir at startup and on "Reload" and validates the frontmatter. Invalid skills are listed in Settings with the error.
2. The **skill index** (name + description of enabled skills) is added to the system prompt (§6).
3. When a request matches a skill, the model calls `load_skill(name)` to get the full body. This keeps the system prompt small no matter how many skills exist.
4. A user can also force a skill by typing `/skill-name` at the start of a message. The agent then injects that skill's body before the first step.

**Trust:** skills are treated as user instructions, because they live in the user's own data dir and the user enables them. A skill cannot change settings, add workspace folders or enable tools. It can only describe how to use what the session already allows.

### 10.2 The `invoice-list` skill (built-in)

Example request: *"I have invoices in this folder, make an Excel list sorted by due date."*

Instructions the skill gives the model:
1. Use `list_dir` on the folder (and subfolders only if asked) to find `.pdf`, `.jpg`, `.jpeg`, `.png` files.
2. Call `extract_invoice` **once per file**. Never `read_pdf` whole invoices into the chat, and keep only the returned JSON. This keeps context small for folders with dozens of files.
3. Never re-calculate or "correct" amounts yourself. Use the values and validation results returned by the tool.
4. Build rows with these columns:
   - Source file
   - Vendor
   - Vendor NPWP
   - Invoice no.
   - Invoice date
   - Due date
   - Currency
   - Subtotal
   - Tax
   - Total
   - Status (`OK` / `Needs review`)
   - Notes
5. Sort as requested. Rows with a missing sort key (e.g. no due date) go **at the bottom**, never guessed.
6. Write with `write_xlsx` into the workspace. Default name `invoices_<YYYY-MM-DD>.xlsx`; never overwrite without confirmation.
   - Formatting: dates as real Excel dates, amounts as numbers with the currency in its own column.
   - Header row frozen and auto-filter on.
   - A totals row per currency.
7. Reply with a short summary: number of files processed, how many need review and why (grouped), and any files that could not be read.

Variations the same skill should handle: filter by vendor or month, total per vendor/month, receipts instead of invoices, CSV instead of xlsx.

### 10.3 `extract_invoice` tool (deterministic shell around one LLM call)

**Pipeline per file**
1. Get the text: PDF text layer, or OCR for scanned pages/images (§9). Keep line boxes and confidence.
2. Group lines into rows by y-coordinate (`extraction/layout.py`).
3. **Isolated LLM sub-request** (`extraction/llm_extract.py`):
   - Uses the session's provider and model, and respects Local-only mode.
   - Sends only that document's row text plus the field schema, and asks for strict JSON.
   - Values must be copied as raw strings; unknown fields are `null`; no guessing.
   - Usage is recorded with `triggered_by = "extract"`.
   - The document text never enters the main chat history.
4. Parse raw strings with `parse_amount` / `parse_date`. The LLM never converts numbers or dates itself.
5. Run validation (below) and attach review flags.
6. Return compact JSON: fields, line items (optional, `include_lines=false` by default for list use), `checks[]`, `status`, `notes[]`, `ocr_used`, `min_confidence`.

**Field schema** (`extraction/invoice_schema.py`):
- Header: doc_type (invoice / receipt), vendor name, vendor NPWP, invoice/receipt number, invoice date, due date, currency.
- Line items: description, qty, unit price, amount.
- Totals: subtotal, discount, tax, total.

**Validation** (`extraction/validate.py`, pure Python, fully unit-tested):
- Line amount = qty × unit price (tolerance).
- Sum(line amounts) = subtotal (tolerance).
- Subtotal − discount + tax = total (tolerance).
- Dates parse; due date ≥ invoice date; both within a plausible range.
- NPWP / invoice number match configurable regex patterns (Settings).
- Duplicate check: same vendor + number seen earlier **in the same batch**, tracked per agent run.
- Any field taken from OCR lines below the confidence threshold is flagged.
- Any failed check or required field missing → `status = "Needs review"`, with a human-readable note per issue.

**Number parsing** (`extraction/numbers.py`):
- Handle `1.250.000,00`, `1,250,000.00`, `Rp 1.250.000`, `1.250.000,-`, `IDR 1,250,000`.
- Decide the decimal separator by pattern; return an ambiguity error (not a guess) for cases like `1.250`.
- Thorough tests required.

**Date parsing** (`extraction/dates.py`):
- Handle `dd/mm/yyyy` (default for Indonesian documents, configurable), `yyyy-mm-dd`, `23 Oktober 2026`, `23-Okt-26`, and English month names.
- Ambiguous day/month with no hint → flag.

**Review** happens in the Excel output via the Status/Notes/Source file columns. A dedicated review UI is out of scope for v1.

---

## 11. Usage tracking and pricing

- **Recording:** after every model call, insert a `usage` row: timestamp, session_id, provider, model_id, input_tokens, output_tokens, triggered_by (`chat` | `tool_followup` | `extract` | `vision`), est_cost, currency.
- **Pricing table** (Settings → Pricing): user-editable rows of provider, model_id, input price per 1M tokens, output price per 1M tokens, unit (USD or Cloudflare Neurons conversion). No hardcoded prices; ship a sample file only.
- **Usage window:**
  - Range selector: today / 7 days / 30 days / custom.
  - Totals: input tokens, output tokens, estimated cost.
  - Daily bar chart (pyqtgraph).
  - Breakdown tables by provider + model, and by session (click a row to open that chat).
  - Export to CSV.
  - Label everything "Estimated on this device". Authoritative billing is AWS Cost Explorer / Cloudflare AI Gateway analytics.

---

## 12. Storage

**Location:** `platformdirs.user_data_dir("deskai")`, holding `deskai.db`, `logs/` and `backups/`.

**Tables** (via migrations in `storage/db.py`):

```
settings(key TEXT PK, value_json TEXT)
providers(id PK, kind, display_name, config_json, enabled)        -- no secrets
models(id PK, provider_id FK, model_id, display_name, supports_tools, supports_images, context_tokens)
sessions(id PK, title, provider_id, model_row_id, privacy_mode, created_at, updated_at)
session_folders(session_id FK, path, read_only, added_at)
messages(id PK, session_id FK, role, parts_json, created_at)
tool_calls(id PK, session_id FK, message_id FK, name, args_json, result_summary, status, duration_ms, created_at)
usage(id PK, ts, session_id, provider, model_id, input_tokens, output_tokens, triggered_by, est_cost, currency)
pricing(provider, model_id, input_per_mtok, output_per_mtok, unit, PRIMARY KEY(provider, model_id))
ocr_cache(file_hash, page, params_hash, result_json, PRIMARY KEY(file_hash, page, params_hash))
mcp_servers(id PK, name, transport, config_json, enabled)
skills_state(name PK, enabled, builtin_hash)        -- skill bodies live as files, not in the DB
```

- **Secrets** live only in keyring, under service `deskai` with keys like `provider:<id>:api_key`. They are never written to the DB, logs or exports.
- **Logs:** rotating file logs. Never log prompt contents or secrets at INFO level; DEBUG contents require an explicit setting.

---

## 13. Threading / concurrency model

- **Main thread:** the Qt event loop with an asyncio loop on top via `qasync`. All UI updates happen here.
- **Network:** async (httpx); boto3 calls run via `loop.run_in_executor`.
- **CPU-heavy work** (OCR, large Office parsing) goes to the `ProcessPoolExecutor` or `ThreadPoolExecutor`, never the main thread.
- **One agent task per session.** The UI disables Send while a task runs and shows Stop.

---

## 14. Packaging and release

- **PyInstaller** in one-folder mode (not `--onefile`, which starts slowly and triggers more antivirus flags).
  - Include: Qt plugins, WebEngine resources (if used), RapidOCR models, app icons.
- **Windows:** Inno Setup installer → `deskai-<version>-windows-x64-setup.exe`.
- **macOS:** `.app` in a `.dmg`, built separately for arm64 (and x86_64 if needed) or as universal2.
- **CI:** GitHub Actions matrix (`windows-latest`, `macos-latest`) builds on tag push and uploads to a GitHub Release.
- **Signing:**
  - Optional in v1; leave hooks in CI for a later Windows code-signing cert and Apple Developer ID notarization.
  - The README explains how to open unsigned builds: SmartScreen "More info → Run anyway"; macOS System Settings → Privacy & Security → "Open Anyway".
- **Version:** single source in `pyproject.toml`, shown in Settings → General.

---

## 15. Build phases and acceptance criteria

**Phase 1 — Shell**
- Tray, Chat, Settings and Usage windows (Usage empty).
- Close-to-tray, single instance, SQLite with migrations, keyring wrapper.
- Decide on QWebEngineView vs QTextBrowser for chat rendering.
- *Done when:* the app launches to the tray, windows open and hide correctly, and settings persist across restarts.

**Phase 2 — Chat with Bedrock**
- Provider protocol, Bedrock adapter, streaming chat, session list, per-session model dropdown, test connection, usage recording.
- *Done when:* a streamed conversation with a Bedrock model works, history persists, and usage rows are written.

**Phase 3 — Read tools + workspace**
- Folder chips per session, sandbox, tool registry, agent loop, tool-call cards.
- Tools: `list_dir`, `file_info`, `search_files`, `read_text`, `read_docx`, `read_xlsx`, `read_pptx`, `read_pdf` (text layer).
- *Done when:* the AI can answer questions about files in an attached folder, and is refused (with a clear message) outside it.

**Phase 4 — OCR + write tools**
- RapidOCR pipeline with cache, `ocr_*` tools, auto-OCR in `read_pdf`, `view_image`.
- Write/update tools with confirmation dialog, diffs and backups.
- *Done when:* a scanned PDF is read correctly, and a docx/xlsx edit is applied only after approval with a backup created.

**Phase 5 — More providers + usage window**
- Cloudflare adapter (with optional AI Gateway), OpenAI-compatible/local adapter, Local-only privacy mode, pricing table, Usage window with chart and CSV export.
- *Done when:* the same chat works on all three provider types, and usage shows per model and per session.

**Phase 6 — Skills + invoice skill**
- Skills loader, skill index in the system prompt, `load_skill`, `/skill-name` invocation, Settings → Skills tab.
- `parse_amount`, `parse_date`, `validate_invoice`, `extract_invoice`.
- Built-in `invoice-list` skill.
- *Done when:* "I have invoices in this folder, make an Excel list sorted by due date" on a test folder of 20+ mixed invoices (text PDFs, scanned PDFs, photos) produces a correct sorted xlsx. Every arithmetic or parsing problem is marked `Needs review` with a note, and chat context stays small (documents are not dumped into history).

**Phase 7 — MCP, polish, packaging**
- MCP client.
- Hotkey and autostart.
- PyInstaller + installers + GitHub Actions release.
- *Done when:* tagged builds produce installers for both OSes that run on a clean machine with nothing else installed.

---

## 16. Security checklist

- [ ] Secrets only in keyring; redacted in logs, errors and exports.
- [ ] Every file path goes through the sandbox, and the sandbox has tests for `..`, symlinks, case-insensitivity (Windows/macOS) and UNC paths.
- [ ] Write/destructive tools always pass through confirmation unless the user allowed them for this session.
- [ ] Document contents are treated as untrusted. They are never used to change settings, add folders or enable tools.
- [ ] Skills can only describe how to use tools already allowed in the session; they cannot change settings, folders or tool enablement.
- [ ] `extract_invoice` sub-requests respect the session's privacy mode (Local only → local provider).
- [ ] Tool-call cards show what was sent to the model.
- [ ] Local-only sessions never instantiate a cloud provider.
- [ ] MCP servers are clearly labeled as running outside the sandbox.
