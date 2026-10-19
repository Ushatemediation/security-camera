@echo off
rem setup.bat — установка «в одну команду» для Windows.
rem Создаёт venv, ставит зависимости, качает модели, готовит config.yaml.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [!] Python не найден. Установите Python 3.10+ с https://python.org
    echo     и при установке включите "Add Python to PATH".
    pause & exit /b 1
)

echo [1/4] Виртуальное окружение...
python -m venv venv
set "PY=venv\Scripts\python.exe"

echo [2/4] Зависимости (несколько минут: torch, onnxruntime, insightface...)...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r requirements.txt

echo [3/4] Модели (YOLOv8n + InsightFace buffalo_l, ~300 MB)...
if not exist yolov8n.pt "%PY%" -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"
"%PY%" -c "from insightface.app import FaceAnalysis; FaceAnalysis(name='buffalo_l', allowed_modules=['detection','recognition'])"

echo [4/4] Конфиг...
if not exist config.yaml copy config.example.yaml config.yaml

where ffmpeg >nul 2>nul || echo [!] ffmpeg не найден - скачайте с ffmpeg.org и добавьте в PATH

echo.
echo Готово. Дальше:
echo   1) впишите telegram.token и telegram.chat_id в config.yaml
echo   2) положите фото в known\ и выполните:  venv\Scripts\python.exe enroll.py
echo   3) запустите:  start.bat
pause
