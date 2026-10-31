@echo off
rem ============================================================================
rem  build.bat — сборка Windows-релиза HomeCamera (PyInstaller, --onedir).
rem  ЗАПУСКАТЬ НА WINDOWS. На macOS/Linux этот скрипт не работает
rem  (PyInstaller не умеет кросс-компиляцию) — для Mac есть build_mac.sh.
rem ============================================================================
setlocal
cd /d "%~dp0"

echo === HomeCamera v2.0: сборка (Windows) ===

where python >nul 2>nul
if errorlevel 1 (
    echo [!] Python не найден. Установите Python 3.10+ с python.org ^(Add to PATH^).
    pause & exit /b 1
)

if not exist venv (
    echo [*] Создаю виртуальное окружение...
    python -m venv venv
)
set "PY=venv\Scripts\python.exe"

echo [*] Ставлю зависимости (torch, insightface, ultralytics... это долго)...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r requirements.txt
"%PY%" -m pip install --upgrade pyinstaller

echo [*] Готовлю модель yolov8n.pt...
if not exist yolov8n.pt "%PY%" -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"

echo [*] Собираю PyInstaller (--onedir)...
"%PY%" -m PyInstaller --noconfirm --clean HomeCamera.spec
if errorlevel 1 (
    echo [!] Сборка не удалась.
    pause & exit /b 1
)

set "OUT=dist\HomeCamera"
echo [*] Докладываю ресурсы в сборку: конфиг, known/, ffmpeg.exe...
copy /y config.example.yaml "%OUT%\" >nul
if not exist "%OUT%\known" mkdir "%OUT%\known"
if exist known\*.jpg copy /y known\*.jpg "%OUT%\known\" >nul 2>nul
if exist known\*.png copy /y known\*.png "%OUT%\known\" >nul 2>nul
if exist known\.gitkeep copy /y known\.gitkeep "%OUT%\known\" >nul 2>nul

rem ffmpeg: ищем рядом с проектом или в tools\
if exist ffmpeg.exe copy /y ffmpeg.exe "%OUT%\ffmpeg.exe" >nul
if exist tools\ffmpeg.exe copy /y tools\ffmpeg.exe "%OUT%\ffmpeg.exe" >nul
if not exist "%OUT%\ffmpeg.exe" echo [!] ffmpeg.exe не найден — скачайте и положите рядом с build.bat (см. README).

echo.
echo Готово: %OUT%\HomeCamera.exe
echo Дальше: PACK_RELEASE.bat  (соберёт ZIP для раздачи)
endlocal
pause
