"""decision_wizard.py — walk through Tier 1 open decisions in a small GUI.

Run from the project root:
    python tools/tier1_decisions/decision_wizard.py

Saves answers to docs/tier1-decisions.json.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ANSWERS_PATH = PROJECT_ROOT / "docs" / "tier1-decisions.json"


@dataclass
class Question:
    key: str
    title: str
    context: str           # plain-prose explanation
    options: list[tuple[str, str]]  # (option_id, display label with optional sub-text)
    recommendation: str | None = None


QUESTIONS: list[Question] = [
    Question(
        key="named_insureds_shape",
        title="Q1 · Named Insureds — Path A or Path B?",
        context=(
            "Today's GUI shows the primary named insured as a special row at the top, "
            "with additional named insureds rendered as repeatable rows below. EPIC "
            "actually stores all named insureds in one list (the 'OtherNamedInsureds' "
            "screen), with one of them flagged as primary. We can model it either way.\n\n"
            "• PATH A — keep the visual split. Primary uses singleton tags "
            "(account.named_insured, account.fein, ...). Additional rows use a "
            "separate repeatable namespace (account.other_named_insured.*).\n"
            "Two parallel schemas for the same concept.\n\n"
            "• PATH B — unify. ALL named insureds become rows in a single repeatable "
            "group account.named_insured.* with a 'type=primary | dba | additional' "
            "discriminator. One schema, matches EPIC's storage."
        ),
        options=[
            ("path_a", "Path A — keep primary as a special singleton, repeat the rest"),
            ("path_b", "Path B — unify all named insureds into one repeatable group"),
        ],
        recommendation="Path B (cleaner data model; matches EPIC's actual storage)",
    ),
    Question(
        key="vehicle_rescrape",
        title="Q2 · Re-scrape the Business Auto > Vehicle screen?",
        context=(
            "The scraper grabbed `Business Auto > Vehicle` as a screen entry but its "
            "fields list is empty. We have no automation_ids for vehicle year, make, "
            "model, VIN, etc. Without those, vehicles can't be entered into EPIC by "
            "the entry stage.\n\n"
            "• YES — re-run the scraper for that one screen now (~30 min of work).\n"
            "• ANNOTATE WITH ACORD — annotate vehicle tags using ACORD form analogs, "
            "leaving automation_ids null until later.\n"
            "• DEFER — leave vehicles unmapped in Tier 1; revisit when entry is built."
        ),
        options=[
            ("rescrape_now", "Re-scrape the Vehicle screen before annotation"),
            ("annotate_acord", "Annotate tags now, fill automation_ids later"),
            ("defer", "Skip vehicles in Tier 1"),
        ],
        recommendation="Re-scrape — vehicles are too central to do later",
    ),
    Question(
        key="gl_additional_interest",
        title="Q3 · GL Additional Interest — what's actually in EPIC?",
        context=(
            "The Field Map screen `General Liability > AdditionalInterest` contains "
            "fields like `streProduct`, `streAnnualGrossSales`, and Edit0..Edit9 / "
            "Combo0..Combo9 — that's a Products schedule, NOT an Additional Interest "
            "screen. Either the scraper grabbed the wrong screen, or GL Additional "
            "Interests are handled differently in EPIC.\n\n"
            "• RE-SCRAPE — point the scraper at the correct GL Additional Interest "
            "screen.\n"
            "• INVESTIGATE FIRST — open EPIC manually, find where GL Additional "
            "Interests live, document the path, then scrape.\n"
            "• REUSE PATTERN — assume GL Additional Interests share the same shape "
            "as Property/Auto/IM (they do in ACORD), and tag accordingly with "
            "automation_ids null until verified.\n"
            "• ACCEPT GAP — leave GL Additional Interests unmapped for Tier 1."
        ),
        options=[
            ("rescrape", "Re-scrape the correct GL AI screen"),
            ("investigate", "Investigate EPIC first, then scrape"),
            ("reuse_pattern", "Reuse Property/Auto/IM pattern, automation_ids deferred"),
            ("accept_gap", "Leave GL Additional Interests unmapped for now"),
        ],
        recommendation="Investigate first — Additional Interests across LOBs are similar but not identical in EPIC",
    ),
    Question(
        key="address_subfields",
        title="Q4 · Composite address fields — fix the scraper or work around?",
        context=(
            "The Field Map scraper expanded `adeMailing` into sub-fields (-streetLine, "
            "-state, -country) on SOME screens but not others. AND even where expanded, "
            "`adeMailing-city` and `adeMailing-zip` are MISSING — the scraper found "
            "street, state, and country only. That's not a complete address.\n\n"
            "Affected screens include: Commercial AP > Applicant (parent only, no "
            "expansion), OtherNamedInsureds (partial expansion), WC Applicant (partial), "
            "all 5 AdditionalInterest screens (partial via adePrimary-*).\n\n"
            "• FIX SCRAPER — patch the scraper to consistently expand all 4 sub-fields "
            "(street, city, state, zip) on every composite address.\n"
            "• MANUAL FILL — leave scraper alone; manually add missing city/zip "
            "entries to the Field Map during annotation.\n"
            "• HANDLE IN ANNOTATOR — annotator detects parent-only `ade*` fields, "
            "writes the 4 sub-fields itself."
        ),
        options=[
            ("fix_scraper", "Fix the scraper to expand composite addresses correctly"),
            ("manual_fill", "Manually add missing entries during annotation"),
            ("handle_in_annotator", "Have the annotator expand parent-only addresses"),
        ],
        recommendation="Fix the scraper — every other address field downstream depends on this being right",
    ),
    Question(
        key="property_building_bpp",
        title="Q5 · Property — Building Limit and BPP Limit as separate columns?",
        context=(
            "Today the GUI's property table has separate columns for 'Building Limit' "
            "and 'BPP Limit'. EPIC stores both as `deceAmount` rows on the Subject "
            "screen, with `cboSubject` distinguishing whether the amount applies to "
            "Building, BPP, BI, etc.\n\n"
            "• ONE ROW PER AMOUNT — match EPIC. Each property line item is one row "
            "with a Coverage Type column. Building, BPP, BI all show as separate "
            "rows. Honest to the data shape.\n"
            "• PIVOT IN GUI — keep the current layout. Each location/building is "
            "one row; building limit, BPP limit, BI limit are columns. Reads better "
            "for ops but requires a pivot transformation."
        ),
        options=[
            ("one_row_per_amount", "One row per amount (matches EPIC)"),
            ("pivot_in_gui", "Pivot in GUI (current layout)"),
        ],
        recommendation="One row per amount — pivots tend to drift; honest data shape is easier to maintain",
    ),
    Question(
        key="umbrella_underlying",
        title="Q6 · Umbrella — underlying policies in Tier 1 or defer?",
        context=(
            "EPIC's Commercial Umbrella > UnderlyingInsurance screen is structured as "
            "parallel field blocks per coverage line: streAutoLine, streAutoCarrier, "
            "streAutoPolNum (and similar for GL/EL/Other). One screen produces multiple "
            "rows in our GUI.\n\n"
            "Treating this as Tier 1 means designing a tag scheme that handles the "
            "line discriminator AND mapping ~50 fields. Many real dec pages don't "
            "include underlying policy detail at all; the data may be empty for most "
            "submissions.\n\n"
            "• TIER 1 — annotate now using a `policy.umbrella.underlying.*` repeatable "
            "with a 'line' discriminator (auto, gl, el, ...).\n"
            "• DEFER — mark unmapped for Tier 1, address when a real submission with "
            "underlying detail comes through."
        ),
        options=[
            ("tier1", "Tier 1 — annotate now"),
            ("defer", "Defer to Tier 2"),
        ],
        recommendation="Defer — common dec pages don't carry this; revisit on demand",
    ),
]


# ---------------------------------------------------------------------------
# Page widget
# ---------------------------------------------------------------------------


class QuestionPage(QWidget):
    def __init__(self, q: Question, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.q = q
        self._radio_group = QButtonGroup(self)
        self._radio_group.setExclusive(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 22)
        outer.setSpacing(14)

        title = QLabel(q.title)
        f = title.font()
        f.setPointSize(16)
        f.setBold(True)
        title.setFont(f)
        title.setWordWrap(True)
        outer.addWidget(title)

        # Scrollable context
        ctx = QLabel(q.context)
        ctx.setWordWrap(True)
        ctx.setStyleSheet("color:#374151; font-size:12px; line-height:1.4;")
        ctx.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(ctx)
        scroll.setMinimumHeight(120)
        scroll.setMaximumHeight(220)
        outer.addWidget(scroll)

        if q.recommendation:
            rec = QLabel(f"Recommendation: {q.recommendation}")
            rec.setWordWrap(True)
            rec.setStyleSheet(
                "color:#1d4ed8; font-size:12px; font-style:italic;"
                "background:#eff6ff; border:1px solid #dbeafe;"
                "border-radius:4px; padding:6px 10px;"
            )
            outer.addWidget(rec)

        opts_label = QLabel("Choose one:")
        opts_label.setStyleSheet("color:#374151; font-size:12px; font-weight:bold;")
        outer.addWidget(opts_label)

        self._option_buttons: dict[str, QRadioButton] = {}
        for opt_id, opt_text in q.options:
            btn = QRadioButton(opt_text)
            btn.setStyleSheet("font-size:13px; padding:4px;")
            self._radio_group.addButton(btn)
            self._option_buttons[opt_id] = btn
            outer.addWidget(btn)

        notes_label = QLabel("Notes (optional):")
        notes_label.setStyleSheet("color:#374151; font-size:12px; font-weight:bold; margin-top:8px;")
        outer.addWidget(notes_label)

        self._notes = QPlainTextEdit()
        self._notes.setMaximumHeight(72)
        self._notes.setPlaceholderText(
            "Anything you want to remember when we lock the Tier 1 list…"
        )
        self._notes.setStyleSheet("font-size:12px;")
        outer.addWidget(self._notes)

        outer.addStretch(1)

    def selected_option(self) -> str | None:
        for opt_id, btn in self._option_buttons.items():
            if btn.isChecked():
                return opt_id
        return None

    def notes_text(self) -> str:
        return self._notes.toPlainText().strip()

    def restore(self, option_id: str | None, notes: str) -> None:
        if option_id and option_id in self._option_buttons:
            self._option_buttons[option_id].setChecked(True)
        if notes:
            self._notes.setPlainText(notes)


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------


class DecisionWizard(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Tier 1 Decisions — IGA Marketing Master 2.0")
        self.resize(820, 720)

        self._answers: dict[str, dict] = {}
        self._load_existing()

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Top progress strip
        self._progress_label = QLabel("")
        self._progress_label.setStyleSheet(
            "background:#1e293b; color:#f1f5f9; padding:10px 24px;"
            "font-size:13px; font-weight:bold;"
        )
        layout.addWidget(self._progress_label)

        # Stack of question pages
        self._stack = QStackedWidget()
        self._pages: list[QuestionPage] = []
        for q in QUESTIONS:
            page = QuestionPage(q)
            saved = self._answers.get(q.key, {})
            page.restore(saved.get("option"), saved.get("notes", ""))
            self._pages.append(page)
            self._stack.addWidget(page)
        layout.addWidget(self._stack, 1)

        # Bottom button bar
        btn_bar = QWidget()
        btn_bar.setStyleSheet("background:#f8fafc; border-top:1px solid #e2e8f0;")
        bb = QHBoxLayout(btn_bar)
        bb.setContentsMargins(20, 12, 20, 12)
        bb.setSpacing(10)

        self._back_btn = QPushButton("← Back")
        self._back_btn.setStyleSheet(_BTN_SECONDARY)
        self._back_btn.clicked.connect(self._on_back)
        bb.addWidget(self._back_btn)

        bb.addStretch(1)

        self._save_btn = QPushButton("Save & Exit")
        self._save_btn.setStyleSheet(_BTN_SECONDARY)
        self._save_btn.clicked.connect(self._on_save_exit)
        bb.addWidget(self._save_btn)

        self._next_btn = QPushButton("Next →")
        self._next_btn.setStyleSheet(_BTN_PRIMARY)
        self._next_btn.clicked.connect(self._on_next)
        bb.addWidget(self._next_btn)

        layout.addWidget(btn_bar)

        self._refresh_chrome()

    # -- Persistence -------------------------------------------------------

    def _load_existing(self) -> None:
        if ANSWERS_PATH.is_file():
            try:
                self._answers = json.loads(ANSWERS_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._answers = {}

    def _capture_current_page(self) -> None:
        idx = self._stack.currentIndex()
        page = self._pages[idx]
        opt = page.selected_option()
        notes = page.notes_text()
        if opt is None and not notes:
            return
        self._answers[page.q.key] = {"option": opt, "notes": notes}

    def _persist(self) -> None:
        ANSWERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        ANSWERS_PATH.write_text(
            json.dumps(self._answers, indent=2, sort_keys=True), encoding="utf-8"
        )

    # -- Navigation --------------------------------------------------------

    def _on_back(self) -> None:
        self._capture_current_page()
        self._persist()
        idx = self._stack.currentIndex()
        if idx > 0:
            self._stack.setCurrentIndex(idx - 1)
            self._refresh_chrome()

    def _on_next(self) -> None:
        self._capture_current_page()
        self._persist()
        idx = self._stack.currentIndex()
        if idx < len(self._pages) - 1:
            self._stack.setCurrentIndex(idx + 1)
            self._refresh_chrome()
        else:
            self._show_finish_dialog()

    def _on_save_exit(self) -> None:
        self._capture_current_page()
        self._persist()
        self.close()

    def _show_finish_dialog(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        answered = sum(
            1 for q in QUESTIONS
            if self._answers.get(q.key, {}).get("option")
        )
        msg = QMessageBox(self)
        msg.setWindowTitle("Done")
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText(
            f"All {len(QUESTIONS)} questions reviewed.\n\n"
            f"{answered} of {len(QUESTIONS)} answered.\n\n"
            f"Saved to:\n{ANSWERS_PATH}"
        )
        msg.exec()
        self.close()

    def _refresh_chrome(self) -> None:
        idx = self._stack.currentIndex()
        total = len(self._pages)
        answered = sum(
            1 for q in QUESTIONS
            if self._answers.get(q.key, {}).get("option")
        )
        self._progress_label.setText(
            f"Question {idx + 1} of {total}    ·    {answered} answered so far"
        )
        self._back_btn.setEnabled(idx > 0)
        self._next_btn.setText("Finish" if idx == total - 1 else "Next →")


_BTN_PRIMARY = """
QPushButton {
    background:#2563eb; color:white; border:none; border-radius:6px;
    padding:8px 22px; font-size:13px; font-weight:bold; min-width:90px;
}
QPushButton:hover { background:#1d4ed8; }
QPushButton:disabled { background:#93c5fd; color:#dbeafe; }
"""

_BTN_SECONDARY = """
QPushButton {
    background:white; color:#374151; border:1px solid #d1d5db;
    border-radius:6px; padding:7px 18px; font-size:13px; min-width:80px;
}
QPushButton:hover { background:#f9fafb; border-color:#9ca3af; }
QPushButton:disabled { color:#d1d5db; border-color:#e5e7eb; }
"""


def main() -> int:
    app = QApplication(sys.argv)
    win = DecisionWizard()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
