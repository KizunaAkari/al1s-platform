Option Explicit

' GUI-subsystem launcher: no console is created for the scheduled action.
' Wait for PowerShell so task overlap protection, timeout and exit status remain effective.
Dim shell, files, pattern, hostname, container, script, powershell, command, result
If WScript.Arguments.Count <> 2 Then WScript.Quit 2
hostname = WScript.Arguments(0)
container = WScript.Arguments(1)
Set pattern = New RegExp
pattern.Pattern = "^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}\.local$"
If Not pattern.Test(hostname) Then WScript.Quit 2
pattern.Pattern = "^[a-zA-Z0-9][a-zA-Z0-9_.-]+$"
If Not pattern.Test(container) Then WScript.Quit 2
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
script = files.BuildPath(files.GetParentFolderName(WScript.ScriptFullName), "Sync-TerminalLanAddress.ps1")
If Not files.FileExists(script) Then WScript.Quit 3
powershell = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
command = Quote(powershell) & " -NoProfile -NonInteractive -WindowStyle Hidden -File " & Quote(script) & _
    " -TerminalHostname " & Quote(hostname) & " -ContainerName " & Quote(container)
result = shell.Run(command, 0, True)
WScript.Quit result

Function Quote(value)
    Quote = Chr(34) & value & Chr(34)
End Function
