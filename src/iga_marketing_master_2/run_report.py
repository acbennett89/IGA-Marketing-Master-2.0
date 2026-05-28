"""run_report.py — package and email a run's artifacts via Outlook COM.

The ``EntryRuntime`` writes screenshots and JSON sidecars into a per-run
folder under ``<Working Library>/<Client>/run_artifacts/<run_id>/``.
After the run, the operator clicks "Send report" on the busy dialog;
this module zips the folder, opens (or sends) an Outlook message with
the zip attached, and deletes the source folder once the operator
confirms send.

Outlook COM is preferred over SMTP because the user is already
authenticated in Outlook — no credentials in config. The trade-off is
Windows-only and requires Outlook to be installed.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_log = logging.getLogger("iga.run_report")


@dataclass(slots=True, kw_only=True)
class ReportContext:
    """Inputs needed to compose and send a run report."""

    run_id: str
    client_name: str
    artifacts_dir: Path
    """The per-run folder containing screenshots + JSON sidecars + run.log."""
    recipient: str
    finding_count: int
    finding_summary_lines: list[str]
    """Short bullets per validation finding (1 line each)."""
    outcome: str
    """``"completed"`` / ``"cancelled"`` / ``"failed"``."""


def send_report(ctx: ReportContext, *, auto_send: bool = False) -> bool:
    """Compose (or send) an Outlook message; return True on success.

    :param auto_send: When True, the message is sent silently via
        ``mailItem.Send()``. When False, ``mailItem.Display()`` opens a
        compose window so the operator can review and send manually.
    """
    if not ctx.artifacts_dir.exists():
        _log.warning("artifacts dir %s missing — nothing to send", ctx.artifacts_dir)
        return False

    zip_path = _zip_artifacts(ctx.artifacts_dir, ctx.run_id, ctx.client_name)
    if zip_path is None:
        return False

    subject = f"IGA Entry Run — {ctx.client_name} — {ctx.outcome.title()}"
    body = _compose_body(ctx)

    sent = _compose_or_send_outlook(
        recipient=ctx.recipient,
        subject=subject,
        body=body,
        attachment=zip_path,
        auto_send=auto_send,
    )

    if sent and auto_send:
        # Only delete on confirmed silent send. For compose mode, we don't
        # know whether the user actually sent it, so we leave the folder
        # for them to retry.
        _cleanup(ctx.artifacts_dir, zip_path)

    return sent


def cleanup_after_send(artifacts_dir: Path, zip_path: Path | None = None) -> None:
    """Public hook for the GUI to call after the user confirms the send.

    Removes the run-artifacts folder and the temp zip so the next run
    starts fresh.
    """
    _cleanup(artifacts_dir, zip_path)


# ── Internals ───────────────────────────────────────────────────────────────


def _zip_artifacts(artifacts_dir: Path, run_id: str, client_name: str) -> Path | None:
    """Zip the artifacts folder into the system temp dir; return the zip path."""
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_client = "".join(c if c.isalnum() else "_" for c in client_name)[:40]
        out_base = Path(tempfile.gettempdir()) / f"iga_run_{safe_client}_{ts}"
        # shutil.make_archive auto-appends .zip
        zip_str = shutil.make_archive(
            str(out_base), "zip",
            root_dir=str(artifacts_dir.parent),
            base_dir=artifacts_dir.name,
        )
        return Path(zip_str)
    except Exception as exc:
        _log.error("failed to zip artifacts: %s", exc)
        return None


def _compose_body(ctx: ReportContext) -> str:
    lines = [
        f"Client:   {ctx.client_name}",
        f"Run ID:   {ctx.run_id}",
        f"Outcome:  {ctx.outcome}",
        f"Findings: {ctx.finding_count}",
        "",
    ]
    if ctx.finding_summary_lines:
        lines.append("Validation findings:")
        for line in ctx.finding_summary_lines:
            lines.append(f"  - {line}")
        lines.append("")
    lines.append("Screenshots and JSON details are in the attached zip.")
    return "\n".join(lines)


def _compose_or_send_outlook(
    *,
    recipient: str,
    subject: str,
    body: str,
    attachment: Path,
    auto_send: bool,
) -> bool:
    """Drive Outlook via COM. Returns True on success."""
    try:
        import win32com.client  # type: ignore[import-not-found]
    except ImportError:
        _log.error(
            "pywin32 not installed — cannot send Outlook email. "
            "Run: pip install pywin32"
        )
        return False

    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        mail = outlook.CreateItem(0)  # 0 = olMailItem
        mail.To = recipient
        mail.Subject = subject
        mail.Body = body
        mail.Attachments.Add(str(attachment))
        if auto_send:
            mail.Send()
            _log.info("Outlook: silent send to %s", recipient)
        else:
            mail.Display(False)  # non-modal
            _log.info("Outlook: compose window opened for %s", recipient)
        return True
    except Exception as exc:
        _log.error("Outlook COM failed: %s", exc)
        return False


def _cleanup(artifacts_dir: Path, zip_path: Path | None) -> None:
    try:
        if artifacts_dir.exists():
            shutil.rmtree(artifacts_dir, ignore_errors=True)
            _log.info("removed artifacts dir %s", artifacts_dir)
    except Exception as exc:
        _log.warning("cleanup of %s failed: %s", artifacts_dir, exc)
    if zip_path and zip_path.exists():
        try:
            zip_path.unlink()
        except Exception:
            pass
