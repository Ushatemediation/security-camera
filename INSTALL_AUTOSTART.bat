@echo off
chcp 65001 >nul
rem ============================================================================
rem  INSTALL_AUTOSTART.bat — автозапуск через Планировщик задач.
rem  Имя exe может быть ЛЮБЫМ: скрипт находит первый .exe рядом (кроме ffmpeg).
rem  Задача создаётся от имени ТЕКУЩЕГО пользователя (доступ к камере/микрофону).
rem ============================================================================
setlocal enabledelayedexpansion
set "TASK=HomeCameraService"

net session >nul 2>&1
if errorlevel 1 (
    echo Запрашиваю права администратора...
    powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
    exit /b
)

cd /d "%~dp0"

set "EXE="
for %%f in (*.exe) do (
    if /i not "%%~nxf"=="ffmpeg.exe" if not defined EXE set "EXE=%%~ff"
)
if not defined EXE (
    echo [!] Рядом со скриптом нет ни одного .exe.
    pause & exit /b 1
)
if not exist config.yaml (
    echo [!] Нет config.yaml — сначала запустите START_CAMERA.bat.
    pause & exit /b 1
)

echo Программа : !EXE!
echo Задача    : %TASK%
schtasks /create /tn "%TASK%" /tr "\"!EXE!\"" /sc onlogon /rl highest /f
if errorlevel 1 (
    echo [!] Не удалось создать задачу.
    pause & exit /b 1
)

schtasks /run /tn "%TASK%" >nul 2>&1
echo.
echo   ✓ Автозапуск включён: задача "%TASK%"
echo   Проверить: schtasks /query /tn %TASK%
echo   Снять:     UNINSTALL.bat
pause
endlocal
