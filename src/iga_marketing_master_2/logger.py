"""logger.py — structured logging.

Provides the project-wide logger. Writes both a per-client `runs.log` (under
the active client's Working Library folder) and a global app log under
`platformdirs.user_log_dir`. Honors `--debug` to bump verbosity and capture
raw Claude requests/responses + Playwright traces under `debug/`.
"""

# TODO: implementation pending
