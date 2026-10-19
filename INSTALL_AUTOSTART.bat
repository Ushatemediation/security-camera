@echo off
chcp 65001 >nul
rem ============================================================================
rem  INSTALL_AUTOSTART.bat — добавить камеру в автозапуск (Планировщик задач).
rem  Задача создаётся от имени ТЕКУЩЕГО пользователя (чтобы был доступ к камере
rem  и микрофону). При запуске запрашивает права администратора.
rem ============================================================================
setlocal
set "TASK=HomeCameraService"

rem --- запрос прав администратора при необходимости ---
net session >nul 2>&1
if errorlevel 1 (
    echo Запрашиваю права администратора...
    powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
    exit /b
)

cd /d "%~dp0"
set "EXE=%~dp0HomeCamera.exe"
if not exist "%EXE%" (
    echo [!] Не найден HomeCamera.exe рядом со скриптом.
    pause & exit /b 1
)
if not exist "config.yaml" (
    echo [!] Нет config.yaml — сначала запустите START_CAMERA.bat и впишите токен.
    pause & exit /b 1
)

echo Создаю задачу "%TASK%" (запуск при входе в систему, от текущего пользователя)...
schtasks /create /tn "%TASK%" /tr "\"%EXE%\"" /sc onlogon /rl highest /f
if errorlevel 1 (
    echo [!] Не удалось создать задачу.
    pause & exit /b 1
)

schtasks /run /tn "%TASK%" >nul 2>&1
echo.
echo   ✓ Автозапуск установлен. Камера будет стартовать при входе в систему.
echo   Снять: UNINSTALL.bat
pause
endlocal
