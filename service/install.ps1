param(
    # Папка с приложением (корень проекта). По умолчанию — папка уровнем выше.
    [string]$ProjectDir = (Join-Path $PSScriptRoot ".."),
    [string]$TaskName   = "HomeCameraService",
    # "SYSTEM" — фон без входа пользователя; либо "DOMAIN\User" (LogonType S4U).
    [string]$User       = "SYSTEM",
    # Если пусто — ищем venv\Scripts\pythonw.exe, затем pythonw.exe в PATH.
    [string]$Pythonw    = ""
)
# install.ps1 — регистрация задачи HomeCameraService в Планировщике Windows.
# ЗАПУСКАТЬ ОТ ИМЕНИ АДМИНИСТРАТОРА:
#   powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path $ProjectDir).Path
$Supervisor = Join-Path $PSScriptRoot "supervisor.py"
$AppEntry   = Join-Path $ProjectDir "main.py"
$Exe        = Join-Path $ProjectDir "$TaskName.exe"

# 1. Проверка прав администратора
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Запустите скрипт от имени администратора." }

foreach ($p in @($Supervisor, $AppEntry)) {
    if (-not (Test-Path $p)) { throw "Не найден файл: $p" }
}

# 2. Выбор способа запуска: собранный exe > pythonw.exe + supervisor.
if (Test-Path $Exe) {
    $exec = $Exe
    $arg  = ""
    Write-Host "Режим запуска: собранный exe -> $Exe (имя процесса HomeCameraService)"
} else {
    if (-not $Pythonw) {
        $cand = Join-Path $ProjectDir "venv\Scripts\pythonw.exe"
        if (Test-Path $cand) { $Pythonw = $cand }
        else { $Pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source }
    }
    if (-not $Pythonw -or -not (Test-Path $Pythonw)) {
        throw "Не найден pythonw.exe. Укажите его через -Pythonw 'C:\path\pythonw.exe'."
    }
    $exec = $Pythonw
    $arg  = "`"$Supervisor`" `"$AppEntry`""
    Write-Host "Режим запуска: pythonw.exe + supervisor.py (без окна консоли)"
}

# 3. Действие (без окна: pythonw.exe либо --noconsole exe)
$action = New-ScheduledTaskAction `
    -Execute $exec `
    -Argument $arg `
    -WorkingDirectory $ProjectDir

# 4. Триггер: при загрузке системы (+30 сек на инициализацию сети/USB/камеры)
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT30S"

# 5. Принципал
if ($User -eq "SYSTEM") {
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" `
        -LogonType ServiceAccount -RunLevel Highest
} else {
    $principal = New-ScheduledTaskPrincipal -UserId $User `
        -LogonType S4U -RunLevel Highest
}

# 6. Настройки: автоперезапуск при падении + без лимита времени
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1)

# 7. Регистрация (перезапись, если задача уже есть)
Register-ScheduledTask `
    -TaskName    $TaskName `
    -Action      $action `
    -Trigger     $trigger `
    -Principal   $principal `
    -Settings    $settings `
    -Description "Home security camera background service (headless)" `
    -Force | Out-Null

# 8. Запуск и проверка
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
