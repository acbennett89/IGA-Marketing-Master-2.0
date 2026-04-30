"""config.py — runtime configuration and path discovery.

Owns all path resolution via `platformdirs`:
- Working Library default: `Path(platformdirs.user_documents_dir()) /
  "IGA Marketing Master" / "Working Library"` (configurable via first-run
  picker).
- Config + state files: `platformdirs.user_config_dir("IGA Marketing Master")`.

Also owns: model defaults (Sonnet 4.6 / Opus 4.7), confidence thresholds,
debug flag, and any other runtime knobs. No secrets here — those live in
`secret_store.py`.
"""

# TODO: implementation pending
