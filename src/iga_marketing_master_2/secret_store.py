"""secret_store.py — keyring wrapper for the Anthropic API key.

Thin wrapper around the `keyring` library. On Windows, `keyring` selects
the `WinVaultKeyring` backend, which writes through Windows Credential
Manager (DPAPI under the hood). No raw `pywin32` calls live in this module.

Retrieval order (per ARCHITECTURE.md §9.3):
    1. `os.environ[ENV_OVERRIDE_API_KEY]` if non-empty
    2. `keyring.get_password(SECRET_SERVICE_NAME, SECRET_USERNAME_API_KEY)`
    3. `None` (caller decides whether to prompt or raise)

The architecture's listed `prompt_for_anthropic_api_key_via_gui` function
is NOT defined here. Doing so would require importing PySide6 from
`secret_store`, which would violate §11's dependency graph
(`gui -> secret_store`, never the reverse). Instead we expose
`prompt_for_anthropic_api_key_via_console` for headless / CI use, and
the GUI agent owns its own `ApiKeyPromptDialog` (an `OperatorModal`)
that calls `set_anthropic_api_key` on accept. Flagged in
STATUS_config-and-cli-agent.md.
"""

from __future__ import annotations

import os
from getpass import getpass

import keyring
from keyring.errors import KeyringError

from .logger import get_logger

__all__ = [
    "ENV_OVERRIDE_API_KEY",
    "SECRET_SERVICE_NAME",
    "SECRET_USERNAME_API_KEY",
    "SECRET_USERNAME_EPIC_USERCODE",
    "SECRET_USERNAME_EPIC_PASSWORD",
    "SecretStoreError",
    "delete_anthropic_api_key",
    "get_anthropic_api_key",
    "get_epic_credentials",
    "set_epic_credentials",
    "prompt_for_anthropic_api_key_via_console",
    "set_anthropic_api_key",
]

# Public constants — Architecture Appendix A. ------------------------------
SECRET_SERVICE_NAME: str = "IGA Marketing Master"
SECRET_USERNAME_API_KEY: str = "anthropic_api_key"
SECRET_USERNAME_EPIC_USERCODE: str = "epic_usercode"
SECRET_USERNAME_EPIC_PASSWORD: str = "epic_password"
ENV_OVERRIDE_API_KEY: str = "ANTHROPIC_API_KEY"

_logger = get_logger("secret_store")


class SecretStoreError(Exception):
    """Raised when the underlying keyring backend fails."""


def get_anthropic_api_key() -> str | None:
    """Return the active Anthropic API key, or None if unset.

    Order:
        1. `ANTHROPIC_API_KEY` env var (if non-empty)
        2. Keyring entry under (`IGA Marketing Master`, `anthropic_api_key`)
        3. None
    """
    # Env var wins so devs can run with a throwaway key without overwriting
    # the operator's stored key in Credential Manager.
    env_value = os.environ.get(ENV_OVERRIDE_API_KEY, "")
    if env_value.strip():
        _logger.debug("api key resolved from env var %s", ENV_OVERRIDE_API_KEY)
        return env_value
    try:
        stored = keyring.get_password(SECRET_SERVICE_NAME, SECRET_USERNAME_API_KEY)
    except KeyringError as exc:
        _logger.error("keyring backend error on get_password: %s", exc)
        raise SecretStoreError(f"keyring read failed: {exc}") from exc
    if stored:
        _logger.debug("api key resolved from keyring")
        return stored
    return None


def set_anthropic_api_key(value: str) -> None:
    """Store the Anthropic API key in the keyring.

    `value` must be a non-empty string. Whitespace-only inputs are rejected
    so a stray Enter at a prompt doesn't silently clear the key.
    """
    # A stray Enter at a console prompt would otherwise wipe the stored key.
    if not isinstance(value, str) or not value.strip():
        raise ValueError("api key must be a non-empty string")
    try:
        keyring.set_password(SECRET_SERVICE_NAME, SECRET_USERNAME_API_KEY, value)
    except KeyringError as exc:
        _logger.error("keyring backend error on set_password: %s", exc)
        raise SecretStoreError(f"keyring write failed: {exc}") from exc
    _logger.info("api key written to keyring (service=%s)", SECRET_SERVICE_NAME)


def delete_anthropic_api_key() -> None:
    """Remove the Anthropic API key from the keyring.

    No-op if no key is stored (suppresses `PasswordDeleteError`). Used by
    the GUI's "change my key" / re-prompt flows.
    """
    try:
        keyring.delete_password(SECRET_SERVICE_NAME, SECRET_USERNAME_API_KEY)
        _logger.info("api key removed from keyring")
    except KeyringError as exc:
        # `PasswordDeleteError` is a subclass of `KeyringError`. Treat
        # "doesn't exist" as success; raise on other failures.
        if "not found" in str(exc).lower() or "no such" in str(exc).lower():
            _logger.debug("delete_anthropic_api_key: no key was stored")
            return
        _logger.error("keyring backend error on delete_password: %s", exc)
        raise SecretStoreError(f"keyring delete failed: {exc}") from exc


def get_epic_credentials() -> tuple[str, str] | None:
    """Return (usercode, password) from the keyring, or None if not stored.

    Credentials are stored under the same Windows Credential Manager service
    name as the Anthropic API key, keyed by ``epic_usercode`` /
    ``epic_password``. Returns None when either value is missing so the
    caller can prompt the operator to run set_epic_credentials first.
    """
    try:
        usercode = keyring.get_password(SECRET_SERVICE_NAME, SECRET_USERNAME_EPIC_USERCODE)
        password = keyring.get_password(SECRET_SERVICE_NAME, SECRET_USERNAME_EPIC_PASSWORD)
    except KeyringError as exc:
        _logger.error("keyring backend error reading EPIC credentials: %s", exc)
        raise SecretStoreError(f"keyring read failed: {exc}") from exc
    if usercode and password:
        _logger.debug("EPIC credentials resolved from keyring")
        return usercode, password
    return None


def set_epic_credentials(usercode: str, password: str) -> None:
    """Store EPIC usercode and password in Windows Credential Manager.

    Both values must be non-empty strings. Call this once from a setup
    utility or the GUI's settings dialog — the persistent Playwright profile
    should only need it on the very first login.
    """
    if not usercode or not usercode.strip():
        raise ValueError("EPIC usercode must be a non-empty string")
    if not password or not password.strip():
        raise ValueError("EPIC password must be a non-empty string")
    try:
        keyring.set_password(SECRET_SERVICE_NAME, SECRET_USERNAME_EPIC_USERCODE, usercode)
        keyring.set_password(SECRET_SERVICE_NAME, SECRET_USERNAME_EPIC_PASSWORD, password)
    except KeyringError as exc:
        _logger.error("keyring backend error writing EPIC credentials: %s", exc)
        raise SecretStoreError(f"keyring write failed: {exc}") from exc
    _logger.info("EPIC credentials written to keyring (service=%s)", SECRET_SERVICE_NAME)


def prompt_for_anthropic_api_key_via_console(
    *,
    set_after: bool = True,
) -> str | None:
    """Console-fallback API-key prompt (no Qt).

    Used by the bootstrap script, CI, and any code path running before the
    GUI is available. Reads from stdin via `getpass` so the key is not
    echoed. On non-interactive stdin, returns None without raising.

    If `set_after` is True (default) and the user enters a non-empty value,
    the key is written to the keyring before returning.
    """
    try:
        entered = getpass("Enter your Anthropic API key (input hidden): ")
    except (EOFError, KeyboardInterrupt):
        _logger.info("api key prompt cancelled")
        return None
    if not entered or not entered.strip():
        _logger.info("api key prompt: empty input; not stored")
        return None
    if set_after:
        set_anthropic_api_key(entered)
    return entered
