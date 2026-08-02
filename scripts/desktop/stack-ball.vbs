' Launch the status ball with no console window.
' Double-click this, or drop a shortcut to it in shell:startup to have it come
' back after a reboot.
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & here & "\stack-ball.ps1""", 0, False
