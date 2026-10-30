@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ==================================================
echo   HOME CAMERA - ДИАГНОСТИКА
echo ==================================================
echo Дата:  %DATE% %TIME%
echo Папка: %CD%
echo.

echo --- 1. ФАЙЛЫ -------------------------------------------------
if exist HomeCamera.exe (echo   HomeCamera.exe ....... ЕСТЬ) else (echo   HomeCamera.exe ....... НЕТ !)
if exist _internal     (echo   _internal ............ ЕСТЬ) else (echo   _internal ............ НЕТ !)
if exist config.yaml   (echo   config.yaml .......... ЕСТЬ) else (echo   config.yaml .......... НЕТ !)
if exist known         (echo   known\ ............... ЕСТЬ) else (echo   known\ ............... НЕТ !)
if exist logs\app.log  (echo   logs\app.log ......... ЕСТЬ) else (echo   logs\app.log ......... НЕТ)
echo.

echo --- 2. ФОТО В known\ -----------------------------------------
dir /b known 2>nul
echo.

echo --- 3. МОДЕЛИ -------------------------------------------------
if exist yolov8n.pt (echo   yolov8n.pt ........... ЕСТЬ) else (echo   yolov8n.pt ........... НЕТ - нужна загрузка^)
if exist "%USERPROFILE%\.insightface\models\buffalo_l" (echo   buffalo_l ............ ЕСТЬ) else (echo   buffalo_l ............ НЕТ - нужна загрузка^)
echo.

echo --- 4. ПРОЦЕСС ------------------------------------------------
tasklist /fi "imagename eq HomeCamera.exe" 2>nul | findstr /i "HomeCamera" || echo   Процесс НЕ запущен
echo.

echo --- 5. КОНФИГ (секреты не показываю) --------------------------
powershell -NoProfile -Command "$c = Get-Content config.yaml -Raw -ErrorAction SilentlyContinue; if (-not $c) { 'config.yaml не читается'; exit }; if ($c -match 'token:\s*[^\"''\s]') { 'token:     заполнен' } else { 'token:     ПУСТОЙ (!)' }; if ($c -match 'chat_ids:\s*\[?\s*\d') { 'chat_ids:  заполнены' } else { 'chat_ids:  ПУСТЫЕ (!)' }; if ($c -match 'device_id:\s*(\d+)') { 'device_id: ' + $matches[1] }"
echo.

echo --- 6. ПОСЛЕДНИЕ 30 СТРОК ЛОГА --------------------------------
powershell -NoProfile -Command "if (Test-Path 'logs\app.log') { Get-Content 'logs\app.log' -Tail 30 } else { 'ЛОГА НЕТ - приложение ни разу не стартовало' }"
echo.

echo --- 7. ЗАДАЧА АВТОЗАПУСКА -------------------------------------
schtasks /query /tn HomeCameraService /fo LIST 2>nul | findstr /i "TaskName Status Last" || echo   Задача HomeCameraService не создана
echo.
echo ==================================================
echo   КОНЕЦ. Скопируй весь текст выше и пришли мне.
echo ==================================================
pause
