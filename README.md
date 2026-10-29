# deskai

A desktop AI assistant: a PySide6 tray app for Windows and macOS with chat,
settings and usage windows. It supports Bedrock, Cloudflare Workers AI and
OpenAI-compatible (local) model providers, MCP servers, and local file,
Office, PDF and OCR tools sandboxed to per-chat workspace folders.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how it's built.

## Running from source

```bash
uv sync
uv run python -m deskai
```

## Building an installer

```bash
uv run pyinstaller packaging/deskai.spec --noconfirm
```

- **Windows**: then build `packaging/windows/installer.iss` with
  [Inno Setup](https://jrsoftware.org/isinfo.php) to get
  `deskai-<version>-windows-x64-setup.exe`.
- **macOS**: then run `bash packaging/macos/build_dmg.sh` to get
  `deskai-<version>-macos-<arch>.dmg`.

Tagged pushes (`vX.Y.Z`) build both automatically via
`.github/workflows/release.yml` and attach them to a GitHub Release.

### Opening an unsigned build

These builds aren't code-signed or notarized yet (optional for v1 — see
ARCHITECTURE.md §14), so your OS will warn you the first time you open one:

- **Windows**: SmartScreen will say "Windows protected your PC" — click
  **More info**, then **Run anyway**.
- **macOS**: Gatekeeper will refuse to open it from Finder. Go to
  **System Settings → Privacy & Security**, scroll to the blocked-app
  notice, and click **Open Anyway**.
