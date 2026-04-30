"""cli.py — command-line entrypoint.

Single command, no subcommands:

    iga-marketing-master-2 [--debug] [--working-library PATH] [--client NAME]

`main()` parses argv, builds Settings via `config.load_settings`, configures
logging via `logger.configure_logging`, then hands off to `gui.IgaApp.run`.

The GUI import is deferred until inside `main()` so unit tests of the
argument parser don't pull PySide6 into the test process at collection time.

See ARCHITECTURE.md §9.1 for the contract; §10 for `--debug` discipline.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import Settings, load_settings, settings_as_dict
from .logger import configure_logging, get_logger

__all__ = ["build_parser", "main", "parse_args", "settings_from_args"]


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the CLI.

    Exposed for testing; production code calls `parse_args` instead.
    """
    parser = argparse.ArgumentParser(
        prog="iga-marketing-master-2",
        description=(
            "IGA Marketing Master 2.0 — extract insurance data from PDFs via "
            "Claude and enter into Applied EPIC via Playwright."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Enable verbose logging, console output at DEBUG level, "
            "Playwright tracing, and full debug artifact retention."
        ),
    )
    parser.add_argument(
        "--working-library",
        metavar="PATH",
        type=str,
        default=None,
        help=(
            "Override the Working Library folder for this run (does not "
            "persist to config.json)."
        ),
    )
    parser.add_argument(
        "--client",
        metavar="NAME",
        type=str,
        default=None,
        help="Auto-select a client folder on launch (skips the picker).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"iga-marketing-master-2 {__version__}",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse argv and return the namespace."""
    parser = build_parser()
    return parser.parse_args(list(argv) if argv is not None else None)


def settings_from_args(args: argparse.Namespace) -> Settings:
    """Translate parsed CLI args into a Settings object via load_settings."""
    # We only forward the flags the operator actually passed. load_settings()
    # then layers them on top of the persisted config.json + platformdirs
    # defaults so absent flags don't clobber saved preferences.
    overrides: dict[str, Any] = {"debug": bool(args.debug)}
    if args.working_library:
        overrides["working_library"] = Path(args.working_library)
    if args.client:
        overrides["cli_initial_client"] = args.client
    return load_settings(cli_overrides=overrides)


def main(argv: Sequence[str] | None = None) -> int:
    """Process entry point. Returns the Qt exit code (or non-zero on failure)."""
    args = parse_args(argv)
    settings = settings_from_args(args)
    configure_logging(settings)

    logger = get_logger("cli")
    logger.info(
        "iga-marketing-master-2 %s starting (debug=%s)",
        __version__,
        settings.debug,
    )
    if settings.debug:
        logger.debug("settings: %s", settings_as_dict(settings))

    # Deferred import so test harnesses can exercise CLI parsing without
    # paying the PySide6 import cost (and without crashing in environments
    # where PySide6 / Qt platform plugins aren't available).
    try:
        from .gui import IgaApp  # type: ignore[import-not-found]
    except ImportError as exc:
        logger.error("failed to import GUI module: %s", exc)
        sys.stderr.write(
            "IGA Marketing Master could not start: the PySide6 GUI is not "
            "available. Run scripts/bootstrap.ps1 to install dependencies, "
            "then try again.\n",
        )
        return 2

    try:
        exit_code = IgaApp.run(debug=settings.debug)
    except Exception:  # noqa: BLE001 — top-level safety net
        # Last line of defense: anything that escapes the GUI loop is logged
        # with traceback and translated to a non-zero exit instead of dumping
        # a traceback to a Windows console window the operator can't read.
        logger.exception("unhandled exception in GUI; shutting down")
        return 1
    finally:
        # Flush the rotating file handler so partial logs don't get lost when
        # the process exits.
        logging.shutdown()
    return int(exit_code)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
