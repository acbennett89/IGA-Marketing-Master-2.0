"""secret_store.py — keyring wrapper for the Anthropic API key.

Thin wrapper around the `keyring` library (Windows Credential Manager backend,
DPAPI under the hood). First-run flow prompts for the API key and stores it
via `keyring.set_password("IGA Marketing Master", "anthropic_api_key", value)`.
Env var `ANTHROPIC_API_KEY` overrides the stored value for power-user / CI use.

Per Amendment #14 (PLAN-REVIEW.md): replaced raw `pywin32 win32crypt` calls
with `keyring` for ~3 lines of code instead of ~30, identical security model.
"""

# TODO: implementation pending
