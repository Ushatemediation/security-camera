' HomeCamera: запуск без окна консоли.
' Двойной клик по этому файлу = приложение работает в фоне, никаких чёрных окон.
Option Explicit

Dim fso, sh, dir, f, exe
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir

exe = ""
For Each f In fso.GetFolder(dir).Files
    If LCase(fso.GetExtensionName(f.Name)) = "exe" Then
        If LCase(f.Name) <> "ffmpeg.exe" Then
            exe = f.Path
            Exit For
        End If
    End If
Next

If exe = "" Then
    MsgBox "HomeCamera.exe not found. Unpack the whole release archive into one folder.", 16, "HomeCamera"
    WScript.Quit 1
End If

' 0 = скрытое окно, False = не ждать завершения
sh.Run """" & exe & """", 0, False
