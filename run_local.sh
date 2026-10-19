#!/usr/bin/env bash
# run_local.sh — локальный запуск охранной камеры (macOS / Linux).
#
# Использование:
#   ./run_local.sh                 # headless
#   ./run_local.sh --preview       # с окном превью (рамки, motion-скор)
#   ./run_local.sh --camera 1      # другая камера
#   ./run_local.sh --source file.mp4   # тест на видеофайле (без камеры)
#
# Скрипт сам находит venv (./venv, ../venv или ./.venv), проверяет ffmpeg,
# конфиг и эмбеддинги лиц, затем запускает main.py.
set -euo pipefail
cd "$(dirname "$0")"

# --- 1. Интерпретатор из venv -------------------------------------------------
PY=""
for cand in "./venv/bin/python" "../venv/bin/python" "./.venv/bin/python"; do
    if [ -x "$cand" ]; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
    echo "✗ Не найдено виртуальное окружение."
    echo "  Создайте его:   ./setup.sh      (или: python3 -m venv venv && venv/bin/pip install -r requirements.txt)"
    exit 1
fi

# --- 2. Проверки окружения ----------------------------------------------------
command -v ffmpeg >/dev/null 2>&1 \
    || echo "⚠  ffmpeg не найден — видео в Telegram уйдёт без перекодирования (brew install ffmpeg)"

if [ ! -f config.yaml ]; then
    echo "✗ Нет config.yaml. Создайте:  cp config.example.yaml config.yaml  и впишите токен бота."
    exit 1
fi

if [ ! -f known/embedding.npy ]; then
    echo "⚠  Нет known/embedding.npy — распознавание лиц не сработает."
    echo "   Положите фото в known/ и запустите:  $PY enroll.py"
fi

echo "▸ Запуск: $PY main.py $*"
echo "▸ Логи:   logs/app.log   (Ctrl+C — остановка)"
echo
exec "$PY" main.py "$@"
