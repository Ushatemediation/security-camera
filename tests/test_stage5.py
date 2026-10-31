"""Тесты Этапа 5: стейт-машина (контракт SendJob), LED Logitech (мок), состояния.

Не требуют камеры, сети и Telegram — всё подменено.

Запуск:
    PYTHONPATH=. ../venv/bin/python tests/test_stage5.py
"""

import logging
import queue
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import led_control as L  # noqa: E402
from main import Pipeline, State  # noqa: E402
from notifier import TelegramNotifier  # noqa: E402

log = logging.getLogger("test_stage5")


# ------------------------------------------------------------ State --

def test_state_enum():
    names = [s.value for s in State]
    assert names == ["IDLE", "RECORDING", "PROCESSING", "SENDING_TELEGRAM"], names
    print("State: состояния на месте = OK")


# ----------------------------------------------------- контракт SendJob --

def _notifier(events):
    """Нотификатор с подменёнными сетью и ffmpeg; фиксирует порядок вызовов."""
    stop = threading.Event()
    n = TelegramNotifier({"token": "x", "chat_ids": ["111"]}, log, Path.cwd(), stop)

    def fake_build(path, window):
        events.append(("build", path.name))
        time.sleep(0.05)
        return path

    def fake_send(chat_id, path, caption, **kwargs):
        events.append(("send_video", chat_id))
        return True

    def fake_photo(caption, jpeg):
        events.append(("send_photo", caption))
        return True

    n._build_send_file = fake_build
    n._send_video_with_retry = fake_send
    n._broadcast_photo = fake_photo
    return n


def _tmp_video() -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "clip.mp4"
    p.write_bytes(b"\x00" * 1024)
    return p


def test_sendjob_contract():
    """PROCESSING -> (ждёт разрешения) -> SENDING: снимок, затем видео."""
    video = _tmp_video()
    events = []
    n = _notifier(events)

    job = n.enqueue_video_job(
        video, "Тест", snapshot_jpeg=b"\xff\xd8\xff", snapshot_caption="Снимок"
    )
    assert job is not None and job.processing_done.wait(5), "processing_done не наступил"
    # Отправка НЕ начинается без явного разрешения главного цикла.
    assert not job.go_send.is_set()
    time.sleep(0.3)
    assert not any(e[0].startswith("send") for e in events), events

    job.go_send.set()
    assert job.sending_done.wait(5), "sending_done не наступил"
    assert [e[0] for e in events] == ["build", "send_photo", "send_video"], events
    print("SendJob: склейка -> разрешение -> снимок -> видео = OK")


def test_sendjob_archive_without_telegram():
    """Telegram выключен: файл уходит в on_ready (архив), отправок нет."""
    video = _tmp_video()
    stop = threading.Event()
    n = TelegramNotifier({"token": "", "chat_ids": []}, log, Path.cwd(), stop)
    got = []
    n.on_ready = got.append

    job = n.enqueue_video_job(video, "Без телеги")
    assert job is not None and job.processing_done.wait(5)
    job.go_send.set()
    assert job.sending_done.wait(5)
    assert got, "on_ready не получил файл"
    print("SendJob: без Telegram файл ушёл в архив = OK")


# --------------------------------------------------------------- LED --

class FakeCam:
    """Псевдо-UVC: хранит состояние; «не понимает» 0x03 как режим авто."""

    def __init__(self, desc=b""):
        self.state = bytearray(b"\x03\x00\x01\x00\x01")
        self.sent = []
        self.desc = desc

    def ctrl_transfer(self, rt, req, wv, wi, data, timeout=None):
        if rt == 0x80 and req == 0x06:
            return self.desc
        if rt == 0x21 and req == 0x01:
            b = bytearray(data)
            if b[3] != 0x03:
                self.state = b
            self.sent.append(bytes(b))
            return len(b)
        if rt == 0xA1 and req == 0x81:
            return bytes(self.state)
        raise RuntimeError("unexpected transfer")


# VideoControl-интерфейс + XU unit 11 с GUID ffe52d21 (как у C920/C922).
XU_DESCRIPTOR = (
    bytes([9, 0x02, 0x2D, 0x00, 1, 1, 0, 0x80, 0x32])
    + bytes([9, 0x04, 0x00, 0x00, 0x00, 0x0E, 0x01, 0x00, 0x00])
    + bytes([27, 0x24, 0x06, 0x0B])
    + L.LED_XU_FFE52D21.bytes_le
    + bytes([2, 1, 1, 2, 0x00, 0x41, 0x00])
)


def test_led_descriptor_and_modes():
    cam = FakeCam(XU_DESCRIPTOR)
    led = L.LogitechLEDController(log, {"enabled": True})
    led.dev, led._connected = cam, True

    unit, guid = led._find_xu_unit()
    assert (unit, guid) == (0x0B, L.LED_XU_FFE52D21), (unit, guid)

    led.selector, led.payload_len = 0x09, 5
    assert led.set_led_mode(1) and cam.sent[0] == bytes([3, 0, 1, 1, 1]), cam.sent
    # «Авто»: 0x03 не применился -> fallback на 0x02 (проверено чтением GET_CUR).
    assert led.set_led_mode(2) and cam.state[3] == 0x02
    assert cam.sent[-2][3] == 0x03 and cam.sent[-1][3] == 0x02
    assert led.set_led(0) and cam.state[3] == 0x00
    print("LED: XU по GUID, payload 03 00 01 MODE 01, fallback авто = OK")


def test_led_graceful_without_usb():
    """Без камеры/доступа модуль не бросает исключений (фича не критична)."""
    led = L.LogitechLEDController(log, {"enabled": True, "device": 0x085C})
    led.apply_startup_mode()      # True/False — главное, без исключения
    led.shutdown()
    print("LED: отсутствие камеры обрабатывается мягко = OK")


def test_retry_survives_shutdown():
    """Ретраи видео не обрываются по stop_event: иначе клип теряется при выходе."""
    stop = threading.Event()
    n = TelegramNotifier(
        {"token": "x", "chat_ids": ["1"], "retry_base_seconds": 0.01,
         "retry_max_attempts": 3},
        log, Path.cwd(), stop,
    )
    calls = {"n": 0}

    def always_fail(chat_id, path, caption):
        calls["n"] += 1
        return False

    n._send_video = always_fail
    stop.set()

    n._send_video_with_retry("1", Path("x.mp4"), "t", abort_on_stop=False)
    assert calls["n"] == 3, calls["n"]        # все попытки выполнены
    calls["n"] = 0
    n._send_video_with_retry("1", Path("x.mp4"), "t", abort_on_stop=True)
    assert calls["n"] == 1, calls["n"]        # старое поведение: обрыв по stop
    print("Ретраи: видео доотправляется даже при shutdown = OK")


def test_pipeline_state_machine():
    """Pipeline: finalized_q -> PROCESSING -> (склейка) -> SENDING -> IDLE."""
    events = []
    n = _notifier(events)
    p = Pipeline(
        {"recording": {}, "telegram": {"cooldown_seconds": 0}},
        log, n, types.SimpleNamespace(enabled=False), queue.Queue(),
    )
    video = _tmp_video()
    p.finalized_q.put((video, None))

    p.tick()                                  # IDLE -> PROCESSING: создан SendJob
    assert p.state is State.PROCESSING and p.current_job is not None, p.state
    job = p.current_job

    assert job.processing_done.wait(5), "processing_done"
    p.tick()                                  # PROCESSING -> SENDING + go_send
    assert p.state is State.SENDING_TELEGRAM and job.go_send.is_set(), p.state
    assert job.sending_done.wait(5), "sending_done"

    p.tick()                                  # SENDING -> IDLE
    assert p.state is State.IDLE and not p.pending_jobs, (p.state, p.pending_jobs)
    print("Pipeline: IDLE -> PROCESSING -> SENDING_TELEGRAM -> IDLE = OK")


def test_writer_cfr_padding():
    """Видео идёт в реальном времени: пропуски кадров закрываются дублями."""
    from main import WriterThread

    d = Path(tempfile.mkdtemp())
    cfg = {"recording": {"queue_size": 60, "max_recordings_gb": 10, "fps": 10}}
    stop = threading.Event()
    w = WriterThread(d, cfg, log, stop)
    got: dict = {}
    w.on_finalize = lambda p, win: got.update(path=p, window=win)
    w.start()

    frame = np.zeros((64, 64, 3), np.uint8)
    w.start_recording(frame)
    t0 = 5000.0
    for dt in (0.0, 0.35, 0.70, 1.05):     # «рваная» подача: ~2–3 кадра в секунду
        w.put(t0 + dt, frame.copy())
        time.sleep(0.05)
    w.finish()
    assert w.wait_finalized(10), "финализация не завершилась"
    for _ in range(50):
        if got:
            break
        time.sleep(0.02)
    assert got and got["path"].exists(), "файл не создан"
    dur = got["window"][1] - got["window"][0]
    # Допуск — один-два слота (0.1с): дальше проверяем, что таймлайн держится
    # по wall-clock, а не «сжимается» до числа присланных кадров.
    assert abs(dur - 1.05) <= 0.25, f"длительность {dur:.2f}с вместо ~1.05с"
    stop.set()
    w.join(5)
    print(f"WriterThread: CFR-паддинг ({dur:.2f}с ≈ реальные 1.05с, дублей {w.padded}) = OK")


def test_ffmpeg_selfcontained():
    """ffmpeg доступен без установки в систему: бинарь вшит через imageio-ffmpeg."""
    import os as _os

    import paths

    old = _os.environ.get("PATH", "")
    try:
        _os.environ["PATH"] = ""          # имитируем чистую систему без ffmpeg
        resolved = paths.resolve_ffmpeg("ffmpeg")
        assert Path(resolved).exists(), f"ffmpeg не найден: {resolved}"
        assert paths.ffmpeg_available("ffmpeg")
    finally:
        _os.environ["PATH"] = old
    print("ffmpeg: вшит в приложение (imageio-ffmpeg) = OK")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    test_state_enum()
    test_pipeline_state_machine()
    test_writer_cfr_padding()
    test_ffmpeg_selfcontained()
    test_sendjob_contract()
    test_sendjob_archive_without_telegram()
    test_retry_survives_shutdown()
    test_led_descriptor_and_modes()
    test_led_graceful_without_usb()
    print("\nВСЕ ТЕСТЫ ЭТАПА 5 ПРОЙДЕНЫ")
