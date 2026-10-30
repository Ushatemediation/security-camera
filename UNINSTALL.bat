@echo off
chcp 65001 >nul
rem ============================================================================
rem  UNINSTALL.bat — остановить камеру и убрать автозапуск.
rem  Имя exe может быть ЛЮБЫМ: скрипт находит его рядом (кроме ffmpeg.exe).
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

echo Останавливаю камеру и удаляю автозапуск "%TASK%"...
schtasks /end    /tn "%TASK%" >nul 2>&1
schtasks /delete /tn "%TASK%" /f >nul 2>&1

if defined EXE (
    for %%f in ("!EXE!") do taskkill /im "%%~nxf" /f >nul 2>&1
)
taskkill /im HomeCamera.exe /f >nul 2>&1

echo.
echo   ✓ Камера остановлена, автозапуск снят.
echo   (Файлы проекта, записи и фото не удаляются.)
pause
endlocal
