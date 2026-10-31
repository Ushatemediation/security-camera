"""camwatch.py — кто ещё использует веб-камеру на этом компьютере.

Windows ведёт журнал доступа к камере в реестре:
  HKCU/HKLM \\Software\\Microsoft\\Windows\\CurrentVersion\\
      CapabilityAccessManager\\ConsentStore\\webcam[\\NonPackaged]\\<приложение>
где у каждого приложения есть:
  LastUsedTimeStart — когда начало использовать (FILETIME)
  LastUsedTimeStop  — когда закончило; 0 = ИСПОЛЬЗУЕТ ПРЯМО СЕЙЧАС

Модуль опрашивает журнал раз в несколько секунд и сообщает, когда камеру
захватило другое приложение (или когда она освободилась).
"""

import datetime
import os
import sys
import threading
import time

try:
    import winreg
except ImportError:  # не Windows
    winreg = None

SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam"
ROOTS = [
    ("HKCU", "HKEY_CURRENT_USER", ""),
    ("HKCU", "HKEY_CURRENT_USER", r"\NonPackaged"),
    ("HKLM", "HKEY_LOCAL_MACHINE", ""),
    ("HKLM", "HKEY_LOCAL_MACHINE", r"\NonPackaged"),
]


def _hive(name):
    return winreg.HKEY_CURRENT_USER if name == "HKEY_CURRENT_USER" else winreg.HKEY_LOCAL_MACHINE


def _iter_apps(hive_name: str, suffix: str):
    hive = _hive(hive_name)
    path = SUBKEY + suffix
    try:
        key = winreg.OpenKey(hive, path)
    except OSError:
        return
    try:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(key, i)
            except OSError:
                break
            i += 1
            yield hive, path + "\\" + name, name
    finally:
        key.Close()


def _friendly(name: str) -> str:
    """Имя ключа — путь к exe; оставляем только имя файла."""
    base = name.replace("#", "\\").rstrip("\\")
    tail = base.split("\\")[-1]
    return tail or name


def active_users():
    """Список (имя, когда начали) приложений, которые ДЕРЖАТ камеру сейчас."""
    if winreg is None or os.name != "nt":
        return []
    now = []
    for hive_name, suffix in [(r[1], r[2]) for r in ROOTS]:
        for hive, sub, name in _iter_apps(hive_name, suffix):
            try:
                key = winreg.OpenKey(hive, sub)
            except OSError:
                continue
            try:
                start, _ = winreg.QueryValueEx(key, "LastUsedTimeStart")
                stop, _ = winreg.QueryValueEx(key, "LastUsedTimeStop")
            except OSError:
                continue
            finally:
                key.Close()
            if stop == 0:
                when = None
                try:
                    when = datetime.datetime(1601, 1, 1) + datetime.timedelta(microseconds=start / 10)
                except Exception:
                    pass
                now.append((_friendly(name), when))
    return now


class CameraWatcher(threading.Thread):
    """Фоновый поток: сообщает, когда камеру заняло/освободило другое приложение."""

    def __init__(self, logger, stop_event, interval: float = 5.0, on_change=None):
        super().__init__(daemon=True, name="CameraWatcher")
        self.logger = logger
        self.stop_event = stop_event
        self.interval = interval
        self.on_change = on_change
        self._known: dict[str, bool] = {}

    def run(self) -> None:
        if winreg is None or os.name != "nt":
            self.logger.info("CameraWatcher: не Windows — наблюдение выключено")
            return

        while not self.stop_event.is_set():
            try:
                users = {name: when for name, when in active_users()}
            except Exception as e:
                self.logger.debug(f"CameraWatcher: ошибка чтения реестра: {e}")
                users = {}

            for name in users:
                if not self._known.get(name, False):
                    when = users[name]
                    stamp = when.strftime("%H:%M:%S") if when else "?"
                    self.logger.warning(f"CameraWatcher: камеру использует «{name}» (с {stamp})")
                    if self.on_change:
                        try:
                            self.on_change(name, True)
                        except Exception:
                            pass

            for name in list(self._known):
                if self._known[name] and name not in users:
                    self.logger.info(f"CameraWatcher: «{name}» освободил камеру")
                    if self.on_change:
                        try:
                            self.on_change(name, False)
                        except Exception:
                            pass

            self._known = {name: True for name in users}
            self.stop_event.wait(self.interval)
