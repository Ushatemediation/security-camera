#!/usr/bin/env bash
# ============================================================================
#  build_mac.sh — сборка на macOS ЛИБО Linux.
#
#  ВАЖНО: PyInstaller НЕ умеет кросс-компиляцию. Здесь собирается бинарник
#  ДЛЯ ТЕКУЩЕЙ ОС (для проверки логики на Mac), а не .exe для Windows.
#  Windows-релиз собирается build.bat на Windows или через GitHub Actions.
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

# --- интерпретатор из venv -------------------------------------------------
if [ -x venv/bin/python ]; then
    PYBIN=venv/bin/python
elif [ -x ../venv/bin/python ]; then
    PYBIN=../venv/bin/python
else
    echo "▸ Создаю venv..."
    "${PYTHON:-python3}" -m venv venv
    PYBIN=venv/bin/python
    "$PYBIN" -m pip install -q -r requirements.txt
fi

echo "▸ Ставлю PyInstaller..."
"$PYBIN" -m pip install -q --upgrade pyinstaller

echo "▸ Готовлю модель yolov8n.pt..."
[ -f yolov8n.pt ] || "$PYBIN" -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"

echo "▸ Сборка (--onedir)..."
"$PYBIN" -m PyInstaller --noconfirm --clean HomeCamera.spec

OUT="dist/HomeCamera"
cp config.example.yaml "$OUT/"
mkdir -p "$OUT/known"
[ -f known/.gitkeep ] && cp known/.gitkeep "$OUT/known/"

echo
echo "✓ Готово: $OUT/"
echo "  Проверка на Mac:"
echo "    $OUT/HomeCamera enroll            # зарегистрировать лица из known/"
echo "    $OUT/HomeCamera --preview         # запуск с окном превью"
echo "    $OUT/HomeCamera selftest          # проверка Telegram"
