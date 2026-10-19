#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HomeCameraService — супервизор фонового приложения охранной камеры.

Работает БЕЗ консольного окна: либо интерпретатором `pythonw.exe`, либо как
собранный PyInstaller-exe `HomeCameraService.exe`. Держит приложение
(main.py) живым: если процесс упал/завершился — поднимает снова.

Два уровня перезапуска:
  1) Планировщик Windows (RestartOnFailure) — если упадёт сам супервизор.
  2) Этот супервизор — если упадёт/завершится дочернее приложение.

Путь к приложению:
  * аргумент командной строки:      pythonw supervisor.py D:\\path\\to\\main.py
  * переменная окружения:           HOME_CAMERA_APP=D:\\path\\to\\main.py
  * по умолчанию:                   <папка service/>/../main.py (корень проекта)
"""

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# Точка входа приложения (приоритет: argv > env > путь по умолчанию).
if len(sys.argv) > 1:
    APP = Path(sys.argv[1]).resolve()
elif os.environ.get("HOME_CAMERA_APP"):
    APP = Path(os.environ["HOME_CAMERA_APP"]).resolve()
else:
    APP = (SCRIPT_DIR.parent / "main.py").resolve()

APP_DIR = APP.parent
RESTART_DELAY = 10          # пауза между перезапусками, сек
BACKOFF_MAX = 60            # максимальная пауза, если падает подряд
STABLE_AFTER = 60           # проработало >= столько сек — считаем «стабильным»
LOG = SCRIPT_DIR / "service.log"

# 0x08000000 = CREATE_NO_WINDOW: у дочернего процесса не будет окна консоли.
CREATE_NO_WINDOW = 0x08000000

logging.basicConfig(
    filename=str(LOG),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("HomeCameraService")

_stop = False


def _handle_stop(signum, frame):
    global _stop
    _stop = True
    log.info("Получен сигнал %s — завершаюсь", signum)


def _child_command() -> list[str]:
    """Команда запуска приложения.

    Если приложение собрано в HomeCameraService.exe — запускаем его напрямую.
    Иначе ищем pythonw.exe рядом с текущим интерпретатором (без окна консоли).
    """
    exe = APP_DIR / "HomeCameraService.exe"
    if exe.exists():
        return [str(exe)]
    exe_dir = Path(sys.executable).parent
    pyw = exe_dir / ("pythonw.exe" if os.name == "nt" else "pythonw")
    interpreter = str(pyw) if pyw.exists() else sys.executable
    return [interpreter, str(APP)]


def main() -> None:
    signal.signal(signal.SIGINT, _handle_stop)
    try:
        signal.signal(signal.SIGTERM, _handle_stop)
    except (ValueError, AttributeError):
        pass

    log.info("Супервизор запущен (pid=%s, python=%s)", os.getpid(), sys.executable)
    log.info("Приложение: %s", APP)

    delay = RESTART_DELAY
    while not _stop:
        cmd = _child_command()
        log.info("Запуск: %s", " ".join(cmd))
        started = time.time()
        popen_kwargs = {}
        if os.name == "nt":
            # CREATE_NO_WINDOW поддерживается только на Windows.
            popen_kwargs["creationflags"] = CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(APP_DIR),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **popen_kwargs,
            )
        except Exception:
            log.exception("Не удалось запустить приложение")
            proc = None

        if proc is not None:
            rc = None
            while rc is None and not _stop:
                try:
                    rc = proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    continue
            if rc is None:
                log.info("Остановка — завершаю дочерний процесс")
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            else:
                log.warning("Приложение завершилось, код=%s", rc)
                ran = time.time() - started
                delay = RESTART_DELAY if ran >= STABLE_AFTER else min(BACKOFF_MAX, delay * 2)

        if _stop:
            break
        log.info("Перезапуск через %s сек", delay)
        end = time.time() + delay
        while time.time() < end and not _stop:
            time.sleep(0.5)

    log.info("Супервизор остановлен")


if __name__ == "__main__":
    main()
