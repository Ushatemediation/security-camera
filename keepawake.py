"""Не давать системе уходить в сон, пока работает приложение (опция keep-awake).

Зачем: постоянная охранная камера должна работать, когда экран погашен и никто
не трогает компьютер. Иначе ОС усыпит ПК, процесс приостановится и события
перестанут приходить.

Платформы:
  * Windows — ``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)``
    из отдельного живущего потока (состояние привязано к потоку).
  * macOS  — демон ``caffeinate -i -s`` (не даёт уйти в простой/сон; на питании от сети).
  * Linux  — ``systemd-inhibit --what=idle:sleep`` (или ``caffeinate``, если есть).

Управление — ``config.yaml``::

    system:
      prevent_sleep: true
"""

import os
import shutil
import subprocess
import sys
import threading


class KeepAwake:
    """Держит систему бодрой, пока приложение запущено."""

    def __init__(self, cfg: dict, logger):
        self.cfg = cfg or {}
        self.logger = logger
        self.enabled = bool(self.cfg.get("prevent_sleep", True))
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.active = False

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if not self.enabled:
            self.logger.info("KeepAwake отключён (system.prevent_sleep: false)")
            return
        try:
            if os.name == "nt":
                self._start_windows()
            elif sys.platform == "darwin":
                self._start_subprocess("caffeinate", ["caffeinate", "-i", "-s"])
            else:
                self._start_linux()
        except Exception as e:
            self.logger.warning(f"KeepAwake не удалось включить: {e}")
            self.active = False

    def _start_windows(self) -> None:
        import ctypes

        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        kernel32 = ctypes.windll.kernel32

        def run():
            kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            # периодически подтверждаем запрос, пока поток жив
            while not self._stop.wait(60):
                kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            kernel32.SetThreadExecutionState(ES_CONTINUOUS)  # снять запрос

        self._thread = threading.Thread(target=run, daemon=True, name="KeepAwake")
        self._thread.start()
        self.active = True
        self.logger.info("KeepAwake: система не уйдёт в сон (Windows SetThreadExecutionState)")

    def _start_subprocess(self, name: str, cmd: list[str]) -> None:
        if not shutil.which(cmd[0]):
            self.logger.warning(f"KeepAwake: '{cmd[0]}' не найден — пропуск")
            return
        flags = 0
        if os.name == "nt":
            flags = 0x08000000  # CREATE_NO_WINDOW
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags
        )
        self.active = True
        self.logger.info(f"KeepAwake: система не уйдёт в сон ({name})")

    def _start_linux(self) -> None:
        if shutil.which("systemd-inhibit"):
            self._start_subprocess(
                "systemd-inhibit",
                ["systemd-inhibit", "--what=idle:sleep", "--why=HomeCamera",
                 "--mode=block", "sleep", "infinity"],
            )
        elif shutil.which("caffeinate"):
            self._start_subprocess("caffeinate", ["caffeinate", "-i"])
        else:
            self.logger.warning("KeepAwake: нет systemd-inhibit/caffeinate — пропуск")

    # ------------------------------------------------------------------ #
    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        if self.active:
            self.logger.info("KeepAwake: остановлен")
        self.active = False
