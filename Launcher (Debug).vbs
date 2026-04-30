' ============================================================
' Launcher (Debug).vbs — IGA Marketing Master 2.0
' ------------------------------------------------------------
' Same as Launcher.vbs, but adds the --debug flag. Use this
' when you want:
'   - Verbose console + file logging (DEBUG level)
'   - Raw Anthropic API request/response JSON saved per call
'   - Playwright trace.zip + screenshots before/after each
'     entry action saved under <Client>/debug/
'
' Debug artifacts are auto-pruned to the last 5 runs per client.
' ============================================================

Option Explicit

Dim shell, fso, projRoot, pythonw, cmd

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

projRoot = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = projRoot & "\.venv\Scripts\pythonw.exe"

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

cmd = """" & pythonw & """ -m iga_marketing_master_2.cli --debug"

shell.CurrentDirectory = projRoot
shell.Run cmd, 0, False
