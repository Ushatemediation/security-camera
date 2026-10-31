"""Пути приложения — работают и в исходниках, и в собранном PyInstaller-бандле.

Разница:

* **APP_DIR** — «рабочий» каталог рядом с исполняемым файлом (или папка исходников).
  Сюда пишем изменяемые данные: ``config.yaml``, ``known/``, ``recordings/``,
  ``logs/``, ``_audio/``, ``_tmp/``. В собранном виде это папка с ``HomeCamera.exe``.
* **BUNDLE_DIR** — ресурсы, упакованные внутрь сборки (``sys._MEIPASS``): веса
  ``yolov8n.pt``, ``ffmpeg.exe``, ``config.example.yaml``. Только для чтения.

``find_resource`` ищет сначала рядом с exe (чтобы пользователь мог подменить
файл), затем внутри бандла.

ffmpeg **не нужно ставить вручную**: пакет ``imageio-ffmpeg`` везёт бинарь внутри
себя (и он попадает в PyInstaller-сборку), поэтому достаточно ``pip install -r
requirements.txt``. Свой ffmpeg рядом с exe по-прежнему имеет приоритет.
"""

import os
import shutil
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    APP_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
else:
    APP_DIR = Path(__file__).resolve().parent
    BUNDLE_DIR = APP_DIR


def find_resource(*names: str) -> Path | None:
    """Найти ресурс: сначала рядом с exe (APP_DIR), затем внутри бандла (BUNDLE_DIR)."""
    for base in (APP_DIR, BUNDLE_DIR):
        p = base.joinpath(*names)
        if p.exists():
            return p
    return None


def _imageio_ffmpeg() -> str | None:
    """ffmpeg из пакета ``imageio-ffmpeg`` (бинарь внутри колеса, ставить нечего)."""
    try:
        import imageio_ffmpeg  # ленивый импорт: нужен только при отсутствии своего бинаря

        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and Path(path).exists():
            return path
    except Exception:
        pass
    return None


def resolve_ffmpeg(value: str | None) -> str:
    """Путь к ffmpeg — всегда рабочий, без ручной установки.

    Приоритет: явный путь из конфига → ffmpeg(.exe) рядом с exe/в бандле →
    ffmpeg в PATH → бинарь из пакета ``imageio-ffmpeg``.
    """
    if value and value not in ("ffmpeg", "ffmpeg.exe"):
        return value
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    found = find_resource(exe)
    if found is not None:
        return str(found)
    name = value or "ffmpeg"
    if shutil.which(name):
        return name
    bundled = _imageio_ffmpeg()
    if bundled:
        return bundled
    return name


def ffmpeg_available(value: str | None) -> bool:
    """Есть ли реально доступный ffmpeg (файл, PATH или вшитый в пакет)."""
    resolved = resolve_ffmpeg(value)
    return bool(shutil.which(resolved) or Path(resolved).exists())
