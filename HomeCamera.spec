# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec для HomeCamera (режим --onedir: не тормозит на распаковке весов).

Собирает один exe `HomeCamera` (без окна консоли) + папку с библиотеками.
Внутрь бандла кладём config.example.yaml и (если есть) веса/ffmpeg.

Сборка:  pyinstaller --noconfirm --clean HomeCamera.spec
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

# --- данные, упакованные в бандл (доступны через sys._MEIPASS) ---------------
datas = [("config.example.yaml", ".")]

# веса модели и ffmpeg — если лежат рядом, кладём внутрь сборки
for extra in ("yolov8n.pt", "ffmpeg.exe", "ffmpeg"):
    if Path(extra).exists():
        datas.append((extra, "."))

# --- тяжёлые пакеты: забираем всё, что PyInstaller обычно теряет -------------
# (usb=pyusb — для LED Logitech; libusb-1.0.dll в бандл не кладём: на Windows
#  LED-фича деградирует мягко, см. led_control.py)
hiddenimports = []
binaries = []
for pkg in ("insightface", "onnxruntime", "ultralytics", "sounddevice", "usb", "imageio_ffmpeg"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# бинарные данные PortAudio для sounddevice
try:
    d, b, h = collect_all("_sounddevice_data")
    datas += d
    binaries += b
    hiddenimports += h
except Exception:
    pass

hiddenimports += ["cv2", "yaml", "requests", "numpy", "camera_watcher", "version"]

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "IPython", "tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HomeCamera",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # без чёрного окна консоли
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="HomeCamera",
)
