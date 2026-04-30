"""logger.py — structured logging setup.

Configures the project-wide `iga` logger tree with two outputs:

- A rotating file handler at `<log_dir>/iga.log`, sized per Architecture
  Appendix A constants (`LOG_FILE_MAX_BYTES = 10 * 1024 * 1024`,
  `LOG_FILE_BACKUP_COUNT = 5`).  Total disk envelope: ~50 MB.
- A console (stderr) handler whose level depends on `Settings.debug`.

Per-client `runs.log` files are attached on demand via `configure_run_log`.
This per-client handler does not rotate (per-client audit trail is a feature,
not a budget item).

See ARCHITECTURE.md §9.4 for the contract; §10 for `--debug` discipline.
The retention policy is documented in DECISION-MAP-config-and-cli-agent.md
section D, resolving Open Item §14 #4.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Settings

__all__ = [
    "LOG_FILE_BACKUP_COUNT",
    "LOG_FILE_MAX_BYTES",
    "LOG_FORMAT",
    "ROOT_LOGGER_NAME",
    "configure_logging",
    "configure_run_log",
    "get_logger",
]

# Public constants — mirror Architecture Appendix A. ------------------------
LOG_FORMAT: str = "%(asctime)s %(levelname)s %(name)s %(message)s"
LOG_FILE_MAX_BYTES: int = 10 * 1024 * 1024  # 10 MB per file
LOG_FILE_BACKUP_COUNT: int = 5  # 5 backups -> ~50 MB total
ROOT_LOGGER_NAME: str = "iga"

_APP_LOG_FILENAME: str = "iga.log"
_RUN_LOG_FILENAME: str = "runs.log"

# Sentinel attribute names used to tag handlers we own. Lets us idempotently
# reconfigure logging without piling up duplicate handlers.
_APP_HANDLER_TAG: str = "_iga_app_handler"
_CONSOLE_HANDLER_TAG: str = "_iga_console_handler"
_RUN_HANDLER_TAG: str = "_iga_run_handler"


def _build_formatter() -> logging.Formatter:
    return logging.Formatter(LOG_FORMAT)


def _remove_tagged_handlers(logger: logging.Logger, tag: str) -> None:
    """Remove handlers we previously attached (identified by the given attr tag)."""
    to_remove = [h for h in logger.handlers if getattr(h, tag, False)]
    for handler in to_remove:
        logger.removeHandler(handler)
        try:
            handler.close()
        except OSError:
            pass


def configure_logging(settings: Settings) -> None:
    """Configure the `iga` logger tree.

    Idempotent — repeated calls reset the handlers we own without touching
    handlers attached by other code (e.g., pytest's caplog).
    """
    log_dir: Path = settings.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger(ROOT_LOGGER_NAME)
    root.setLevel(logging.DEBUG if settings.debug else logging.INFO)
    # Don't bubble to the bare root logger (avoids double-emission to any
    # ambient stderr handler the host environment installed — Qt and pytest
    # both like to attach their own).
    root.propagate = False

    # Idempotency guard: only remove handlers we tagged on a prior call. Hands
    # off whatever pytest's caplog or external code attached.
    _remove_tagged_handlers(root, _APP_HANDLER_TAG)
    _remove_tagged_handlers(root, _CONSOLE_HANDLER_TAG)

    formatter = _build_formatter()

    # Rotating file handler — always attached.
    file_handler = RotatingFileHandler(
        log_dir / _APP_LOG_FILENAME,
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG if settings.debug else logging.INFO)
    file_handler.setFormatter(formatter)
    setattr(file_handler, _APP_HANDLER_TAG, True)
    root.addHandler(file_handler)

    # Console handler — verbose under --debug, quieter otherwise.
    console_handler = logging.StreamHandler(stream=sys.stderr)
    console_handler.setLevel(logging.DEBUG if settings.debug else logging.WARNING)
    console_handler.setFormatter(formatter)
    setattr(console_handler, _CONSOLE_HANDLER_TAG, True)
    root.addHandler(console_handler)

    root.debug("logging configured: log_dir=%s debug=%s", log_dir, settings.debug)


def get_logger(name: str) -> logging.Logger:
    """Return `logging.getLogger("iga.<name>")`.

    Modules should call this at import time:
        logger = get_logger("config")  # -> "iga.config"
    """
    if not name:
        return logging.getLogger(ROOT_LOGGER_NAME)
    if name.startswith(f"{ROOT_LOGGER_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")


def configure_run_log(client_path: Path) -> logging.Logger:
    """Attach a per-client `runs.log` FileHandler to the `iga.run` logger.

    Idempotent for the given client path — calling repeatedly with the same
    `client_path` does not duplicate handlers. Calling with a new path
    replaces the previous run-log handler (one active client at a time, per
    Path B / single-user assumption).

    Returns the `iga.run` logger for convenience.
    """
    client_path.mkdir(parents=True, exist_ok=True)
    target = client_path / _RUN_LOG_FILENAME

    run_logger = logging.getLogger(f"{ROOT_LOGGER_NAME}.run")
    run_logger.setLevel(logging.INFO)
    # Bubble up to the parent "iga" logger so the global app log also captures
    # everything the per-client log captures — useful when comparing two
    # clients' runs in the same window.
    run_logger.propagate = True

    # Cheap re-call: same client, no work to do.
    for handler in run_logger.handlers:
        if getattr(handler, _RUN_HANDLER_TAG, False):
            current = getattr(handler, "baseFilename", None)
            if current and Path(current) == target.resolve():
                return run_logger

    # Switching clients: detach the prior run-log handler before we attach the
    # new one. Single active client at a time per Path B.
    _remove_tagged_handlers(run_logger, _RUN_HANDLER_TAG)

    run_handler = logging.FileHandler(target, encoding="utf-8")
    run_handler.setLevel(logging.INFO)
    run_handler.setFormatter(_build_formatter())
    setattr(run_handler, _RUN_HANDLER_TAG, True)
    run_logger.addHandler(run_handler)
    run_logger.info("attached run log: %s", target)
    return run_logger
