@echo off
rem start.bat — запуск охранной камеры в фоне под Windows (без окна консоли).
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\pythonw.exe" (
    echo [!] Окружение не найдено. Сначала запустите setup.bat
    pause & exit /b 1
)
if not exist "config.yaml" (
    echo [!] Нет config.yaml. Скопируйте config.example.yaml в config.yaml и впишите токен.
    pause & exit /b 1
)

echo Запускаю охранную камеру в фоне (без окна консоли)...
start "" "venv\Scripts\pythonw.exe" main.py
echo Готово. Камера работает в фоне. Логи: logs\app.log
echo Остановить: Диспетчер задач -> завершить pythonw.exe, либо: taskkill /im pythonw.exe /f
