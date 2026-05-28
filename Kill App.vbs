Set oWMI = GetObject("winmgmts:\\.\root\cimv2")
Set oProcesses = oWMI.ExecQuery("SELECT * FROM Win32_Process WHERE Name = 'python.exe' OR Name = 'pythonw.exe'")

For Each oProcess In oProcesses
    Dim sCmdLine
    sCmdLine = oProcess.CommandLine
    If Not IsNull(sCmdLine) And Not IsEmpty(sCmdLine) Then
        If InStr(sCmdLine, "iga_marketing_master") > 0 Or InStr(sCmdLine, "iga-marketing-master") > 0 Then
            oProcess.Terminate()
        End If
    End If
Next
