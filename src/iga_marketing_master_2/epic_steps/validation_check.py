"""validation_check.py — scan the live EPIC page for validation errors.

Called at checkpoints inside each step file (after section nav, after row
fills, before commits). When an error is detected:

1. Capture a screenshot to the run's artifacts folder.
2. Build a :class:`ValidationFinding` describing the error + context.
3. Call the runtime's ``on_validation_halt`` callback. The GUI shows a
   modal with the error text, screenshot, and an ``expecting`` line. The
   user picks Proceed (run continues) or Cancel (run aborts).
4. If Proceed, the function returns and the step file moves on. The
   operator is expected to have fixed the issue in EPIC manually before
   clicking Proceed.
5. If Cancel, raise :class:`EntryCancelled` to abort the run cleanly.

When no runtime is active (e.g. the standalone ``test_*.py`` scripts),
``check_and_halt`` is a no-op so existing tests keep working.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .runtime import (
    EntryCancelled,
    ValidationFinding,
    check_cancel,
    get_runtime,
)


def checkpoint(page: Any, expecting: str) -> None:
    """Combined cancel + validation check.

    Convenience wrapper called at major checkpoints in step files. Equivalent to
    ``check_cancel()`` followed by ``check_and_halt(page, expecting)``. Both are
    no-ops when no runtime is active.
    """
    check_cancel()
    check_and_halt(page, expecting)

_log = logging.getLogger("iga.epic_steps.validation_check")


def check_and_halt(page: Any, expecting: str) -> None:
    """Inspect the page for EPIC validation surfaces; halt for human on hit.

    :param page: Playwright ``Page``.
    :param expecting: Short operator-facing description of what the script
        is about to do next (e.g. ``"Inland Marine > Additional Coverages
        > adding row 12: 'Borrowed, Leased, Rented, or Hired Equipment'"``).

    Behavior:
      * No runtime set → no-op (standalone test scripts).
      * No error found → returns normally.
      * Error found → capture artifacts, call GUI callback. On
        ``"proceed"`` returns; on ``"cancel"`` raises ``EntryCancelled``.
    """
    rt = get_runtime()
    if rt is None:
        return

    detection = _detect_error(page)
    if detection is None:
        return

    error_text, surface, screen_code = detection
    seq = len(rt.findings) + 1

    screenshot_path = _capture_screenshot(page, rt.artifacts_dir, seq, screen_code)
    _write_finding_json(rt.artifacts_dir, seq, error_text, surface, screen_code,
                        expecting, screenshot_path)

    finding = ValidationFinding(
        error_text=error_text,
        surface=surface,
        screen_code=screen_code,
        expecting=expecting,
        screenshot_path=screenshot_path,
        sequence=seq,
    )
    rt.findings.append(finding)
    _log.warning(
        "validation_halt seq=%d screen=%s surface=%s: %s",
        seq, screen_code, surface, error_text,
    )

    choice = rt.on_validation_halt(finding)
    if choice == "cancel":
        raise EntryCancelled(f"user cancelled at validation halt #{seq}: {error_text}")
    # Otherwise resume — operator fixed it in EPIC manually


# ── Detection ───────────────────────────────────────────────────────────────


def _detect_error(page: Any) -> tuple[str, str, str] | None:
    """Return ``(error_text, surface, screen_code)`` or ``None``."""
    try:
        result = page.evaluate(
            """() => {
                // 1. Portal modal (React) — most common on newer screens
                const portal = document.querySelector('[data-test="common-message-modal"]');
                if (portal && portal.offsetParent !== null) {
                    const txt = (portal.textContent || '').trim();
                    // Trim the OK / Close button labels off the end
                    const stripped = txt.replace(/(OK|Close|×)$/i, '').trim();
                    if (stripped && /error|required|must|invalid|missing/i.test(stripped)) {
                        return { text: stripped, surface: 'portal_modal' };
                    }
                }
                // 2. Legacy <message-box> (Angular)
                for (const m of document.querySelectorAll('message-box')) {
                    if (m.offsetParent === null) continue;
                    const txt = (m.textContent || '').trim();
                    if (!txt) continue;
                    const stripped = txt.replace(/(OK|Close|×)$/i, '').trim();
                    if (stripped && /error|required|must|invalid|missing/i.test(stripped)) {
                        return { text: stripped, surface: 'message_box' };
                    }
                }
                // 3. Inline red field markers — best-effort
                const inline = document.querySelector('.field-error, [class*="error-message"]:not(:empty)');
                if (inline && inline.offsetParent !== null) {
                    const t = (inline.textContent || '').trim();
                    if (t && t.length > 3) {
                        return { text: t, surface: 'inline_marker' };
                    }
                }
                return null;
            }"""
        )
        if not result:
            return None
        # Pull the screen code from the status bar
        try:
            screen_code = page.evaluate(
                """() => {
                    const text = document.body.innerText || '';
                    const m = text.match(/\\b(CHM-[A-Z]{3,12}|MKADMSTR|MK[A-Z0-9]{2,12})\\b/);
                    return m ? m[1] : '';
                }"""
            ) or ""
        except Exception:
            screen_code = ""
        return str(result.get("text", "")), str(result.get("surface", "unknown")), screen_code
    except Exception as exc:
        _log.debug("validation_check page eval failed: %s", exc)
        return None


def _capture_screenshot(page: Any, artifacts_dir: Path, seq: int, screen_code: str) -> Path | None:
    """Screenshot the page; return path or None on failure."""
    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        suffix = f"_{screen_code}" if screen_code else ""
        path = artifacts_dir / f"validation_{seq:03d}{suffix}.png"
        page.screenshot(path=str(path), full_page=False)
        return path
    except Exception as exc:
        _log.warning("validation screenshot failed: %s", exc)
        return None


def _write_finding_json(
    artifacts_dir: Path,
    seq: int,
    error_text: str,
    surface: str,
    screen_code: str,
    expecting: str,
    screenshot_path: Path | None,
) -> None:
    """Persist a JSON sidecar so the email recipient can read structured info."""
    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "sequence": seq,
            "error_text": error_text,
            "surface": surface,
            "screen_code": screen_code,
            "expecting": expecting,
            "screenshot": screenshot_path.name if screenshot_path else None,
        }
        path = artifacts_dir / f"validation_{seq:03d}.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:
        _log.warning("validation finding JSON write failed: %s", exc)
