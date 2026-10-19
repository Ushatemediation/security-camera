"""Тесты путей: работа в исходниках и в собранном (frozen) виде."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths  # noqa: E402


def _with(app: Path, bundle: Path):
    paths.APP_DIR = app
    paths.BUNDLE_DIR = bundle


def test_find_resource_prefers_app_dir(tmp_path):
    app = tmp_path / "app"
    bundle = tmp_path / "bundle"
    app.mkdir()
    bundle.mkdir()
    (bundle / "yolov8n.pt").write_text("bundle")
    (app / "yolov8n.pt").write_text("app")
    _with(app, bundle)
    # рядом с exe приоритетнее, чем внутри бандла
    assert paths.find_resource("yolov8n.pt") == app / "yolov8n.pt"
    (app / "yolov8n.pt").unlink()
    assert paths.find_resource("yolov8n.pt") == bundle / "yolov8n.pt"
    assert paths.find_resource("нет-такого") is None
    print("find_resource: приоритет APP_DIR -> BUNDLE_DIR = OK")


def test_resolve_ffmpeg(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    (app / exe).write_text("x")
    _with(app, app)
    # из конфига указан просто "ffmpeg" -> находим рядом с exe
    assert paths.resolve_ffmpeg("ffmpeg").endswith(exe)
    # явный путь не трогаем
    assert paths.resolve_ffmpeg("/custom/path/ffmpeg") == "/custom/path/ffmpeg"
    assert paths.resolve_ffmpeg("C:/tools/ffmpeg.exe") == "C:/tools/ffmpeg.exe"
    print("resolve_ffmpeg: бандл/явный путь = OK")


def test_frozen_detection(tmp_path):
    """Симуляция PyInstaller: sys.frozen + sys._MEIPASS."""
    import importlib

    meipass = tmp_path / "meipass"
    (meipass).mkdir()
    old = (getattr(sys, "frozen", None), getattr(sys, "_MEIPASS", None))
    try:
        sys.frozen = True
        sys._MEIPASS = str(meipass)
        importlib.reload(paths)
        assert paths.FROZEN is True
        assert paths.BUNDLE_DIR.resolve() == meipass.resolve()
        assert paths.APP_DIR == Path(sys.executable).resolve().parent
    finally:
        if old[0] is None:
            del sys.frozen
        else:
            sys.frozen = old[0]
        if old[1] is None:
            del sys._MEIPASS
        else:
            sys._MEIPASS = old[1]
        importlib.reload(paths)
    print("frozen-детект (sys._MEIPASS) = OK")


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        test_find_resource_prefers_app_dir(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_resolve_ffmpeg(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_frozen_detection(Path(d))
    print("\nВСЕ ТЕСТЫ ПУТЕЙ ПРОЙДЕНЫ")
