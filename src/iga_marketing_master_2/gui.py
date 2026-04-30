"""gui.py — PySide6 review/edit interface.

Components:
- `QTableView` + `QAbstractTableModel` per Field Map section.
- `QStyledItemDelegate` editor widgets (text, dropdown for enum_values, date,
  etc.).
- Confidence highlighting (color-coded cells) + Opus-escalation badges.
- Repeatable groups via list+form pattern.
- `QtPdf` preview panel with page deep-link from clicked field.
- `QPlainTextEdit` audit log.
- Domain-tag confirmation dialog for JIT proposals.
- **`OperatorModal` widget class** (Amendment #5, mandatory): every
  operator-facing error/conflict/pause modal must use this class. Enforces a
  fixed structure:
    1. Plain-English headline.
    2. What to do now.
    3. What happens if you click Cancel.
    4. Optional 'Show technical details' fold-out.

Drag-drop or browse PDF intake. First-run dialog for Working Library path
override.
"""

# TODO: implementation pending
