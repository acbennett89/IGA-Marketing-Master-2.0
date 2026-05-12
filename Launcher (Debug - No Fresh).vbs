' ============================================================
' Launcher (Debug - No Fresh).vbs -- IGA Marketing Master 2.0
' ------------------------------------------------------------
' Same as Launcher (Debug).vbs but WITHOUT --fresh, so the
' existing diagnostic client state is preserved across runs:
'
'     1. --debug: verbose logs, raw Claude request/response,
'        Playwright trace.zip + screenshots per entry action
'     2. --client _DIAGNOSTIC: auto-selects the diagnostic
'        client (skips the picker)
'     3. --working-library: points at the in-repo diagnostic
'        workspace under "Testing and Example Library"
'
' Use this launcher when iterating on GUI / review behaviour
' without re-running extraction each time.
'
' Debug artifacts live under <Working Library>\<Client>\debug\
' and are auto-pruned to the last 5 runs per client.
'
' This file is pure ASCII to avoid any Windows codepage
' surprises with non-ASCII characters in MsgBox dialogs.
' ============================================================

Option Explicit

Dim shell, fso, projRoot, pythonw, workingLib, queuePdfs, cmd
Dim exitCode, response, msg

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

projRoot   = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw    = projRoot & "\.venv\Scripts\pythonw.exe"
workingLib = projRoot & "\Testing and Example Library\diagnostic_workspace"
queuePdfs  = projRoot & "\Testing and Example Library\diagnostic_inputs"

' Build the launch command once -- used by both the fast path and the
' post-bootstrap path below.
'   Q = a literal double-quote (VBS embeds " as "" inside string literals)
Dim Q : Q = """"
cmd = Q & pythonw & Q & " -m iga_marketing_master_2.cli" & _
      " --debug --client _DIAGNOSTIC" & _
      " --working-library " & Q & workingLib & Q & _
      " --queue-pdfs "      & Q & queuePdfs  & Q

' ---- Fast path ----
If fso.FileExists(pythonw) Then
    shell.CurrentDirectory = projRoot
    shell.Run cmd, 0, False
    WScript.Quit 0
End If

' ---- First-run confirm ----
msg = "First-time setup is needed. The launcher will install:" & vbCrLf & vbCrLf & _
      "    1. A Python virtual environment (.venv)" & vbCrLf & _
      "    2. All required Python packages" & vbCrLf & _
      "    3. Playwright's Chromium browser (downloaded once)" & vbCrLf & vbCrLf & _
      "This may take a few minutes. A PowerShell window will open" & vbCrLf & _
      "to show progress. After setup completes, the app will launch" & vbCrLf & _
      "in DEBUG mode." & vbCrLf & vbCrLf & _
      "Continue?"
response = MsgBox(msg, vbYesNo + vbQuestion, _
    "IGA Marketing Master 2.0 -- First-time setup (Debug)")
If response <> vbYes Then
    WScript.Quit 0
End If

' ---- Python 3.13 pre-check ----
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

' ---- Run bootstrap visibly ----
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

' ---- Verify and launch in debug mode ----
If Not fso.FileExists(pythonw) Then
    msg = "Setup reported success, but pythonw.exe was not found at:" & vbCrLf & vbCrLf & _
          "    " & pythonw & vbCrLf & vbCrLf & _
          "This is unexpected. Check the bootstrap output for clues, or" & vbCrLf & _
          "delete .venv and try the launcher again."
    MsgBox msg, vbCritical, "IGA Marketing Master 2.0 -- Setup incomplete"
    WScript.Quit 1
End If

shell.CurrentDirectory = projRoot
shell.Run cmd, 0, False
WScript.Quit 0
