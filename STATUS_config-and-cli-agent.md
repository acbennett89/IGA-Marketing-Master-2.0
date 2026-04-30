# STATUS — config-and-cli-agent

**Agent:** config-and-cli-agent
**Date:** 2026-04-30
**Phase:** 1 (Foundation)
**Status:** Green light — implementation complete and within scope.
**Authoritative reference:** [ARCHITECTURE.md](ARCHITECTURE.md) §§ 2, 9, 10, 13, 14
**Decision map:** [DECISION-MAP-config-and-cli-agent.md](DECISION-MAP-config-and-cli-agent.md)

---

## 1. Files written

| File | Status | Purpose |
|---|---|---|
| `src/iga_marketing_master_2/cli.py` | written (replaces placeholder) | argparse CLI + GUI handoff |
| `src/iga_marketing_master_2/config.py` | written (replaces placeholder) | `Settings` dataclass + path discovery + persisted config I/O |
| `src/iga_marketing_master_2/logger.py` | written (replaces placeholder) | `iga.*` logger tree, rotating file + console + per-client run log |
| `src/iga_marketing_master_2/secret_store.py` | written (replaces placeholder) | `keyring`-backed Anthropic API key wrapper |
| `scripts/bootstrap.ps1` | written (replaces placeholder) | idempotent Windows setup script |
| `tests/test_config.py` | new | 17 tests, hermetic via tmp_path + monkeypatched platformdirs |
| `tests/test_logger.py` | new | 17 tests, hermetic via tmp_path |
| `tests/test_secret_store.py` | new | 18 tests, hermetic via fake-keyring monkeypatch |
| `DECISION-MAP-config-and-cli-agent.md` | new | Mermaid flowcharts for every decision in scope |
| `STATUS_config-and-cli-agent.md` | new | this file |

No files outside the prompt's allowed list were modified. `pyproject.toml`, `requirements.txt`, `__init__.py`, and `ARCHITECTURE.md` were left as-is.

---

## 2. Public API surface (signatures)

### `config.py`

```python
APP_NAME: str = "IGA Marketing Master"
CONFIG_FILENAME: str = "config.json"

@dataclass(slots=True, kw_only=True, frozen=True)
class Settings:
    debug: bool = False
    working_library: Path = ...
    user_config_dir: Path = ...
    user_data_dir: Path = ...
    playwright_profile: Path = ...
    field_map_path: Path = ...
    log_dir: Path = ...
    cli_initial_client: str | None = None

def load_settings(*, cli_overrides: dict[str, Any] | None = None) -> Settings: ...
def save_user_config(settings: Settings) -> None: ...
def is_first_run(settings: Settings) -> bool: ...
def settings_as_dict(settings: Settings) -> dict[str, Any]: ...

def default_working_library() -> Path: ...
def default_user_config_dir() -> Path: ...
def default_user_data_dir() -> Path: ...
def default_playwright_profile() -> Path: ...
def default_log_dir() -> Path: ...
def default_field_map_path() -> Path: ...
```

### `logger.py`

```python
LOG_FORMAT: str = "%(asctime)s %(levelname)s %(name)s %(message)s"
LOG_FILE_MAX_BYTES: int = 10 * 1024 * 1024
LOG_FILE_BACKUP_COUNT: int = 5
ROOT_LOGGER_NAME: str = "iga"

def configure_logging(settings: Settings) -> None: ...
def get_logger(name: str) -> logging.Logger: ...
def configure_run_log(client_path: Path) -> logging.Logger: ...
```

### `secret_store.py`

```python
SECRET_SERVICE_NAME: str = "IGA Marketing Master"
SECRET_USERNAME_API_KEY: str = "anthropic_api_key"
ENV_OVERRIDE_API_KEY: str = "ANTHROPIC_API_KEY"

class SecretStoreError(Exception): ...

def get_anthropic_api_key() -> str | None: ...
def set_anthropic_api_key(value: str) -> None: ...
def delete_anthropic_api_key() -> None: ...
def prompt_for_anthropic_api_key_via_console(*, set_after: bool = True) -> str | None: ...
```

### `cli.py`

```python
def build_parser() -> argparse.ArgumentParser: ...
def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace: ...
def settings_from_args(args: argparse.Namespace) -> Settings: ...
def main(argv: Sequence[str] | None = None) -> int: ...
```

CLI surface: `iga-marketing-master-2 [--debug] [--working-library PATH] [--client NAME] [--version]`.

### `scripts/bootstrap.ps1`

Idempotent. Steps: verify Python 3.13 via the `py` launcher; create `.venv` if missing (skip otherwise); upgrade pip; `pip install -r requirements.txt`; `playwright install chromium`; `pip install -e .`; health-check import. Color-coded `[OK]/[--]/[!!]` messages; non-zero exit on any failure.

---

## 3. Test count + coverage notes

- **52 tests total** across the three test files (17 + 17 + 18).
- All tests hermetic — no real keyring writes, no real platformdirs lookups, no network calls. Each test fixture redirects platformdirs / keyring / `getpass` to in-memory or `tmp_path` substitutes.
- All files pass `ast.parse` under Python 3.13.12 (verified locally).
- Per the prompt I did NOT run `pip install`, so I could not actually invoke pytest. The tests are written to standard pytest conventions and will run as-is once `pytest` is installed via the bootstrap script (see Flag #2 below).

Coverage by module (each public symbol exercised by at least one test):

| Module | Public functions | Covered |
|---|---|---|
| `config.py` | `load_settings`, `save_user_config`, `is_first_run`, `settings_as_dict`, all default-* helpers | yes (defaults, CLI overrides, persisted config, corrupt JSON, atomic write, round-trip) |
| `logger.py` | `configure_logging`, `get_logger`, `configure_run_log` | yes (level routing under debug on/off, idempotency, namespacing, run-log replacement on client change) |
| `secret_store.py` | `get/set/delete_anthropic_api_key`, `prompt_for_anthropic_api_key_via_console` | yes (env-vs-keyring precedence, whitespace handling, KeyringError wrapping, console prompt with stdin/EOF/empty paths) |

`cli.main` itself is not unit-tested with full execution because the GUI import would require PySide6 at test time. `build_parser` / `parse_args` / `settings_from_args` are testable surfaces (and trivially covered by config tests via the `settings_from_args` shape) but I did not add a separate `test_cli.py` since the prompt's mandated test file list is `test_config.py / test_logger.py / test_secret_store.py`. Adding a `test_cli.py` is straightforward future work for the gui-agent or a follow-up.

---

## 4. Decisions logged (Open Items resolved)

### §14 #4 — `log_dir` retention specifics — RESOLVED

Chosen: `RotatingFileHandler(maxBytes=10 MB, backupCount=5)`. Total envelope: ~50 MB. Honors Architecture Appendix A constants. Documented in `logger.py` module docstring and DECISION-MAP §D.

The prompt's recommendation of `5 MB × 10` produces the same total envelope; I chose 10×5 to match Appendix A exactly and reduce the number of rotated files (easier `tail -f`-style ergonomics).

### §14 #7 — `scripts/bootstrap.ps1` content — RESOLVED

Chosen design: see DECISION-MAP §E. Idempotent. Detects existing `.venv`, prints Python version after creation, verifies the version starts with `3.13`, and color-codes every step. Health-check at the end imports `iga_marketing_master_2` to catch broken installs immediately. All script-level errors set `$ErrorActionPreference = 'Stop'` and use `exit 1`.

Per-client `runs.log` retention is **not** rotated — the audit trail is bounded by run length and operators expect it to persist. Documented in DECISION-MAP §D.

---

## 5. Conflicts / Flags

### Flag #1 — `prompt_for_anthropic_api_key_via_gui` cannot live in `secret_store.py`

ARCHITECTURE.md §9.3 lists `prompt_for_anthropic_api_key_via_gui(parent_widget) -> str | None` under the `secret_store.py` public surface. But §11's Module Dependency Graph shows `gui → secret_store`, never the reverse, and `secret_store` is in the dependency cone of `claude_client` (which must not pull Qt into headless test environments).

**Best-guess resolution applied:** I removed the GUI-coupled function from `secret_store.py` and provided `prompt_for_anthropic_api_key_via_console` instead (for bootstrap / CI / headless paths). The GUI agent owns its own `ApiKeyPromptDialog` (an `OperatorModal`, per §8.3) and calls `set_anthropic_api_key` directly on accept. This keeps the dependency graph acyclic and the secret_store import-light.

If the architecture-agent disagrees, the resolution is straightforward: have `secret_store.prompt_for_anthropic_api_key_via_gui` accept a callable (`prompt_callback: Callable[[], str | None]`) provided by the GUI, so the GUI dependency is injected rather than imported. Either pattern is fine; I chose the simpler one.

### Flag #2 — `pytest` is not in `requirements.txt`

The Setup Agent did not include `pytest` in `requirements.txt` (which is sensible — it's a test-only dep, not a runtime dep). The bootstrap script therefore won't install pytest, and a developer running tests for the first time will need a separate install step.

I did NOT modify `requirements.txt` or `pyproject.toml` (not on my output-files list, and the prompt explicitly says no `pip install`). Suggested follow-up for the orchestrator: route a small task to add a `[project.optional-dependencies] test = ["pytest>=8"]` block to `pyproject.toml` and document `pip install -e ".[test]"` in `TROUBLESHOOTING.md`. This is a 3-line change, but it's outside my file scope.

### Flag #3 — `pywin32` is in `requirements.txt` but unused by my modules

Amendment #14 (PLAN-REVIEW.md) explicitly replaced raw `pywin32 win32crypt` calls with `keyring`. `keyring` on Windows uses the Credential Manager via its own `pywin32-ctypes` shim and does NOT require `pywin32`. The `pywin32` floor in `requirements.txt` is therefore not strictly needed for the secret store; it may still be useful for other modules (e.g., a future Office automation hook). Not removing it — out of scope, and harmless if unused.

### No conflicts with ARCHITECTURE.md beyond Flag #1.

Every contract value (`SECRET_SERVICE_NAME`, `SECRET_USERNAME_API_KEY`, `ENV_OVERRIDE_API_KEY`, `LOG_FORMAT`, `LOG_FILE_MAX_BYTES`, `LOG_FILE_BACKUP_COUNT`, `Settings` shape, CLI flag list, retrieval order) matches Appendix A and §9 exactly.

---

## 6. Deviations from ARCHITECTURE.md

### Deviation #1 — `Settings.cli_initial_client` field added (not in §9.2 contract)

§9.1 lists `--client NAME` as a CLI flag but §9.2's `Settings` dataclass does not name a field for it. I added `cli_initial_client: str | None = None` to `Settings` so the GUI can read the auto-select target without re-parsing argv. This is additive and does not change any other module's interface. If the architecture-agent prefers a different surface (e.g., a separate `RuntimeContext` object), happy to adjust.

### Deviation #2 — `prompt_for_anthropic_api_key_via_gui` not implemented in `secret_store.py`

See Flag #1 above. The GUI version is owned by gui-agent and is documented in §8.3 (`ApiKeyPromptDialog`).

### Deviation #3 — `RotatingFileHandler` only on the app log; per-client `runs.log` is not rotated

ARCHITECTURE.md does not mandate rotation on `runs.log`. I made the deliberate choice not to rotate per-client run logs because the audit trail is a feature. Documented in `logger.py` and DECISION-MAP §D.

---

## 7. What's ready for downstream agents

- **claude-client-agent / extract-agent** — can `from .config import Settings, load_settings`, `from .logger import get_logger, configure_run_log`, `from .secret_store import get_anthropic_api_key, SecretStoreError`. The retry-on-`ClaudeAuthError` flow can call `secret_store.get_anthropic_api_key()` post-prompt.
- **state-agent** — can `from .config import Settings` to discover the active Working Library, and `from .logger import get_logger, configure_run_log`.
- **gui-agent** — can wire its `IgaApp.run(debug: bool)` entry to `cli.main`. First-run picker should call `config.is_first_run(settings)` and `config.save_user_config(updated_settings)`. `ApiKeyPromptDialog` should call `secret_store.set_anthropic_api_key(value)`.
- **bootstrap-agent / human operator** — `scripts/bootstrap.ps1` is ready to run from a PowerShell prompt at the repo root.

---

## 8. Verification log

- `py -3.13 -m ast.parse` (via Python `ast.parse`) on all 7 source/test files: **all parse cleanly**.
- `[System.Management.Automation.Language.Parser]::ParseFile` on `scripts/bootstrap.ps1`: **OK: bootstrap.ps1 parses cleanly**.
- `pip install` + `pytest` execution: **NOT RUN** per prompt prohibition. The orchestrator or a human runs `scripts/bootstrap.ps1` then `pytest` to validate the test suite end-to-end.

Green light to proceed.
