@echo off
chcp 65001 >nul
rem ============================================================================
rem  UNINSTALL.bat — удалить автозапуск и остановить камеру.
rem ============================================================================
setlocal
set "TASK=HomeCameraService"

net session >nul 2>&1
if errorlevel 1 (
    echo Запрашиваю права администратора...
    powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
    exit /b
)

echo Останавливаю камеру и удаляю автозапуск "%TASK%"...
schtasks /end    /tn "%TASK%" >nul 2>&1
schtasks /delete /tn "%TASK%" /f >nul 2>&1
taskkill /im HomeCamera.exe /f >nul 2>&1

echo.
echo   ✓ Автозапуск удалён, камера остановлена.
echo   (Файлы проекта, записи и фото не удаляются.)
pause
endlocal
