@echo off
chcp 65001 >nul
rem ============================================================================
rem  START_CAMERA.bat — запуск камеры в фоне. Имя exe может быть ЛЮБЫМ:
rem  скрипт сам находит первый .exe рядом (кроме ffmpeg.exe).
rem ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "EXE="
for %%f in (*.exe) do (
    if /i not "%%~nxf"=="ffmpeg.exe" if not defined EXE set "EXE=%%~ff"
)
if not defined EXE (
    echo [!] Рядом со скриптом нет ни одного .exe.
    echo     Распакуйте релизный архив целиком в одну папку.
    pause & exit /b 1
)

if not exist config.yaml (
    if exist "config.example.yaml" copy /y "config.example.yaml" "config.yaml" >nul
    echo [!] Создан config.yaml — впишите telegram.token и server.*
    notepad "config.yaml"
    echo Сохраните файл и запустите START_CAMERA.bat снова.
    pause & exit /b 0
)

echo Запускаю: !EXE!
rem Запуск БЕЗ окна консоли: через VBS (wscript — GUI-подсистема, чёрного окна нет).
if exist "%~dp0START_CAMERA.vbs" (
    start "" /b wscript //nologo "%~dp0START_CAMERA.vbs"
) else (
    start "" "!EXE!"
)
echo.
echo   ✓ Камера работает в фоне.
echo   Логи:        logs\app.log
echo   Записи:      recordings\
echo   Остановить:  UNINSTALL.bat
echo   Автозапуск:  INSTALL_AUTOSTART.bat
endlocal
