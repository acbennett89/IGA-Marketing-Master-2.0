"""cli.py — command-line entrypoint.

Parses CLI arguments (`--debug`, client selection, run mode), wires up the
logger, secret store, and config, then hands off to the GUI (default) or to
non-interactive subcommands as the project grows. The CLI is a thin shell;
business logic lives in the other modules.
"""

# TODO: implementation pending
