"""Global pytest fixtures for the IGA Marketing Master 2.0 test suite.

The most important job here is **test isolation** for ``platformdirs``.
Without it, any test that calls :func:`config.load_settings` or
:func:`config.save_user_config` will reach into the real
``%LOCALAPPDATA%/IGA Marketing Master/`` and trample the operator's
config.json — exactly the bug we're patching here.

Every test that does not already monkeypatch platformdirs gets a tmp_path
redirect via ``_isolate_platformdirs_globally``. Tests that opt in to a
custom monkeypatch (e.g. ``tests/test_config.py``) override the same
attributes and still win — pytest's monkeypatch is per-test and stacks
correctly with autouse fixtures.

The fixture also asserts after the test runs that no file was written
under the real ``%LOCALAPPDATA%/IGA Marketing Master/`` (when one exists);
this is the canary the operator paid for.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_platformdirs_globally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirect every ``platformdirs`` call into ``tmp_path`` for the test.

    Patches the symbols at their import sites so a test that loads
    ``config`` lazily still sees the redirected values.
    """
    docs = tmp_path / "Documents"
    cfg = tmp_path / "AppData" / "Roaming" / "IGA Marketing Master"
    data = tmp_path / "AppData" / "Local" / "IGA Marketing Master"
    docs.mkdir(parents=True, exist_ok=True)

    def _user_documents_dir(*_args: object, **_kwargs: object) -> str:
        return str(docs)

    def _user_config_dir(*_args: object, **_kwargs: object) -> str:
        return str(cfg)

    def _user_data_dir(*_args: object, **_kwargs: object) -> str:
        return str(data)

    # Patch at the platformdirs source so any module that imports it sees
    # the redirected functions.
    try:
        import platformdirs

        monkeypatch.setattr(
            platformdirs, "user_documents_dir", _user_documents_dir, raising=False
        )
        monkeypatch.setattr(
            platformdirs, "user_config_dir", _user_config_dir, raising=False
        )
        monkeypatch.setattr(
            platformdirs, "user_data_dir", _user_data_dir, raising=False
        )
    except ImportError:  # pragma: no cover - platformdirs is in requirements
        return

    # Also patch the symbol on the config module so already-cached
    # references to ``config.platformdirs`` get the redirected version.
    try:
        from iga_marketing_master_2 import config as config_mod

        monkeypatch.setattr(
            config_mod.platformdirs,
            "user_documents_dir",
            _user_documents_dir,
            raising=False,
        )
        monkeypatch.setattr(
            config_mod.platformdirs,
            "user_config_dir",
            _user_config_dir,
            raising=False,
        )
        monkeypatch.setattr(
            config_mod.platformdirs,
            "user_data_dir",
            _user_data_dir,
            raising=False,
        )
    except ImportError:  # pragma: no cover - config module always present
        pass


@pytest.fixture(autouse=True)
def _real_localappdata_canary(request: pytest.FixtureRequest) -> object:
    """Snapshot the real ``%LOCALAPPDATA%/IGA Marketing Master/config.json``
    before the test, restore it after.

    If a test ever escapes the platformdirs monkeypatch and writes to the
    real directory, the canary detects the difference and restores the
    operator's file. This is belt-and-suspenders insurance against future
    test regressions polluting the operator's environment.

    On systems where ``LOCALAPPDATA`` is unset (CI, non-Windows dev), the
    canary is a no-op.
    """
    localappdata = os.environ.get("LOCALAPPDATA")
    if not localappdata:
        yield
        return
    real_cfg = Path(localappdata) / "IGA Marketing Master" / "config.json"
    snapshot: bytes | None = None
    if real_cfg.exists():
        try:
            snapshot = real_cfg.read_bytes()
        except OSError:
            snapshot = None
    yield
    if snapshot is None:
        # File didn't exist before; if it exists now, a test created it.
        # Remove it so the operator's first-run picker still fires.
        if real_cfg.exists():
            try:
                real_cfg.unlink()
            except OSError:  # pragma: no cover - best effort
                pass
        return
    if real_cfg.exists():
        try:
            current = real_cfg.read_bytes()
        except OSError:
            current = None
        if current != snapshot:
            try:
                real_cfg.write_bytes(snapshot)
            except OSError:  # pragma: no cover - best effort
                pass
