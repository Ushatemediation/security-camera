# uninstall.ps1 — удаление задачи HomeCameraService.
# ЗАПУСКАТЬ ОТ ИМЕНИ АДМИНИСТРАТОРА.

$TaskName = "HomeCameraService"

Stop-ScheduledTask        -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask  -TaskName $TaskName -Confirm:$false
Write-Host "Задача '$TaskName' удалена."
