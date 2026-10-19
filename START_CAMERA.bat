@echo off
chcp 65001 >nul
rem ============================================================================
rem  START_CAMERA.bat — запуск охранной камеры в фоне (без чёрного окна консоли).
rem ============================================================================
setlocal
cd /d "%~dp0"

if not exist "HomeCamera.exe" (
    echo [!] Не найден HomeCamera.exe.
    echo     Соберите проект ^(build.bat^) или распакуйте релизный ZIP целиком.
    pause & exit /b 1
)

if not exist "config.yaml" (
    if exist "config.example.yaml" copy /y "config.example.yaml" "config.yaml" >nul
    echo [!] Создан config.yaml — впишите telegram.token и telegram.chat_id.
    notepad "config.yaml"
    echo Сохраните файл и запустите START_CAMERA.bat снова.
    pause & exit /b 0
)

echo Запускаю HomeCamera в фоне...
start "" "HomeCamera.exe"
echo.
echo   ✓ Камера работает в фоне.
echo   Логи:        logs\app.log
echo   Остановить:  taskkill /im HomeCamera.exe /f   (или UNINSTALL.bat)
echo   Автозапуск:  INSTALL_AUTOSTART.bat
endlocal
