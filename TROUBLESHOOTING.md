# Troubleshooting — IGA Marketing Master 2.0

Operator-facing recovery guide. Each section starts with the symptom you'll actually see, followed by the steps to fix it. If something here doesn't match what you're seeing, scroll to **"When to ask Andrew (or designated admin) for help"** at the bottom.

---

## How to install / first-time setup

**Symptom:** You just got the project folder and don't know how to start.

**Do this:**

1. **Open** PowerShell.
2. **Navigate** to the project folder. For example:
   ```
   cd "C:\Users\<your-username>\Documents\GitHub\IGA-Marketing-Master-2.0"
   ```
3. **Run** the bootstrap script:
   ```
   .\scripts\bootstrap.ps1
   ```
4. **Watch for the API key prompt.** When the script asks for your Anthropic API key, **paste** it (your typing is hidden — that's expected) and **press Enter**. You only do this once; the key goes into Windows Credential Manager and the tool finds it automatically next time.
5. **Test the install** by running `iga-marketing-master-2`. The window should open.

If the script fails partway through, look at the last few lines of output. The most common causes:
- **"python is not recognized"** — Python 3.13 isn't on your PATH. Reinstall Python from python.org and tick the "Add to PATH" checkbox.
- **"playwright install failed"** — Network problem or your firewall blocked the Chromium download. Re-run the script after fixing the network.

---

## First run: where to put my Anthropic API key

**Symptom:** The window opens but says "Add your Anthropic API key" and won't run extractions until you provide one.

**Do this:**

1. **Open** `console.anthropic.com` in your normal browser and sign in.
2. **Generate** a new API key (it starts with `sk-ant-`).
3. **Copy** the whole key.
4. **Click** the "Save" button on the dialog after **pasting** the key into the password field. The tool stores it in Windows Credential Manager — you only have to do this once per machine.

**Override option (advanced):** If you want a different key for one session without overwriting the saved one, set the env var before launching:

```
$env:ANTHROPIC_API_KEY = "sk-ant-..."
iga-marketing-master-2
```

The env var wins over the stored key for that session only.

---

## Working Library default location

**Symptom:** You want to know where your client data lives, or you want to move it.

The default location is:

```
%USERPROFILE%\Documents\IGA Marketing Master\Working Library\
```

(Each client is a sub-folder underneath it.)

**To change it:**

1. **Launch** the tool.
2. **Pick "Pick client..."** from the toolbar — when the file dialog opens, navigate to wherever you want the new Working Library to live and **click** "Select Folder".
3. The new path is saved to your config and used from then on.

**One-shot override** (for a single session, no permanent change):

```
iga-marketing-master-2 --working-library "D:\Other\Path"
```

**Putting it on OneDrive:** Technically supported but at your own risk. The tool does not coordinate with OneDrive's sync; if you have the same client open on two machines at once you can lose edits. v1 is single-user-single-machine by design.

---

## EPIC isn't accepting my values

**Symptom:** During an entry session, the tool pauses and shows you a dialog headlined **"EPIC didn't accept '\<value\>' for '\<field\>'."**

**What's happening:** The tool typed the value into EPIC, read it back, and either EPIC reformatted it differently than expected, EPIC rejected it outright with a red error, or the value didn't pass EPIC's validation rules.

**Do this:**

1. **Open** the field in EPIC manually (it's already on screen).
2. **Type or pick** a value EPIC will accept. The dialog tells you exactly which field on which screen.
3. **Click** "Resume." The tool re-reads what you typed and treats your value as the new canonical answer — your edit is saved to `state.json` and the run continues.

**Other options on the dialog:**
- **"Skip this field"** — leaves the field as-is in EPIC and continues with the next field. The state is marked pending so you can revisit later.
- **"Cancel run"** — saves where you are and ends the entry session. You can come back to it later.

**If this happens to the same field twice in a row,** the value Claude extracted is probably wrong (a wrong format, wrong list option, or a typo in the source PDF). Edit the value in the GUI's review tab, approve it, and re-run **Begin Entry** — it picks up where you left off.

---

## "Couldn't find the field" (selector drift)

**Symptom:** During an entry session, the tool pauses and shows you a dialog headlined **"Couldn't find the \<field\> field on the \<screen\> screen."**

**What's happening:** EPIC moved or renamed an HTML element since the Field Map was scraped. The tool tried three different ways to find the field (its automation ID, its name attribute, and its label text) and none of them matched.

**Do this:**

1. **Make sure** you're on the right EPIC screen. The dialog tells you which one.
2. **Find** the field manually in EPIC. **Type** or **pick** the value yourself.
3. **Click** "Resume." The tool will re-read the screen and continue from the next field.

**If selector drift happens repeatedly across many fields,** EPIC has probably had a UI update. **Stop the run** (cancel) and let Andrew know — the Field Map needs to be refreshed. See "When to ask Andrew" below.

The pre-flight smoke check at the start of every entry session catches a lot of this proactively. If you see a yellow banner at the top of the audit log saying "N selector(s) look stale," the tool already knows there's drift and will be slower than usual.

---

## I see two state files for the same client

**Symptom:** You opened a client folder in Explorer and see both `state.json` and `state.json.bak` (or possibly other JSON files). You're not sure which one is the real one.

**Quick answer:** `state.json` is canonical. `state.json.bak` is the previous-good copy that the tool keeps as a rollback target.

**Path B is single-user.** The tool doesn't expect more than one writer at a time, so if you ended up with multiple `state.json` files (e.g., you copied a folder from another machine and renamed the copy back), pick the one with the **newest modification time** as canonical:

1. **Sort** the folder by Date Modified (descending).
2. **Rename** the newest one to `state.json` (overwriting the existing one if needed — but **back up the existing one first**).
3. **Rename** the second-newest to `state.json.bak`.
4. **Delete** any leftover copies once you're confident.

If you're unsure which copy has the work you actually want, **don't overwrite anything** — copy the folder somewhere safe and ask Andrew.

---

## state.json wouldn't load

**Symptom:** The tool says "Your client's state file is unreadable" or you see a message about state corruption.

**What the tool already tried:** It walks a recovery chain automatically:
1. `state.json` — the canonical file.
2. `state.json.bak` — the rolling backup, refreshed on every save.
3. `<client>/snapshots/state-YYYY-MM-DD.json` — daily snapshots, kept for 30 days.

If all three of those failed, you'll see the corruption error.

**Do this (in order; stop as soon as one works):**

1. **Open** `<your-working-library>\<client>\snapshots\` in Explorer.
2. **Sort** by name (descending) — newest snapshot is at the top.
3. **Copy** the newest snapshot one level up into the client folder.
4. **Rename** your copy to `state.json` (after first **renaming** the broken `state.json` to something like `state.broken.json` so you don't lose it).
5. **Re-launch** the tool and open the client. You should be back in business, minus any work done since that snapshot.

If even the snapshots are unreadable, see "When to ask Andrew" below.

---

## Browser already running

**Symptom:** Launching the tool fails with a "Playwright profile is owned by live PID \<N\>" message, or "Chromium reports the profile is in use."

**What's happening:** The tool keeps a dedicated Chromium profile under your user data dir and refuses to launch a second copy of the browser against the same profile (Chrome and Chromium do not allow it).

**Do this:**

1. **Look** at your taskbar / system tray for an orphan Chromium window from a previous run.
2. **Close** it (or right-click and close from the taskbar).
3. **Try again.** The tool's `cleanup_user_data_dir_lock` step runs before every launch and cleans up stale lock files automatically, so a clean shutdown won't leave you in this state.

**If no Chromium window is visible** but the lock persists, a previous Chromium process may have crashed without releasing its lock. **Open** Task Manager → Details, **find** any `chrome.exe` processes you don't recognize, and **end** them. Then re-launch.

**If the error keeps happening even after Task Manager,** **restart Windows.** This always clears it.

---

## Pause-for-human flow (general)

**Symptom:** During Begin Entry, the audit log scrolls and a dialog pops up asking you to do something in EPIC.

**What's happening:** This is normal. The tool is designed to pause whenever it can't safely proceed alone — either because EPIC rejected a value (see above), the field moved (see above), or something else unexpected happened. The dialog tells you specifically what to do.

**The three buttons mean:**
- **Resume** — "I fixed it; carry on. Re-read the field and treat what's there as canonical."
- **Skip this field** — "Leave this one alone and try the next field."
- **Cancel run** — "Stop. Save my progress. I'll come back later."

**Your edits are always saved.** Cancelling out of a pause doesn't lose anything; the tool persists the pause to disk before opening the dialog so even a power cut wouldn't lose your spot. When you re-launch later, the tool will offer to resume from this exact field.

---

## Where do logs and debug artifacts live

**Plain English summary:** There are three layers of logs.

**Per-client run log** — one line per significant event during a session for one client.

```
<your-working-library>\<client>\runs.log
```

Useful for: "what happened when I worked on this client yesterday?"

**Global app log** — rotating across all clients.

```
%LOCALAPPDATA%\IGA Marketing Master\logs\iga.log
```

Rotates at 10 MB, keeps 5 backups, total budget about 50 MB.

Useful for: "what happened across all my clients this month?"

**Debug artifacts (only when `--debug` is on)** — much noisier diagnostics under the client folder:

```
<your-working-library>\<client>\debug\
├── claude\<run_id>\          # Claude API request/response JSON dumps
├── playwright\<run_id>\      # Playwright trace.zip + screenshots
└── split\<run_id>\           # PDF chunks if a PDF was too big to send whole
```

The tool keeps the **last 5 run_id directories** under `debug\playwright\` per client; older ones are auto-pruned.

To turn this on for one session:

```
iga-marketing-master-2 --debug
```

**Don't share these debug artifacts publicly** — they contain everything Claude saw, including the full source PDFs (in some cases) and any PII inside them.

---

## When to ask Andrew (or designated admin) for help

Most things in this tool are designed to be operator-recoverable. **Email Andrew** (or your designated admin) when you hit one of these:

- **Many fields fail selector resolution in a row** during an entry session. EPIC has likely had a UI update; the Field Map needs to be refreshed before the tool will be useful again.
- **Daily snapshots are also corrupt** (you walked the recovery chain and even the snapshots wouldn't load).
- **`bootstrap.ps1` won't complete** even after you fixed obvious problems (Python on PATH, network up).
- **The tool is asking you to confirm a brand-new domain_tag** and you don't know what it should be tagged as. (See [`docs/workflow/ARCHITECTURE.md`](docs/workflow/ARCHITECTURE.md) §3 for the tag grammar — but if it's unclear, escalate.)
- **Anything you can't explain to yourself.** This tool is meant to keep you in control of the data flowing into EPIC. If something is happening that you don't understand, stop and ask before clicking through.

When emailing for help, include:

- The client name (so Andrew can look at the right `runs.log`).
- The approximate time you ran the session (the global log timestamps can narrow it down).
- A screenshot of any error dialog the tool showed you.
- Whether you were running with `--debug`.

---

## Quick reference

| Want to... | Do this |
|---|---|
| Install for the first time | `.\scripts\bootstrap.ps1` |
| Launch the tool | `iga-marketing-master-2` |
| Launch with debug logs | `iga-marketing-master-2 --debug` |
| Open a specific client on launch | `iga-marketing-master-2 --client "Acme Corp"` |
| Use a different Working Library for one run | `iga-marketing-master-2 --working-library "D:\Path"` |
| Change my saved Anthropic API key | Re-launch; if it's wrong, the tool will re-prompt. Or paste a new one over the env var. |
| Find my client's state | `<your-working-library>\<client>\state.json` |
| Recover from corrupt state | Walk the chain: `state.json` → `state.json.bak` → newest in `<client>\snapshots\` |
| See what happened in a session | `<client>\runs.log` |
