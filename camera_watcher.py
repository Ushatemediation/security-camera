"""camera_watcher.py — мониторинг доступа к камере на Windows (дебаг-заготовка).

Зачем
-----
Нужно видеть, когда ДРУГИЕ программы (или система) берут веб-камеру в работу:
OBS, Zoom, браузер, «Камера» Windows и т.п. Это автономный инструмент для
отладки: опрашивает журнал доступа Windows и печатает события захвата/освобождения.

Где Windows это хранит
----------------------
Реестр, ``CapabilityAccessManager/ConsentStore`` (запись ведётся самим Windows
для всех приложений, включая обычные .exe):

    HKLM|HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\
        CapabilityAccessManager\\ConsentStore\\webcam
            <UWP-пакет>\\             LastUsedTimeStart, LastUsedTimeStop
            NonPackaged\\<путь>.exe   то же для классических программ
                                      (в имени ключа '\\' заменён на '#',
                                       а ':' на '!!')

    * LastUsedTimeStart — когда приложение начало пользоваться камерой,
      FILETIME (100-нс тики с 1601-01-01 UTC); 0 = ещё не пользовалось.
    * LastUsedTimeStop == 0 и Start != 0  →  камера ИСПОЛЬЗУЕТСЯ ПРЯМО СЕЙЧАС.
    * В корне ключа webcam — Value: "Allow"/"Deny" (глобальный доступ к камере).

Монитор: периодически читает записи, сравнивает «кто держит камеру сейчас»
с прошлым опросом и печатает события:

    [+] obs64.exe (с 12:00:05) — камера захвачена
    [-] obs64.exe — камера освобождена

Запуск (Windows):
    python camera_watcher.py                  # следить и печатать события
    python camera_watcher.py --interval 2     # период опроса, сек
    python camera_watcher.py --once           # разовый отчёт: кто держит сейчас
    python camera_watcher.py --all            # все записи журнала, включая неактивные
    python camera_watcher.py --skip HomeCamera.exe    # не логировать себя
    python camera_watcher.py --demo           # демо логики без Windows (macOS/Linux)

ВАЖНО: в проекте уже есть интегрированная версия той же логики — camwatch.py
(фоновый поток внутри main.py, пишет в лог приложения). ЭТОТ файл — автономная
дебаг-заготовка: запускается отдельно, печатает всё, что видит в реестре, и
служит полигоном, прежде чем что-то встраивать в основной цикл.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time

try:
    import winreg
except ImportError:  # не Windows
    winreg = None

CONSENT = (
    r"Software\Microsoft\Windows\CurrentVersion"
    r"\CapabilityAccessManager\ConsentStore\webcam"
)
HIVES = {"HKCU": "HKEY_CURRENT_USER", "HKLM": "HKEY_LOCAL_MACHINE"}
FILE_EPOCH = dt.datetime(1601, 1, 1)


# ------------------------------------------------------------- утилиты --

def _filetime_to_dt(value: int):
    """FILETIME (100-нс тики с 1601-01-01) -> datetime или None."""
    if not value:
        return None
    try:
        return FILE_EPOCH + dt.timedelta(microseconds=value / 10)
    except (OverflowError, OSError, ValueError):
        return None


def _hive_handle(name: str):
    return winreg.HKEY_CURRENT_USER if name == "HKCU" else winreg.HKEY_LOCAL_MACHINE


def _iter_app_keys(hive_name: str, suffix: str = ""):
    """Перебрать подключи приложений: yield (hive_name, полный_путь, имя_ключа)."""
    if winreg is None:
        return
    path = CONSENT + suffix
    try:
        key = winreg.OpenKey(_hive_handle(hive_name), path)
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
            yield hive_name, path + "\\" + name, name
    finally:
        key.Close()


def _read_times(hive_name: str, sub_path: str):
    """(start, stop) как int; None, если значений нет."""
    try:
        key = winreg.OpenKey(_hive_handle(hive_name), sub_path)
    except OSError:
        return None
    try:
        try:
            start, _ = winreg.QueryValueEx(key, "LastUsedTimeStart")
            stop, _ = winreg.QueryValueEx(key, "LastUsedTimeStop")
            return int(start), int(stop)
        except OSError:
            return None
    finally:
        key.Close()


def _app_name(key_name: str) -> str:
    """Имя ключа -> человекочитаемое имя приложения.

    У NonPackaged имя — путь к exe, где '\\' заменён на '#', ':' на '!!'.
    """
    raw = key_name.replace("!!", ":").replace("#", "\\").rstrip("\\")
    tail = raw.split("\\")[-1]
    return tail or key_name


# -------------------------------------------------------------- скан --

def scan_consumers(active_only: bool = True):
    """Снимок журнала: список приложений и их доступ к камере.

    Возвращает list[dict]: name, key, start (datetime | None), stop, in_use.
    """
    if winreg is None:
        raise RuntimeError("Мониторинг доступа к камере реализован для Windows (winreg)")
    out = []
    roots = [(h, "") for h in HIVES] + [(h, r"\NonPackaged") for h in HIVES]
    for hive_name, suffix in roots:
        for _hive, sub_path, key_name in _iter_app_keys(hive_name, suffix):
            times = _read_times(hive_name, sub_path)
            if times is None:
                continue
            start, stop = times
            in_use = (stop == 0) and (start != 0)
            if active_only and not in_use:
                continue
            out.append(
                {
                    "name": _app_name(key_name),
                    "key": f"{hive_name}\\{sub_path}",
                    "start": _filetime_to_dt(start),
                    "stop": _filetime_to_dt(stop),
                    "in_use": in_use,
                }
            )
    return out


def global_consent():
    """Глобальный доступ к камере из корней ключа: {'HKCU': 'Allow', 'HKLM': ...}."""
    if winreg is None:
        return {}
    result = {}
    for hive_name in HIVES:
        try:
            key = winreg.OpenKey(_hive_handle(hive_name), CONSENT)
        except OSError:
            continue
        try:
            try:
                value, _ = winreg.QueryValueEx(key, "Value")
                result[hive_name] = value
            except OSError:
                pass
        finally:
            key.Close()
    return result


# ------------------------------------------------------- события/цикл --

def _fmt(app: dict) -> str:
    started = app["start"].strftime("%H:%M:%S") if app.get("start") else "?"
    return f"{app['name']} (с {started})"


def watch(interval: float = 3.0, scanner=scan_consumers, printer=print,
          max_ticks: int | None = None, skip: set[str] | None = None):
    """Цикл наблюдения: печатает события захвата/освобождения камеры."""
    skip = {s.lower() for s in (skip or set())}
    known: set[str] = set()
    tick = 0
    while max_ticks is None or tick < max_ticks:
        tick += 1
        try:
            users = scanner(active_only=True)
        except Exception as e:
            printer(f"[!] Ошибка чтения журнала доступа: {e}")
            users = []
        now_map = {
            u["name"]: u
            for u in users
            if u["name"].lower() not in skip
        }
        for name in now_map.keys() - known:
            printer(f"[+] {_fmt(now_map[name])} — камера захвачена")
        for name in known - now_map.keys():
            printer(f"[-] {name} — камера освобождена")
        known = set(now_map)
        time.sleep(interval)


def demo(ticks: int = 8, interval: float = 0.7):
    """Демо логики без Windows: синтетический журнал — видно, как печатаются события."""
    t0 = time.time()

    def app(name: str, ago: float):
        return {
            "name": name,
            "key": f"demo\\{name}",
            "start": dt.datetime.now() - dt.timedelta(seconds=ago),
            "stop": None,
            "in_use": True,
        }

    timeline = [
        [],
        [],
        [app("obs64.exe", 1)],
        [app("obs64.exe", 3), app("Zoom.exe", 1)],
        [app("Zoom.exe", 3)],
        [app("WindowsCamera.exe", 1)],
        [],
        [],
    ]

    def fake_scanner(active_only=True):
        idx = min(int((time.time() - t0) // interval), len(timeline) - 1)
        return timeline[idx]

    print("=== ДЕМО (синтетические данные, Windows не нужен) ===")
    watch(interval=interval, scanner=fake_scanner, max_ticks=ticks)
    print("=== ДЕМО завершено ===")


# --------------------------------------------------------------- CLI --

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Кто использует камеру (журнал доступа Windows, ConsentStore)"
    )
    ap.add_argument("--interval", type=float, default=3.0,
                    help="период опроса, сек (по умолчанию 3)")
    ap.add_argument("--once", action="store_true",
                    help="разовый отчёт: кто держит камеру сейчас")
    ap.add_argument("--all", action="store_true",
                    help="показать все записи журнала, включая неактивные")
    ap.add_argument("--skip", action="append", default=[],
                    metavar="ИМЯ", help="не учитывать это приложение (можно несколько)")
    ap.add_argument("--demo", action="store_true",
                    help="демо логики без Windows (macOS/Linux)")
    args = ap.parse_args(argv)

    if args.demo:
        demo()
        return 0

    if winreg is None or sys.platform != "win32":
        print("Мониторинг доступа к камере работает только на Windows (реестр ConsentStore).")
        print("Посмотреть логику без Windows: python camera_watcher.py --demo")
        return 2

    if args.once or args.all:
        try:
            apps = scan_consumers(active_only=not args.all)
        except Exception as e:
            print(f"[!] {e}")
            return 1
        consent = global_consent()
        if consent:
            print(f"Глобальный доступ к камере: {consent}")
        if not apps:
            print("Камеру прямо сейчас никто не держит (или журнал пуст).")
        for a in apps:
            state = "ЗАХВАЧЕНА" if a["in_use"] else "не активна"
            last = a["stop"].strftime("%Y-%m-%d %H:%M:%S") if a["stop"] else "—"
            print(f"  {a['name']:<40} {state:<10} последний сеанс до: {last}")
        return 0

    print(f"Слежение за камерой (опрос каждые {args.interval:g} с, Ctrl+C — выход)...")
    try:
        watch(interval=args.interval, skip=set(args.skip))
    except KeyboardInterrupt:
        print("\nОстановлено.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
