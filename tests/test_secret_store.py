"""Tests for `iga_marketing_master_2.secret_store`.

The `keyring` backend is fully mocked via a fake in-memory implementation so
no real Windows Credential Manager / DPAPI calls happen during testing.
"""

from __future__ import annotations

from typing import Any

import pytest
from keyring.errors import KeyringError, PasswordDeleteError

from iga_marketing_master_2 import secret_store as secret_store_mod
from iga_marketing_master_2.secret_store import (
    ENV_OVERRIDE_API_KEY,
    SECRET_SERVICE_NAME,
    SECRET_USERNAME_API_KEY,
    SecretStoreError,
    delete_anthropic_api_key,
    get_anthropic_api_key,
    prompt_for_anthropic_api_key_via_console,
    set_anthropic_api_key,
)


class _FakeKeyring:
    """In-memory keyring stand-in used by tests.

    The module-under-test calls `keyring.get_password` etc. directly. We swap
    those module-level functions out via monkeypatch.
    """

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.fail_get: Exception | None = None
        self.fail_set: Exception | None = None
        self.fail_delete: Exception | None = None

    def get_password(self, service: str, username: str) -> str | None:
        if self.fail_get is not None:
            raise self.fail_get
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, value: str) -> None:
        if self.fail_set is not None:
            raise self.fail_set
        self.store[(service, username)] = value

    def delete_password(self, service: str, username: str) -> None:
        if self.fail_delete is not None:
            raise self.fail_delete
        if (service, username) not in self.store:
            raise PasswordDeleteError("Password not found in this keyring.")
        del self.store[(service, username)]


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    fake = _FakeKeyring()
    monkeypatch.setattr(secret_store_mod.keyring, "get_password", fake.get_password)
    monkeypatch.setattr(secret_store_mod.keyring, "set_password", fake.set_password)
    monkeypatch.setattr(secret_store_mod.keyring, "delete_password", fake.delete_password)
    return fake


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_OVERRIDE_API_KEY, raising=False)


# Constants -----------------------------------------------------------------


def test_constants_match_architecture_appendix_a() -> None:
    assert SECRET_SERVICE_NAME == "IGA Marketing Master"
    assert SECRET_USERNAME_API_KEY == "anthropic_api_key"
    assert ENV_OVERRIDE_API_KEY == "ANTHROPIC_API_KEY"


# get_anthropic_api_key -----------------------------------------------------


def test_get_returns_none_when_unset(fake_keyring: _FakeKeyring) -> None:
    assert get_anthropic_api_key() is None


def test_get_prefers_env_var(
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_keyring.store[(SECRET_SERVICE_NAME, SECRET_USERNAME_API_KEY)] = "from-keyring"
    monkeypatch.setenv(ENV_OVERRIDE_API_KEY, "from-env")
    assert get_anthropic_api_key() == "from-env"


def test_get_falls_back_to_keyring_when_env_empty(
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_keyring.store[(SECRET_SERVICE_NAME, SECRET_USERNAME_API_KEY)] = "from-keyring"
    monkeypatch.setenv(ENV_OVERRIDE_API_KEY, "")
    assert get_anthropic_api_key() == "from-keyring"


def test_get_treats_whitespace_env_as_empty(
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_keyring.store[(SECRET_SERVICE_NAME, SECRET_USERNAME_API_KEY)] = "from-keyring"
    monkeypatch.setenv(ENV_OVERRIDE_API_KEY, "   ")
    assert get_anthropic_api_key() == "from-keyring"


def test_get_wraps_keyring_failure_in_secret_store_error(
    fake_keyring: _FakeKeyring,
) -> None:
    fake_keyring.fail_get = KeyringError("backend exploded")
    with pytest.raises(SecretStoreError):
        get_anthropic_api_key()


# set_anthropic_api_key -----------------------------------------------------


def test_set_stores_value(fake_keyring: _FakeKeyring) -> None:
    set_anthropic_api_key("sk-test-12345")
    assert (
        fake_keyring.store[(SECRET_SERVICE_NAME, SECRET_USERNAME_API_KEY)]
        == "sk-test-12345"
    )


def test_set_rejects_empty_string(fake_keyring: _FakeKeyring) -> None:
    with pytest.raises(ValueError):
        set_anthropic_api_key("")


def test_set_rejects_whitespace(fake_keyring: _FakeKeyring) -> None:
    with pytest.raises(ValueError):
        set_anthropic_api_key("   ")


def test_set_rejects_non_string(fake_keyring: _FakeKeyring) -> None:
    with pytest.raises(ValueError):
        set_anthropic_api_key(None)  # type: ignore[arg-type]


def test_set_wraps_keyring_failure(fake_keyring: _FakeKeyring) -> None:
    fake_keyring.fail_set = KeyringError("write blocked")
    with pytest.raises(SecretStoreError):
        set_anthropic_api_key("sk-test-12345")


# delete_anthropic_api_key --------------------------------------------------


def test_delete_removes_key(fake_keyring: _FakeKeyring) -> None:
    set_anthropic_api_key("sk-something")
    delete_anthropic_api_key()
    assert get_anthropic_api_key() is None


def test_delete_when_missing_is_a_noop(fake_keyring: _FakeKeyring) -> None:
    # No raise even when the entry doesn't exist.
    delete_anthropic_api_key()


def test_delete_wraps_other_keyring_errors(fake_keyring: _FakeKeyring) -> None:
    set_anthropic_api_key("sk-something")
    fake_keyring.fail_delete = KeyringError("something else broke")
    with pytest.raises(SecretStoreError):
        delete_anthropic_api_key()


# Console prompt ------------------------------------------------------------


def test_console_prompt_stores_entered_value(
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        secret_store_mod, "getpass", lambda prompt="": "sk-from-prompt"
    )
    result = prompt_for_anthropic_api_key_via_console()
    assert result == "sk-from-prompt"
    assert get_anthropic_api_key() == "sk-from-prompt"


def test_console_prompt_returns_none_on_empty_input(
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secret_store_mod, "getpass", lambda prompt="": "")
    result = prompt_for_anthropic_api_key_via_console()
    assert result is None
    assert get_anthropic_api_key() is None


def test_console_prompt_returns_none_on_eof(
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(prompt: str = "") -> str:
        raise EOFError("no stdin")

    monkeypatch.setattr(secret_store_mod, "getpass", _raise)
    result = prompt_for_anthropic_api_key_via_console()
    assert result is None


def test_console_prompt_set_after_false_does_not_store(
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secret_store_mod, "getpass", lambda prompt="": "sk-only-returned")
    result = prompt_for_anthropic_api_key_via_console(set_after=False)
    assert result == "sk-only-returned"
    assert get_anthropic_api_key() is None


# Round-trip ---------------------------------------------------------------


def test_set_then_get_round_trip(fake_keyring: _FakeKeyring) -> None:
    set_anthropic_api_key("sk-roundtrip")
    assert get_anthropic_api_key() == "sk-roundtrip"
    delete_anthropic_api_key()
    assert get_anthropic_api_key() is None
