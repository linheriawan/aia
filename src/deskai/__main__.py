"""Entry point: logging, the qasync event loop, single-instance check, tray."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import multiprocessing
import sys

import qasync
from PySide6.QtWidgets import QApplication

from deskai import paths
from deskai.app import AppController


def _setup_logging() -> None:
    log_dir = paths.logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "deskai.log", maxBytes=2_000_000, backupCount=5
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def main() -> int:
    _setup_logging()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    controller = AppController(app)
    if not controller.acquire_single_instance():
        return 0
    controller.start()

    with loop:
        loop.run_forever()
    return 0


if __name__ == "__main__":
    # Required before anything else touches multiprocessing (OcrEngine's
    # ProcessPoolExecutor): on a frozen Windows build, spawned worker
    # processes re-exec this same entry point, and without this they'd
    # re-run the whole app instead of just the worker (ARCHITECTURE.md §9,
    # §14 — PyInstaller's own docs call this out for exactly this reason).
    multiprocessing.freeze_support()
    raise SystemExit(main())
