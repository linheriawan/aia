"""PyInstaller spec (ARCHITECTURE.md §14): one-folder build, not --onefile
(which starts slowly and triggers more antivirus flags).

Build from the repo root with:
    uv run pyinstaller packaging/deskai.spec --noconfirm

PySide6 and keyring already ship PyInstaller hooks; the handful of
collect_* calls below cover the packages that don't (RapidOCR's bundled
ONNX models, PyMuPDF's bundled resources, and botocore's service-model
JSON data, which boto3 cannot run without).
"""

from __future__ import annotations

import os
import tomllib

from PyInstaller.building.api import COLLECT, EXE, PYZ
from PyInstaller.building.build_main import Analysis
from PyInstaller.building.osx import BUNDLE
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

## SPECPATH is injected by PyInstaller into the spec file's exec namespace
## (a plain `__file__` isn't set when it `exec`s this file) — it's the
## directory containing this .spec file.
ROOT = os.path.dirname(os.path.abspath(SPECPATH))  # noqa: F821
ICON_WINDOWS = os.path.join(ROOT, "packaging", "windows", "deskai.ico")
ICON_MACOS = os.path.join(ROOT, "packaging", "macos", "deskai.icns")

with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
    VERSION = tomllib.load(f)["project"]["version"]

datas = []
datas += collect_data_files("rapidocr_onnxruntime")
datas += collect_data_files("pymupdf")
datas += collect_data_files("botocore")  # service models — boto3 can't run without these
datas += [
    (
        os.path.join(ROOT, "src", "deskai", "skills", "builtin"),
        os.path.join("deskai", "skills", "builtin"),
    )
]

a = Analysis(
    [os.path.join(ROOT, "src", "deskai", "__main__.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="deskai",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=ICON_WINDOWS if os.path.exists(ICON_WINDOWS) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="deskai",
)

# macOS only: wraps the one-folder build into deskai.app, which packaging/macos's
# dmg step then packages into a .dmg (ARCHITECTURE.md §14). Harmless to define on
# other platforms — BUNDLE just isn't invoked unless you build with Analysis'
# target set to darwin.
app = BUNDLE(
    coll,
    name="deskai.app",
    icon=ICON_MACOS if os.path.exists(ICON_MACOS) else None,
    bundle_identifier="com.deskai.app",
    info_plist={
        "LSUIElement": True,  # tray-only app: no Dock icon or menu bar
        "CFBundleShortVersionString": VERSION,
    },
)
