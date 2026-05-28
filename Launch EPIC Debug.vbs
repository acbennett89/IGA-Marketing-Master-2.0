' Launch EPIC Debug.vbs
' --------------------
' Opens an EPIC browser session for test scripts (no GUI app needed).
' Calls launch_browser.py which uses the SAME Playwright launch path as the GUI,
' so profile locking, lock-cleanup, and CDP port 9222 are all handled properly.
'
' Run by double-clicking. A console window will open showing the launch progress.
' Press Enter in that console (or close the browser) to end the session.

Option Explicit

Dim objShell, objFSO, projectDir, pythonExe, scriptPath, cmd
Set objShell = CreateObject("WScript.Shell")
Set objFSO   = CreateObject("Scripting.FileSystemObject")

' Project root = folder this .vbs lives in
projectDir = objFSO.GetParentFolderName(WScript.ScriptFullName)
pythonExe  = projectDir & "\.venv\Scripts\python.exe"
scriptPath = projectDir & "\launch_browser.py"

If Not objFSO.FileExists(pythonExe) Then
    MsgBox "Python venv not found at:" & vbCrLf & pythonExe & vbCrLf & vbCrLf & _
           "Create the venv first.", vbCritical, "Launch EPIC Debug"
    WScript.Quit 1
End If

If Not objFSO.FileExists(scriptPath) Then
    MsgBox "Launcher script not found at:" & vbCrLf & scriptPath, _
           vbCritical, "Launch EPIC Debug"
    WScript.Quit 1
End If

' Use cmd /k so the console window stays open showing Python output and any errors.
' Target: cmd /k ""<pythonExe>" "<scriptPath>""
' In VBScript, every literal " inside a string is written as "".
Dim q
q = Chr(34)  ' "
cmd = "cmd /k " & q & q & pythonExe & q & " " & q & scriptPath & q & q

' 1 = normal window, False = don't wait
objShell.CurrentDirectory = projectDir
objShell.Run cmd, 1, False
