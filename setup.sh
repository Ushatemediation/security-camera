#!/usr/bin/env bash
# setup.sh — установка «в одну команду» для macOS / Linux.
# Создаёт venv, ставит зависимости, качает модели, готовит config.yaml.
set -euo pipefail
cd "$(dirname "$0")"

PYBIN="${PYTHON:-python3}"
if ! command -v "$PYBIN" >/dev/null 2>&1; then
    echo "✗ Python 3 не найден. Установите Python 3.10+ (https://python.org)."
    exit 1
fi

echo "▸ [1/4] Виртуальное окружение (venv/)"
"$PYBIN" -m venv venv
PY=venv/bin/python

echo "▸ [2/4] Зависимости (несколько минут: torch, onnxruntime, insightface…)"
"$PY" -m pip install --upgrade pip
"$PY" -m pip install -r requirements.txt

echo "▸ [3/4] Модели (YOLOv8n + InsightFace buffalo_l, ~300 МБ)"
[ -f yolov8n.pt ] || "$PY" -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"
"$PY" -c "from insightface.app import FaceAnalysis; FaceAnalysis(name='buffalo_l', allowed_modules=['detection','recognition'])"

echo "▸ [4/4] Конфиг"
if [ ! -f config.yaml ]; then
    cp config.example.yaml config.yaml
    echo "  создан config.yaml — впишите telegram.token и telegram.chat_id"
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "⚠ ffmpeg не найден — видео уйдёт без звука/перекодирования."
    echo "  macOS: brew install ffmpeg   |   Linux: sudo apt install ffmpeg"
fi

echo
echo "✓ Установка завершена. Дальше:"
echo "   1) впишите telegram.token и telegram.chat_id в config.yaml"
echo "   2) положите фото людей в known/ и выполните:  $PY enroll.py"
echo "   3) запустите:  ./start.sh   (или ./start.sh --preview)"
