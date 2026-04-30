"""epic_session.py — Playwright persistent context lifecycle.

Owns the EPIC browser session:
- Pinned Playwright >=1.55 (Amendment #16).
- `launch_persistent_context` only — never CDP attach.
- Absolute paths for `user_data_dir`.
- `cleanup_user_data_dir_lock()` runs before every launch — deletes stale
  `SingletonLock` / `LOCK` files when no live PID owns them. Recovers from
  prior crashes that didn't release the lock.
- Headed (operator drives EPIC manually to a starting screen, then clicks
  "Begin Entry").
- `--debug` enables Playwright tracing (`trace.zip`) + per-action screenshots.
"""

# TODO: implementation pending
