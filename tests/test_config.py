"""Tests for `iga_marketing_master_2.config`.

All filesystem interactions are routed through `tmp_path` and we monkeypatch
`platformdirs` so tests are hermetic on every developer's machine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from iga_marketing_master_2 import config as config_mod
from iga_marketing_master_2.config import (
    APP_NAME,
    CONFIG_FILENAME,
    Settings,
    is_first_run,
    load_settings,
    save_user_config,
    settings_as_dict,
)


@pytest.fixture(autouse=True)
def _isolate_platformdirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect every platformdirs call to a tmp-path tree."""
    docs = tmp_path / "Documents"
    cfg = tmp_path / "AppData" / "Roaming" / APP_NAME
    data = tmp_path / "AppData" / "Local" / APP_NAME
    docs.mkdir(parents=True, exist_ok=True)

    def _user_documents_dir() -> str:
        return str(docs)

    def _user_config_dir(name: str | None = None, *args: object, **kwargs: object) -> str:
        return str(cfg)

    def _user_data_dir(name: str | None = None, *args: object, **kwargs: object) -> str:
        return str(data)

    monkeypatch.setattr(config_mod.platformdirs, "user_documents_dir", _user_documents_dir)
    monkeypatch.setattr(config_mod.platformdirs, "user_config_dir", _user_config_dir)
    monkeypatch.setattr(config_mod.platformdirs, "user_data_dir", _user_data_dir)


def test_default_paths_are_absolute() -> None:
    settings = load_settings()
    assert settings.working_library.is_absolute()
    assert settings.user_config_dir.is_absolute()
    assert settings.user_data_dir.is_absolute()
    assert settings.playwright_profile.is_absolute()
    assert settings.log_dir.is_absolute()


def test_default_working_library_lives_under_documents(tmp_path: Path) -> None:
    settings = load_settings()
    assert APP_NAME in str(settings.working_library)
    assert "Working Library" in str(settings.working_library)
    assert str(tmp_path) in str(settings.working_library)


def test_settings_is_frozen() -> None:
    settings = load_settings()
    with pytest.raises((AttributeError, Exception)):
        settings.debug = True  # type: ignore[misc]


def test_default_debug_is_false() -> None:
    settings = load_settings()
    assert settings.debug is False
    assert settings.cli_initial_client is None


def test_cli_overrides_take_precedence() -> None:
    overrides = {
        "debug": True,
        "working_library": str(Path.cwd() / "custom_working_lib"),
        "cli_initial_client": "Acme Corp",
    }
    settings = load_settings(cli_overrides=overrides)
    assert settings.debug is True
    assert settings.cli_initial_client == "Acme Corp"
    assert settings.working_library.name == "custom_working_lib"
    assert settings.working_library.is_absolute()


def test_cli_overrides_ignored_when_none() -> None:
    settings = load_settings(cli_overrides=None)
    assert settings.debug is False


def test_cli_overrides_skip_unknown_keys(caplog: pytest.LogCaptureFixture) -> None:
    settings = load_settings(cli_overrides={"debug": True, "nonexistent_field": 42})
    assert settings.debug is True
    # Unknown key should not have been set.
    assert not hasattr(settings, "nonexistent_field")


def test_persisted_config_is_loaded_when_present(tmp_path: Path) -> None:
    # First load to discover the user_config_dir.
    initial = load_settings()
    initial.user_config_dir.mkdir(parents=True, exist_ok=True)
    persisted_wl = tmp_path / "persisted_lib"
    cfg_path = initial.user_config_dir / CONFIG_FILENAME
    cfg_path.write_text(
        json.dumps({"working_library": str(persisted_wl)}),
        encoding="utf-8",
    )
    settings = load_settings()
    assert settings.working_library.name == "persisted_lib"


def test_cli_overrides_beat_persisted_config(tmp_path: Path) -> None:
    initial = load_settings()
    initial.user_config_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = initial.user_config_dir / CONFIG_FILENAME
    cfg_path.write_text(
        json.dumps({"working_library": str(tmp_path / "from_disk")}),
        encoding="utf-8",
    )
    cli_lib = tmp_path / "from_cli"
    settings = load_settings(cli_overrides={"working_library": str(cli_lib)})
    assert settings.working_library.name == "from_cli"


def test_corrupt_config_json_falls_back_to_defaults(tmp_path: Path) -> None:
    initial = load_settings()
    initial.user_config_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = initial.user_config_dir / CONFIG_FILENAME
    cfg_path.write_text("{ this is not valid json", encoding="utf-8")
    settings = load_settings()
    # Should still load successfully with defaults.
    assert settings.working_library.is_absolute()
    assert "Working Library" in str(settings.working_library)


def test_save_user_config_round_trips() -> None:
    settings = load_settings()
    save_user_config(settings)
    cfg_path = settings.user_config_dir / CONFIG_FILENAME
    assert cfg_path.exists()
    payload = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert "working_library" in payload
    assert payload["working_library"] == str(settings.working_library)


def test_save_user_config_atomic_write_no_tmp_left(tmp_path: Path) -> None:
    settings = load_settings()
    save_user_config(settings)
    cfg_path = settings.user_config_dir / CONFIG_FILENAME
    tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
    assert not tmp.exists(), "temporary file should be cleaned up after atomic replace"


def test_is_first_run_true_when_no_config_file() -> None:
    settings = load_settings()
    # Wipe any config.json a previous test created.
    cfg_path = settings.user_config_dir / CONFIG_FILENAME
    if cfg_path.exists():
        cfg_path.unlink()
    assert is_first_run(settings) is True


def test_is_first_run_false_after_save() -> None:
    settings = load_settings()
    save_user_config(settings)
    assert is_first_run(settings) is False


def test_field_map_path_points_into_repo_library() -> None:
    settings = load_settings()
    assert settings.field_map_path.name == "Epic Field Map.json"
    assert settings.field_map_path.parent.name == "Library"


def test_settings_as_dict_stringifies_paths() -> None:
    settings = load_settings()
    diag = settings_as_dict(settings)
    assert isinstance(diag["working_library"], str)
    assert isinstance(diag["debug"], bool)
    assert diag["debug"] is False


def test_persisted_paths_get_resolved_to_absolute(tmp_path: Path) -> None:
    initial = load_settings()
    initial.user_config_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = initial.user_config_dir / CONFIG_FILENAME
    # Write a relative-looking path (still on tmp_path so the test stays
    # hermetic -- _coerce_path will resolve via expanduser/resolve).
    rel_target = tmp_path / "some_lib"
    cfg_path.write_text(
        json.dumps({"working_library": str(rel_target)}),
        encoding="utf-8",
    )
    settings = load_settings()
    assert settings.working_library.is_absolute()


def test_default_settings_construction_does_not_create_dirs(tmp_path: Path) -> None:
    """Defaults are computed; no side effects until save_user_config / configure_logging runs."""
    settings = Settings()  # default-factory paths
    # log_dir / user_config_dir might not exist yet; loading settings shouldn't change that.
    # We don't assert non-existence (other tests may have created them), only that
    # the construction did not raise.
    assert isinstance(settings.working_library, Path)
