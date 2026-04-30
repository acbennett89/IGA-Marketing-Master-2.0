# diagnostic_inputs/

Drop test PDFs into this folder. The diagnostic script (`scripts/diagnose.py` or `Diagnose.bat`) will use whatever PDFs it finds here on every run -- you stage them once and never have to re-drag-drop them through the GUI.

## What goes here

Real or synthetic insurance documents you want to use for diagnostic extractions. Common picks:

- One simple dec page (small, fast, cheap)
- One vehicle / driver / equipment schedule (tests repeatable groups)
- One forms list (tests the Forms section)
- One full-package declarations bundle (largest, exercises split logic)

## What does NOT go here

- Production client data. The diagnostic uses a separate Working Library at `Testing and Example Library/diagnostic_workspace/` so nothing here pollutes real client folders, but the PDFs themselves still have PII.
- Anything you wouldn't want in your local repo. The `*.pdf` files in this directory are gitignored; only this README is tracked.

## How to run

```
# free -- verifies setup, prompt token sizes, API key:
python scripts\diagnose.py --check

# real extraction against ALL PDFs here:
python scripts\diagnose.py

# cheaper -- just the first PDF (alphabetical):
python scripts\diagnose.py --single

# force Opus 4.7 on every call (more expensive but higher fidelity):
python scripts\diagnose.py --force-opus
```

Or double-click `Diagnose.bat` at the project root for a windowed run with output that stays visible.
