#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest.py — проверка отправки в Telegram: фото + короткое видео со звуком.

Нужен заполненный config.yaml (telegram.token и telegram.chat_id) и доступ к
камере/микрофону. Снимает ~4 секунды с камеры, шлёт снимок и видео со звуком.

Запуск:
    ./venv/bin/python selftest.py      (или из общего venv: ../venv/bin/python)
"""

import logging
import sys
import threading
import time
from pathlib import Path

import cv2
import yaml

from audio import AudioRecorder
from notifier import TelegramNotifier
from paths import APP_DIR

BASE = APP_DIR


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("selftest")

    cfg_path = BASE / "config.yaml"
    if not cfg_path.exists():
        print("✗ Нет config.yaml — скопируйте config.example.yaml и впишите token/chat_id")
        return 1
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    stop = threading.Event()
    audio = AudioRecorder(cfg.get("audio", {}) or {}, logger, BASE, stop)
    audio.start()
    notifier = TelegramNotifier(cfg["telegram"], logger, BASE, stop, audio=audio)
    if not notifier.enabled:
        print("✗ Telegram отключён: впишите telegram.token и telegram.chat_id в config.yaml")
        return 1

    # --- камера ---
    src = cfg["camera"]["device_id"]
    src = int(src) if (isinstance(src, str) and str(src).isdigit()) else src
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        print("✗ Камера не открылась. На macOS разрешите доступ терминалу (TCC → Камера).")
        return 1

    ok, frame = cap.read()
    if not ok:
        print("✗ Не удалось прочитать кадр с камеры")
        cap.release()
        return 1

    # 1) фото — быстрое уведомление
    print("→ Отправляю тестовое фото...")
    notifier.send_snapshot(frame, "Selftest: фото (проверка Telegram)")

    # 2) видео ~4 секунды со звуком
    print("→ Записываю ~4с видео и отправляю со звуком...")
    h, w = frame.shape[:2]
    tmp = BASE / "_selftest_clip.mp4"
    vw = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"mp4v"), 20.0, (w, h))
    t_start = time.time()
    t_end = t_start
    for _ in range(80):
        ok, fr = cap.read()
        if not ok:
            break
        vw.write(fr)
        t_end = time.time()
    vw.release()
    cap.release()

    notifier.enqueue_video(
        tmp,
        "Selftest: видео со звуком (микрофон)",
        audio_window=(t_start, t_end),
        replace_source=False,
    )

    # --- даём воркеру отправить фото и видео ---
    for _ in range(60):
        time.sleep(1)
        if notifier._queue.empty():
            # очередь пуста + ещё пару секунд на финальную отправку
            time.sleep(3)
            break

    stop.set()
    notifier.shutdown()
    audio.shutdown()

    for p in (tmp, BASE / "logs"):
        pass
    tmp.unlink(missing_ok=True)
    print("\n✓ Готово. Проверьте Telegram: должно прийти фото, затем видео со звуком.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
