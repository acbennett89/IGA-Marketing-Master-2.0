"""config.py — runtime configuration and path discovery.

Owns all path resolution via `platformdirs` and the immutable Settings
dataclass that downstream modules consume.

Discovery order (highest priority first):
    1. cli_overrides (passed from cli.py argv parsing)
    2. <user_config_dir>/config.json (persisted user choices)
    3. defaults computed via platformdirs

See ARCHITECTURE.md §9.2 for the contract; §13 #10 for first-run / config
persistence ruling; §14 #4 for log_dir retention (resolved by config-and-cli-agent
in DECISION-MAP-config-and-cli-agent.md as 10 MB x 5 backups, ~50 MB envelope).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import platformdirs

__all__ = [
    "APP_NAME",
    "CONFIG_FILENAME",
    "Settings",
    "default_field_map_path",
    "default_log_dir",
    "default_playwright_profile",
    "default_user_config_dir",
    "default_user_data_dir",
    "default_working_library",
    "load_settings",
    "save_user_config",
]

# Public constants ----------------------------------------------------------

APP_NAME: str = "IGA Marketing Master"
CONFIG_FILENAME: str = "config.json"

# Subset of Settings fields that get persisted to disk via save_user_config().
# All other Settings fields are computed at load time from platformdirs / cwd.
_PERSISTABLE_KEYS: tuple[str, ...] = ("working_library",)

_logger = logging.getLogger("iga.config")


# Default-path computations -------------------------------------------------


def default_working_library() -> Path:
    """Default Working Library location.

    Path B (single-user, local): `<Documents>/IGA Marketing Master/Working Library`.
    Per amendment #13 (PLAN-REVIEW.md) and §9.2.
    """
    return Path(platformdirs.user_documents_dir()) / APP_NAME / "Working Library"


def default_user_config_dir() -> Path:
    """`platformdirs.user_config_dir(APP_NAME)` — persisted user config + state."""
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False))


def default_user_data_dir() -> Path:
    """`platformdirs.user_data_dir(APP_NAME)` — runtime app data (logs, profile)."""
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


def default_playwright_profile() -> Path:
    """Persistent Chromium user-data-dir for Playwright. Always absolute."""
    return default_user_data_dir() / "playwright-profile"


def default_log_dir() -> Path:
    """`<user_data_dir>/logs` — rotating app log lives here."""
    return default_user_data_dir() / "logs"


def default_field_map_path() -> Path:
    """`<repo_root>/Library/Epic Field Map.json`.

    Computed by walking up from this module's directory:
    `src/iga_marketing_master_2/config.py` -> `<repo_root>`.
    """
    module_path = Path(__file__).resolve()
    # config.py -> iga_marketing_master_2 -> src -> repo root
    repo_root = module_path.parent.parent.parent
    return repo_root / "Library" / "Epic Field Map.json"


# Settings dataclass --------------------------------------------------------


@dataclass(slots=True, kw_only=True, frozen=True)
class Settings:
    """Immutable runtime configuration. Constructed by `load_settings`.

    All Path fields are absolute. The dataclass is frozen so downstream
    modules can rely on its immutability after the CLI builds it.
    """

    debug: bool = False
    working_library: Path = field(default_factory=default_working_library)
    user_config_dir: Path = field(default_factory=default_user_config_dir)
    user_data_dir: Path = field(default_factory=default_user_data_dir)
    playwright_profile: Path = field(default_factory=default_playwright_profile)
    field_map_path: Path = field(default_factory=default_field_map_path)
    log_dir: Path = field(default_factory=default_log_dir)
    cli_initial_client: str | None = None  # from --client; advisory for the GUI


# Helpers -------------------------------------------------------------------


def _coerce_path(value: Any) -> Path:
    """Coerce a JSON / CLI string value into an absolute `Path`."""
    if isinstance(value, Path):
        return value.expanduser().resolve()
    if isinstance(value, str):
        return Path(value).expanduser().resolve()
    raise TypeError(f"expected str or Path, got {type(value).__name__}")


def _read_persisted_config(config_path: Path) -> dict[str, Any]:
    """Read `<user_config_dir>/config.json` if present; return {} on miss / parse error."""
    if not config_path.exists():
        return {}
    try:
        raw = config_path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            _logger.warning("config.json not a JSON object; ignoring")
            return {}
        return parsed
    except (OSError, json.JSONDecodeError) as exc:
        _logger.warning("failed to read config.json (%s); using defaults", exc)
        return {}


def _apply_overrides(
    base: Settings,
    overrides: dict[str, Any],
) -> Settings:
    """Return a new Settings with `overrides` applied. Path fields are coerced."""
    if not overrides:
        return base
    valid_keys = {f.name for f in fields(Settings)}
    patched: dict[str, Any] = {}
    for key, value in overrides.items():
        if key not in valid_keys:
            _logger.warning("ignoring unknown settings key %r", key)
            continue
        if value is None:
            continue
        # Path coercion for fields we know are Paths.
        path_keys = {
            "working_library",
            "user_config_dir",
            "user_data_dir",
            "playwright_profile",
            "field_map_path",
            "log_dir",
        }
        if key in path_keys:
            patched[key] = _coerce_path(value)
        else:
            patched[key] = value
    return replace(base, **patched)


# Public API ----------------------------------------------------------------


def load_settings(
    *,
    cli_overrides: dict[str, Any] | None = None,
) -> Settings:
    """Build a Settings object from defaults, persisted config, and CLI overrides.

    Discovery order (highest priority first):
        1. cli_overrides
        2. <user_config_dir>/config.json
        3. platformdirs-computed defaults

    The first call may be invoked before `logger.configure_logging`; we use
    a module-level logger that defers to root config when one is present.
    """
    # Discovery order is intentional: defaults form the floor, the user's
    # saved preferences in config.json overlay them, and CLI flags win last
    # so power users can override anything for one run without persisting.

    # 1. Defaults via field_factory.
    settings = Settings()

    # 2. Persisted config overlay.
    persisted = _read_persisted_config(settings.user_config_dir / CONFIG_FILENAME)
    if persisted:
        settings = _apply_overrides(settings, persisted)

    # 3. CLI overrides win.
    if cli_overrides:
        settings = _apply_overrides(settings, cli_overrides)

    # Belt-and-suspenders: re-coerce all Paths through _apply_overrides so a
    # hand-edited config.json with a relative path can't slip through. The
    # platformdirs defaults are already absolute; this is for the rest.
    settings = _apply_overrides(
        settings,
        {
            "working_library": settings.working_library,
            "user_config_dir": settings.user_config_dir,
            "user_data_dir": settings.user_data_dir,
            "playwright_profile": settings.playwright_profile,
            "field_map_path": settings.field_map_path,
            "log_dir": settings.log_dir,
        },
    )
    return settings


def save_user_config(settings: Settings) -> None:
    """Persist the user-configurable subset of `settings` to disk.

    Writes to `<user_config_dir>/config.json` atomically (tmp + os.replace).
    Only the keys in `_PERSISTABLE_KEYS` are written.
    """
    cfg_dir = settings.user_config_dir
    cfg_dir.mkdir(parents=True, exist_ok=True)
    target = cfg_dir / CONFIG_FILENAME
    payload = {
        key: str(getattr(settings, key)) if isinstance(getattr(settings, key), Path) else getattr(settings, key)
        for key in _PERSISTABLE_KEYS
    }
    # Atomic-write idiom: write to a sibling .tmp file, then os.replace. This
    # guarantees the operator never sees a half-written config.json after a
    # power cut or crash mid-write.
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, target)
        _logger.info("saved user config to %s", target)
    except OSError:
        # If the write failed mid-flight, don't leave the .tmp turd behind.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def is_first_run(settings: Settings) -> bool:
    """True when no persisted config.json exists for this user.

    Used by the GUI to decide whether to show the first-run picker.
    """
    return not (settings.user_config_dir / CONFIG_FILENAME).exists()


def settings_as_dict(settings: Settings) -> dict[str, Any]:
    """Diagnostic helper — returns Settings as a JSON-friendly dict.

    Path values are stringified. Used by `--debug` startup logging.
    """
    raw = asdict(settings)
    return {
        k: str(v) if isinstance(v, Path) else v
        for k, v in raw.items()
    }
