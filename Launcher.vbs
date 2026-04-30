' ============================================================
' Launcher.vbs — IGA Marketing Master 2.0
' ------------------------------------------------------------
' Double-click this file to start the application silently
' (no console window). Logs go to the global log location and
' to the per-client runs.log inside Working Library.
'
' Requirements:
'   1. scripts\bootstrap.ps1 must have been run once to create
'      .venv with all dependencies installed.
'   2. ANTHROPIC_API_KEY must be set in the environment OR
'      stored via the GUI's first-run API-key prompt.
'
' For verbose logging + Playwright tracing + retained debug
' artifacts, double-click "Launcher (Debug).vbs" instead.
' ============================================================

Option Explicit

Dim shell, fso, projRoot, pythonw, cmd

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Resolve the project root from this script's own location.
projRoot = fso.GetParentFolderName(WScript.ScriptFullName)

' pythonw.exe runs Python without opening a console window.
pythonw = projRoot & "\.venv\Scripts\pythonw.exe"

' Verify the venv has been bootstrapped before launching.
If Not fso.FileExists(pythonw) Then
    MsgBox _
        "The Python virtual environment was not found at:" & vbCrLf & vbCrLf & _
        "    " & pythonw & vbCrLf & vbCrLf & _
        "Run the bootstrap script first by opening PowerShell, navigating to the project folder, and running:" & vbCrLf & vbCrLf & _
        "    .\scripts\bootstrap.ps1" & vbCrLf & vbCrLf & _
        "Then double-click this launcher again.", _
        vbCritical, "IGA Marketing Master 2.0 — Setup required"
    WScript.Quit 1
End If

' Build the launch command.
'   "C:\path\to\pythonw.exe" -m iga_marketing_master_2.cli
cmd = """" & pythonw & """ -m iga_marketing_master_2.cli"

' Run with the project root as the working directory so any
' relative path resolution inside the app behaves consistently.
shell.CurrentDirectory = projRoot

' shell.Run arguments:
'   cmd                        — the command to run
'   0                          — window style: hidden (no flash)
'   False                      — do not wait for the process to exit
shell.Run cmd, 0, False
