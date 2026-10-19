"""Тесты этапа 2: RingBuffer (память/pre-roll) и Motion Gate (CPU).

Запуск:  PYTHONPATH=. python tests/test_stage2.py
"""

import signal
import sys
import threading
import time
import types
from collections import deque
from pathlib import Path

import numpy as np
import yaml

import main as M


def test_ringbuffer_bounded_and_preroll():
    rb = M.RingBuffer(seconds=5, fps=30)
    assert rb.capacity == 150
    assert isinstance(rb.frames, deque)
    assert rb.frames.maxlen == 150
    for i in range(500):
        rb.add(float(i), np.zeros((2, 2, 3), np.uint8))
    assert len(rb.frames) == 150               # память ограничена
    assert rb.frames[0][0] == 350.0            # держатся последние 150
    out = rb.release()
    assert len(out) == 150
    assert len(rb.frames) == 0                 # после release пусто
    assert rb.start_ts is None
    print("RingBuffer: bounded=OK, pre-roll последние 150 кадров=OK")


def _install_stubs():
    fa = types.ModuleType("insightface.app")

    class FakeFaceAnalysis:
        def __init__(self, *a, **k):
            pass

        def prepare(self, *a, **k):
            pass

        def get(self, img):
            return []

    fa.FaceAnalysis = FakeFaceAnalysis
    iface = types.ModuleType("insightface")
    iface.app = fa
    sys.modules["insightface"] = iface
    sys.modules["insightface.app"] = fa

    ul = types.ModuleType("ultralytics")

    class FakeYOLO:
        def __init__(self, *a, **k):
            pass

        def track(self, *a, **k):
            return [types.SimpleNamespace(boxes=None)]

    ul.YOLO = FakeYOLO
    sys.modules["ultralytics"] = ul


def _run_loop(dynamic: bool) -> dict:
    """Гоняет main() ~4с на статичной или движущейся сцене. Возвращает счётчики."""
    import cv2

    _install_stubs()

    base = np.full((480, 640, 3), 100, np.uint8)
    state = {"n": 0}

    class FakeCap:
        def isOpened(self):
            return True

        def read(self):
            time.sleep(1 / 30)
            frame = base.copy()
            if dynamic:  # белый прямоугольник, бегающий по кадру
                state["n"] += 1
                x = (state["n"] * 20) % (640 - 120)
                frame[200:280, x:x + 120] = 255
            return True, frame

        def get(self, prop):
            if prop == cv2.CAP_PROP_FRAME_WIDTH:
                return 640
            if prop == cv2.CAP_PROP_FRAME_HEIGHT:
                return 480
            return 0

        def release(self):
            pass

    M.open_camera = lambda cid: FakeCap()

    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    cfg["telegram"]["token"] = ""
    cfg["telegram"]["chat_ids"] = []
    cfg["audio"]["enabled"] = False
    cfg["motion"]["warmup_frames"] = 3
    cfg["motion"]["process_fps"] = 30
    cfg["recording"]["pre_seconds"] = 1
    M.load_config = lambda: cfg

    counters = {"face": 0, "track": 0, "prebuf": 0}

    def bump(key):
        def _inner(self, frame, *a, **k):
            counters[key] += 1
            return orig[key](self, frame, *a, **k)
        return _inner

    orig = {
        "face": M.RecognitionWorker.submit,
        "track": M.PersonTracker.submit,
    }
    M.RecognitionWorker.submit = bump("face")
    M.PersonTracker.submit = bump("track")

    orig_add = M.RingBuffer.add

    def rb_add(self, ts, frame):
        counters["prebuf"] += 1
        return orig_add(self, ts, frame)

    M.RingBuffer.add = rb_add

    def watchdog():
        time.sleep(4.0)
        import os
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        M.main()
    except SystemExit:
        pass
    return counters


def test_motion_gate_no_models_without_motion():
    c = _run_loop(dynamic=False)
    print(f"[нет движения] счётчики: {c}")
    assert c["face"] == 0, "InsightFace вызывалась без движения!"
    assert c["track"] == 0, "YOLO вызывался без движения!"
    assert c["prebuf"] > 30, "pre-buffer не наполнялся (pre-roll порвётся)!"
    print("Motion Gate: без движения модели не зовутся, pre-buffer живой = OK")


def test_motion_gate_calls_models_on_motion():
    c = _run_loop(dynamic=True)
    print(f"[движение]    счётчики: {c}")
    assert c["face"] > 0, "InsightFace НЕ вызывалась при движении!"
    assert c["track"] > 0, "YOLO НЕ вызывался при движении!"
    print("Motion Gate: при движении модели запускаются = OK")


def test_audio_ring_extract():
    """Аудио-окно собирается из кольца в памяти (включая незакрытый хвост)."""
    import logging
    import tempfile
    import wave

    import audio as A

    log = logging.getLogger("t2")
    log.addHandler(logging.NullHandler())
    base = Path(tempfile.mkdtemp())
    rec = A.AudioRecorder({"enabled": False}, log, base, threading.Event())
    sr = rec.samplerate
    t0 = 1000.0
    for i in range(3):  # три блока по 0.5с
        block = (np.arange(int(sr * 0.5), dtype=np.int16) + i * 100).reshape(-1, 1)
        rec._push_ring(t0 + i * 0.5, block)
    out = rec.extract_window(t0 + 0.25, t0 + 1.25)  # ровно 1.0с
    with wave.open(str(out), "rb") as w:
        n, r = w.getnframes(), w.getframerate()
    assert abs(n / r - 1.0) < 0.02, (n, r)
    print("AudioRecorder: extract из кольца = OK (1.0с)")


def test_writer_window_and_callback():
    """WriterThread отдаёт точное окно записи через on_finalize и не рвёт новую запись."""
    import logging
    import tempfile

    log = logging.getLogger("tw")
    log.addHandler(logging.NullHandler())
    d = Path(tempfile.mkdtemp())
    cfg = {"recording": {"queue_size": 60, "max_recordings_gb": 10}}
    stop = threading.Event()
    w = M.WriterThread(d, cfg, log, stop)
    done = {}
    w.on_finalize = lambda p, win: done.update(path=p, window=win)
    w.start()

    frame = np.zeros((120, 160, 3), np.uint8)
    w.start_recording(frame)
    base = 5000.0
    for i in range(10):
        w.put(base + i * 0.04, frame)
    w.finish()
    for _ in range(100):
        if done:
            break
        time.sleep(0.05)
    assert done, "on_finalize не был вызван"
    assert done["window"] is not None and done["window"][1] > done["window"][0]
    assert Path(done["path"]).exists()

    # Новая запись стартует без гонки с финализацией старой.
    w.start_recording(frame)
    assert w.writer is not None, "новая запись потеряла writer"
    w.finish()
    stop.set()
    print("WriterThread: точное окно + on_finalize + старт новой записи = OK")


if __name__ == "__main__":
    test_ringbuffer_bounded_and_preroll()
    test_audio_ring_extract()
    test_writer_window_and_callback()
    test_motion_gate_no_models_without_motion()
    test_motion_gate_calls_models_on_motion()
    print("\nВСЕ ТЕСТЫ ПРОЙДЕНЫ")
