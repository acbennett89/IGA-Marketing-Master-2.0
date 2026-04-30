' ============================================================
' Launcher.vbs -- IGA Marketing Master 2.0
' ------------------------------------------------------------
' Double-click to launch the GUI. On first run, this launcher
' detects that .venv is missing, asks once for confirmation,
' and then runs scripts\bootstrap.ps1 in a visible PowerShell
' window so you can see the install progress.
'
' On every subsequent run, .venv exists and the GUI launches
' silently with no console window.
'
' This file is pure ASCII to avoid any Windows codepage
' surprises with non-ASCII characters in MsgBox dialogs.
' ============================================================

Option Explicit

Dim shell, fso, projRoot, pythonw
Dim exitCode, response, msg

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Resolve project root from this script's own location.
projRoot = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw  = projRoot & "\.venv\Scripts\pythonw.exe"

' ---- Fast path: .venv already exists -- launch GUI silently ----
If fso.FileExists(pythonw) Then
    shell.CurrentDirectory = projRoot
    shell.Run """" & pythonw & """ -m iga_marketing_master_2.cli", 0, False
    WScript.Quit 0
End If

' ---- First-run setup needed: confirm with the user ----
msg = "First-time setup is needed. The launcher will install:" & vbCrLf & vbCrLf & _
      "    1. A Python virtual environment (.venv)" & vbCrLf & _
      "    2. All required Python packages" & vbCrLf & _
      "    3. Playwright's Chromium browser (downloaded once)" & vbCrLf & vbCrLf & _
      "This may take a few minutes. A PowerShell window will open" & vbCrLf & _
      "to show progress." & vbCrLf & vbCrLf & _
      "Continue?"
response = MsgBox(msg, vbYesNo + vbQuestion, _
    "IGA Marketing Master 2.0 -- First-time setup")
If response <> vbYes Then
    WScript.Quit 0
End If

' ---- Pre-check: Python 3.13 via the py launcher ----
' bootstrap.ps1 uses `py -3.13`. If that is missing, surface a friendly
' dialog up front instead of forcing the user to read PowerShell errors.
exitCode = shell.Run("cmd /c py -3.13 --version >NUL 2>&1", 0, True)
If exitCode <> 0 Then
    msg = "Python 3.13 is not installed, or the 'py' launcher cannot find it." & vbCrLf & vbCrLf & _
          "Please:" & vbCrLf & _
          "    1. Download Python 3.13 from https://www.python.org/downloads/" & vbCrLf & _
          "    2. During install, check 'Add python.exe to PATH' and" & vbCrLf & _
          "       'Install py launcher'." & vbCrLf & _
          "    3. After Python finishes installing, double-click this" & vbCrLf & _
          "       launcher again."
    MsgBox msg, vbCritical, "IGA Marketing Master 2.0 -- Python 3.13 required"
    WScript.Quit 1
End If

' ---- Run bootstrap.ps1 in a visible PowerShell window ----
' -NoProfile keeps startup fast; -ExecutionPolicy Bypass avoids policy
' surprises on a stock Windows install. Window style 1 = visible normal.
exitCode = shell.Run( _
    "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & projRoot & "\scripts\bootstrap.ps1""", _
    1, True)

If exitCode <> 0 Then
    msg = "Setup failed. The bootstrap script exited with code " & exitCode & "." & vbCrLf & vbCrLf & _
          "Common causes:" & vbCrLf & _
          "    1. No network access (pip and Playwright need internet)" & vbCrLf & _
          "    2. Wrong Python version (need 3.13)" & vbCrLf & _
          "    3. Insufficient permissions" & vbCrLf & vbCrLf & _
          "Re-run scripts\bootstrap.ps1 manually from PowerShell to" & vbCrLf & _
          "see the full output and the specific failing step."
    MsgBox msg, vbCritical, "IGA Marketing Master 2.0 -- Setup failed"
    WScript.Quit exitCode
End If

' ---- Verify .venv was actually created ----
If Not fso.FileExists(pythonw) Then
    msg = "Setup reported success, but pythonw.exe was not found at:" & vbCrLf & vbCrLf & _
          "    " & pythonw & vbCrLf & vbCrLf & _
          "This is unexpected. Check the bootstrap output for clues, or" & vbCrLf & _
          "delete .venv and try the launcher again."
    MsgBox msg, vbCritical, "IGA Marketing Master 2.0 -- Setup incomplete"
    WScript.Quit 1
End If

' ---- Setup complete: launch the GUI silently ----
shell.CurrentDirectory = projRoot
shell.Run """" & pythonw & """ -m iga_marketing_master_2.cli", 0, False
WScript.Quit 0
