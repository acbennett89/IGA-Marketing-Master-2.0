"""Tests for `iga_marketing_master_2.logger`."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from iga_marketing_master_2 import logger as logger_mod
from iga_marketing_master_2.config import Settings
from iga_marketing_master_2.logger import (
    LOG_FILE_BACKUP_COUNT,
    LOG_FILE_MAX_BYTES,
    LOG_FORMAT,
    ROOT_LOGGER_NAME,
    configure_logging,
    configure_run_log,
    get_logger,
)


def _settings(tmp_path: Path, *, debug: bool = False) -> Settings:
    """Build a Settings whose paths all live under tmp_path."""
    return Settings(
        debug=debug,
        working_library=tmp_path / "wl",
        user_config_dir=tmp_path / "cfg",
        user_data_dir=tmp_path / "data",
        playwright_profile=tmp_path / "data" / "playwright-profile",
        field_map_path=tmp_path / "Library" / "Epic Field Map.json",
        log_dir=tmp_path / "data" / "logs",
    )


@pytest.fixture(autouse=True)
def _reset_root_logger() -> None:
    """Strip handlers we control off the iga logger between tests."""
    yield
    root = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except OSError:
            pass
    run_logger = logging.getLogger(f"{ROOT_LOGGER_NAME}.run")
    for handler in list(run_logger.handlers):
        run_logger.removeHandler(handler)
        try:
            handler.close()
        except OSError:
            pass


def test_log_constants_match_architecture_appendix_a() -> None:
    assert LOG_FILE_MAX_BYTES == 10 * 1024 * 1024
    assert LOG_FILE_BACKUP_COUNT == 5
    assert "%(asctime)s" in LOG_FORMAT
    assert "%(name)s" in LOG_FORMAT
    assert "%(levelname)s" in LOG_FORMAT


def test_configure_logging_creates_log_dir(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert not settings.log_dir.exists()
    configure_logging(settings)
    assert settings.log_dir.is_dir()


def test_configure_logging_attaches_file_and_console_handlers(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    configure_logging(settings)
    root = logging.getLogger(ROOT_LOGGER_NAME)
    handler_types = {type(h).__name__ for h in root.handlers}
    assert "RotatingFileHandler" in handler_types
    assert "StreamHandler" in handler_types


def test_root_logger_level_debug_when_debug_true(tmp_path: Path) -> None:
    settings = _settings(tmp_path, debug=True)
    configure_logging(settings)
    root = logging.getLogger(ROOT_LOGGER_NAME)
    assert root.level == logging.DEBUG


def test_root_logger_level_info_when_debug_false(tmp_path: Path) -> None:
    settings = _settings(tmp_path, debug=False)
    configure_logging(settings)
    root = logging.getLogger(ROOT_LOGGER_NAME)
    assert root.level == logging.INFO


def test_console_level_warning_when_debug_false(tmp_path: Path) -> None:
    settings = _settings(tmp_path, debug=False)
    configure_logging(settings)
    root = logging.getLogger(ROOT_LOGGER_NAME)
    stream_handlers = [h for h in root.handlers if type(h).__name__ == "StreamHandler"]
    assert stream_handlers
    assert stream_handlers[0].level == logging.WARNING


def test_console_level_debug_when_debug_true(tmp_path: Path) -> None:
    settings = _settings(tmp_path, debug=True)
    configure_logging(settings)
    root = logging.getLogger(ROOT_LOGGER_NAME)
    stream_handlers = [h for h in root.handlers if type(h).__name__ == "StreamHandler"]
    assert stream_handlers
    assert stream_handlers[0].level == logging.DEBUG


def test_configure_logging_is_idempotent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    configure_logging(settings)
    configure_logging(settings)
    configure_logging(settings)
    root = logging.getLogger(ROOT_LOGGER_NAME)
    file_handlers = [h for h in root.handlers if type(h).__name__ == "RotatingFileHandler"]
    stream_handlers = [h for h in root.handlers if type(h).__name__ == "StreamHandler"]
    assert len(file_handlers) == 1
    assert len(stream_handlers) == 1


def test_get_logger_namespaces_under_iga(tmp_path: Path) -> None:
    log = get_logger("config")
    assert log.name == "iga.config"


def test_get_logger_passes_through_iga_dotted_names() -> None:
    log = get_logger("iga.run")
    assert log.name == "iga.run"


def test_get_logger_empty_name_returns_root() -> None:
    log = get_logger("")
    assert log.name == ROOT_LOGGER_NAME


def test_log_file_actually_writes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    configure_logging(settings)
    log = get_logger("test_module")
    log.error("canary message for test")
    # Force flush.
    for handler in logging.getLogger(ROOT_LOGGER_NAME).handlers:
        handler.flush()
    log_file = settings.log_dir / "iga.log"
    assert log_file.exists()
    contents = log_file.read_text(encoding="utf-8")
    assert "canary message for test" in contents
    assert "iga.test_module" in contents


def test_root_logger_does_not_propagate(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    configure_logging(settings)
    root = logging.getLogger(ROOT_LOGGER_NAME)
    assert root.propagate is False


def test_configure_run_log_attaches_file_handler(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    configure_logging(settings)
    client_path = settings.working_library / "Acme Corp"
    run_logger = configure_run_log(client_path)
    assert run_logger.name == "iga.run"
    handlers = [h for h in run_logger.handlers if type(h).__name__ == "FileHandler"]
    assert len(handlers) == 1
    assert (client_path / "runs.log").exists()


def test_configure_run_log_idempotent_for_same_client(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    configure_logging(settings)
    client_path = settings.working_library / "Acme Corp"
    configure_run_log(client_path)
    configure_run_log(client_path)
    configure_run_log(client_path)
    run_logger = logging.getLogger("iga.run")
    file_handlers = [h for h in run_logger.handlers if type(h).__name__ == "FileHandler"]
    assert len(file_handlers) == 1


def test_configure_run_log_replaces_when_client_changes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    configure_logging(settings)
    client_a = settings.working_library / "Client A"
    client_b = settings.working_library / "Client B"
    configure_run_log(client_a)
    configure_run_log(client_b)
    run_logger = logging.getLogger("iga.run")
    file_handlers = [h for h in run_logger.handlers if type(h).__name__ == "FileHandler"]
    assert len(file_handlers) == 1
    # Should now point at client_b's runs.log.
    base_filename = Path(file_handlers[0].baseFilename).resolve()
    assert base_filename == (client_b / "runs.log").resolve()


def test_run_log_writes_to_client_folder(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    configure_logging(settings)
    client_path = settings.working_library / "Acme Corp"
    run_logger = configure_run_log(client_path)
    run_logger.info("extraction started for run abc-123")
    for handler in run_logger.handlers:
        handler.flush()
    log_file = client_path / "runs.log"
    assert log_file.exists()
    contents = log_file.read_text(encoding="utf-8")
    assert "extraction started" in contents
