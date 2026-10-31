"""Охранная камера: motion gate -> распознавание лица -> трекинг -> запись + Telegram.

Пайплайн оформлен явной стейт-машиной (класс Pipeline ниже):

    IDLE -> RECORDING -> PROCESSING (склейка ffmpeg) -> SENDING_TELEGRAM -> IDLE

Переходы и отправка живут внутри самого приложения (методы Pipeline) —
никаких внешних вызовов. LED-подсветкой камеры Logitech управляет
led_control (UVC XU).
"""

import argparse
import atexit
import logging
import os
import queue
import shutil
import signal
import sys
import threading
import time
import traceback
from collections import deque
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path

import cv2
import numpy as np
import yaml

from audio import AudioRecorder
from keepawake import KeepAwake
from camwatch import CameraWatcher
from led_control import LogitechLEDController
from notifier import TelegramNotifier
from paths import APP_DIR, find_resource, resolve_ffmpeg
from uploader import ArchiveUploader
from version import __version__

# torch >= 2.6 грузит .pt в weights_only=True и ломает загрузку весов ultralytics.
# Этот флаг возвращает старое поведение для доверенных локальных чекпоинтов.
os.environ.setdefault("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", "1")

BASE_DIR = APP_DIR
CONFIG_PATH = BASE_DIR / "config.yaml"
PID_FILE = BASE_DIR / ".main.lock"


def setup_logger(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("cam")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = RotatingFileHandler(log_dir / "app.log", maxBytes=2_000_000, backupCount=3)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not cfg["telegram"]["token"]:
        logging.getLogger("cam").warning("Токен бота не задан — Telegram отключён")
    return cfg


def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def open_camera(camera_id):
    """camera_id: индекс камеры (int) или путь к видеофайлу (str) для тестов."""
    if isinstance(camera_id, str) and not camera_id.isdigit():
        cap = cv2.VideoCapture(camera_id)          # файл-источник (тест без камеры)
    elif sys.platform == "win32":
        cap = cv2.VideoCapture(int(camera_id), cv2.CAP_DSHOW)
        if cap.isOpened():
            # MJPG вместо YUY2: в ~10 раз меньше данных по USB -> кадры не теряются,
            # картинка не «дёргается». Некоторые камеры молча игнорируют — не страшно.
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FPS, 30)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    else:
        cap = cv2.VideoCapture(int(camera_id))
    if not cap.isOpened():
        raise RuntimeError(
            f"Источник {camera_id} не открылся. На macOS разрешите доступ к камере "
            "терминалу/IDE: Системные настройки → Конфиденциальность и безопасность → Камера."
        )
    return cap


def check_pid_file(logger: logging.Logger) -> bool:
    """Проверяет PID-файл. Возвращает True если можно запускаться."""
    if not PID_FILE.exists():
        return True
    try:
        old_pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        PID_FILE.unlink(missing_ok=True)
        return True
    if old_pid == os.getpid():
        return True
    try:
        os.kill(old_pid, 0)
        logger.error(f"Другой экземпляр уже запущен (PID {old_pid}). Выход.")
        return False
    except Exception:
        # На Windows os.kill для мёртвого PID может бросить SystemError/WinError 6,
        # а не OSError — считаем это «процесса нет» и снимаем блокировку.
        PID_FILE.unlink(missing_ok=True)
        return True


def write_pid_file():
    PID_FILE.write_text(str(os.getpid()))


def remove_pid_file():
    PID_FILE.unlink(missing_ok=True)


class State(Enum):
    """Состояния пайплайна записи (стейт-машина основного цикла).

    Штатная цепочка переходов (всё внутри цикла main(), без внешних вызовов):

        IDLE ──цель в кадре──▶ RECORDING ──запись остановлена──▶ PROCESSING
         ▲                                                            │
         │                                   (ffmpeg: склейка аудио+видео)
         │                                                            ▼
         └───────── отправлено (снимок + mp4 со звуком) ──▶ SENDING_TELEGRAM

    Дополнительно разрешён вход в RECORDING из PROCESSING/SENDING_TELEGRAM:
    новая цель в кадре не ждёт, пока доедет предыдущая отправка.
    """

    IDLE = "IDLE"
    RECORDING = "RECORDING"
    PROCESSING = "PROCESSING"
    SENDING_TELEGRAM = "SENDING_TELEGRAM"


class Pipeline:
    """Стейт-машина пайплайна записи — внутренняя подсистема main.py.

    Владеет состоянием (IDLE → RECORDING → PROCESSING → SENDING_TELEGRAM),
    очередью финализированных записей и заданиями отправки. Главный цикл лишь
    сообщает события («запись началась/остановилась», «прошёл кадр»), а склейка
    (ffmpeg) и отправка видео в Telegram запускаются отсюда — сменой состояния,
    без каких-либо внешних вызовов извне программы.
    """

    def __init__(self, cfg: dict, logger, notifier, audio, finalized_q):
        self.cfg = cfg
        self.logger = logger
        self.notifier = notifier
        self.audio = audio
        self.finalized_q = finalized_q

        self.state = State.IDLE
        self.current_job = None        # SendJob текущей записи
        self.pending_jobs: list = []   # все незавершённые отправки
        self.pending_snapshot = None   # (jpeg, caption) — снимок для SENDING-фазы
        self.last_notify = 0.0         # cooldown снимков

    # ---------------------------------------------------------- свойства --

    @property
    def writing(self) -> bool:
        """Идёт ли сейчас запись (используется главным циклом)."""
        return self.state is State.RECORDING

    # ----------------------------------------------------------- события --

    def _set_state(self, new_state: State, reason: str = "") -> None:
        """Единственная точка смены состояния пайплайна (пишет лог перехода)."""
        if new_state is self.state:
            return
        note = f" ({reason})" if reason else ""
        self.logger.info(f"[STATE] {self.state.value} -> {new_state.value}{note}")
        self.state = new_state

    def on_recording_started(self, frame, ts: float) -> None:
        """Цель в кадре: IDLE → RECORDING + снимок для фазы SENDING_TELEGRAM."""
        self._set_state(State.RECORDING, "цель в кадре")
        cooldown = float(self.cfg["telegram"]["cooldown_seconds"])
        if (self.pending_snapshot is not None or not self.notifier.enabled
                or (ts - self.last_notify) <= cooldown):
            return
        self.last_notify = ts
        try:
            ok_snap, buf = cv2.imencode(
                ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85]
            )
            if ok_snap:
                self.pending_snapshot = (
                    buf.tobytes(),
                    f"Цель в кадре: {time.strftime('%H:%M:%S')}",
                )
        except Exception as e:
            self.logger.warning(f"Не удалось закодировать снимок: {e}")

    def on_recording_finished(self, reason: str) -> None:
        """Запись остановлена: → PROCESSING (ждём on_finalize, затем склейка)."""
        self._set_state(State.PROCESSING, reason)

    # ------------------------------------------------- обслуживание (tick) --

    def tick(self) -> None:
        """Каждый кадр: разрешаем отправку после склейки, чистим готовые задания."""
        for job in list(self.pending_jobs):
            if job.processing_done.is_set() and not job.go_send.is_set():
                if job is self.current_job and self.state is State.PROCESSING:
                    self._set_state(State.SENDING_TELEGRAM, "склейка ffmpeg завершена")
                job.go_send.set()          # «добро» на отправку внутри нотификатора
            if job.sending_done.is_set():
                self.pending_jobs.remove(job)
                self.logger.info(f"[PIPELINE] отправка завершена: {job.path.name}")
                if job is self.current_job:
                    self.current_job = None
                    if self.state is State.SENDING_TELEGRAM:
                        self._set_state(State.IDLE, "отправка завершена")
        self._drain_finalized()

    def _drain_finalized(self) -> None:
        """finalized_q → SendJob: упаковать ВСЕ готовые записи в задания отправки.

        Без привязки к состоянию: иначе запись, финализированная во время чужой
        отправки, «повисла» бы в очереди навсегда.
        """
        while True:
            try:
                rec_path, rec_window = self.finalized_q.get_nowait()
            except queue.Empty:
                return
            aw = rec_window if (self.audio.enabled and rec_window) else None
            want_local = bool(self.cfg["recording"].get("mux_audio", True)) and aw is not None
            snap_jpeg, snap_caption = self.pending_snapshot or (None, "")
            self.pending_snapshot = None
            job = self.notifier.enqueue_video_job(
                rec_path,
                f"Запись завершена: {rec_path.name}",
                audio_window=aw,
                replace_source=want_local,
                snapshot_jpeg=snap_jpeg,
                snapshot_caption=snap_caption,
            )
            self.pending_jobs.append(job)
            if self.current_job is None:
                self.current_job = job
                if self.state is State.IDLE:
                    self._set_state(State.PROCESSING, "финализированная запись в обработке")
            self.logger.info(
                f"[PIPELINE] {rec_path.name}: обработка началась "
                f"(аудио: {'есть' if aw else 'нет'})"
            )

    # ----------------------------------------------------------- остановка --

    def shutdown(self, writer_thread, timeout: float) -> None:
        """Выход: дождаться финализации, упаковать остаток, разрешить отправку.

        notifier.shutdown() в main() затем доработает очередь (склейка + отправка
        + архив) — последний клип не теряется.
        """
        if self.state is State.RECORDING:
            writer_thread.finish()
            self._set_state(State.PROCESSING, "завершение программы")
        if self.state is State.PROCESSING:
            if not writer_thread.wait_finalized(timeout=timeout):
                self.logger.warning(
                    "Финализация записи не завершилась вовремя — возможна потеря хвоста"
                )
        self._drain_finalized()
        for job in self.pending_jobs:
            job.go_send.set()


class CaptureThread(threading.Thread):
    """Читает камеру на полной скорости, последний кадр под lock."""

    def __init__(self, camera_id: int, logger: logging.Logger, cfg: dict, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.logger = logger
        self.camera_id = camera_id
        self.cfg = cfg
        self.stop_event = stop_event
        self.cap = open_camera(camera_id)
        self.is_file = isinstance(camera_id, str) and not str(camera_id).isdigit()
        self.file_fps = 0.0
        if self.is_file:
            fps = self.cap.get(cv2.CAP_PROP_FPS)
            self.file_fps = fps if fps and fps > 1 else 30.0
        self.lock = threading.Lock()
        self.frame: np.ndarray | None = None
        self.ts: float = 0.0
        self.frame_seq: int = 0
        self.new_frame = threading.Event()
        self._stop = threading.Event()
        self.fps_window: deque[float] = deque(maxlen=60)
        self.fail_count = 0
        self.start_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.start_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.logger.info(
            f"Камера открыта: {self.start_w}x{self.start_h}, "
            f"заявленный fps={self.cap.get(cv2.CAP_PROP_FPS):.0f}, "
            f"fourcc={self._fourcc_name()}"
        )

    def _fourcc_name(self) -> str:
        """Четырёхбуквенный кодек камеры (MJPG/YUY2/…)."""
        try:
            v = int(self.cap.get(cv2.CAP_PROP_FOURCC))
        except Exception:
            return "?"
        name = "".join(chr((v >> (8 * i)) & 0xFF) for i in range(4))
        return name if name.strip("\x00 ") else "?"

    @property
    def current_fps(self) -> float:
        """Реальный fps по последним кадрам (не то, что заявляет камера)."""
        if len(self.fps_window) >= 2:
            dt = self.fps_window[-1] - self.fps_window[0]
            if dt > 0:
                return (len(self.fps_window) - 1) / dt
        return 0.0

    def run(self):
        try:
            threshold = self.cfg["resilience"]["camera_fail_threshold"]
            reconnect_sec = self.cfg["resilience"]["camera_reconnect_seconds"]
            while not self.stop_event.is_set():
                ret, frame = self.cap.read()
                if not ret:
                    if self.is_file:
                        # Тестовый видеофайл закончился — зацикливаем.
                        self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    self.fail_count += 1
                    if self.fail_count >= threshold:
                        self.logger.error(f"Камера отвалилась ({self.fail_count} неудачных чтений)")
                        self._reconnect(reconnect_sec)
                    else:
                        time.sleep(0.1)
                    continue
                self.fail_count = 0
                ts = time.time()
                with self.lock:
                    self.frame = frame
                    self.ts = ts
                self.frame_seq += 1
                self.new_frame.set()
                self.fps_window.append(ts)
                if self.is_file and self.file_fps > 0:
                    time.sleep(1.0 / self.file_fps)   # пейсинг файла как у камеры
        except Exception:
            self.logger.error(f"CaptureThread упал:\n{traceback.format_exc()}")
            os._exit(1)

    def _reconnect(self, reconnect_sec: float):
        self.cap.release()
        while not self.stop_event.is_set():
            time.sleep(reconnect_sec)
            try:
                self.cap = open_camera(self.camera_id)
                self.start_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                self.start_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                self.logger.info("Камера переоткрыта успешно")
                return
            except RuntimeError as e:
                self.logger.error(f"Reconnect не удался: {e}")

    def get_frame(self) -> tuple[float, np.ndarray] | None:
        with self.lock:
            if self.frame is None:
                return None
            return self.ts, self.frame.copy()

    def stop(self):
        self.cap.release()


class RecognitionWorker(threading.Thread):
    """Распознавание лиц в отдельном потоке. Возвращает (found, face_bbox)."""

    def __init__(self, face_app, emb_data_ref, cfg: dict, logger: logging.Logger, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.face_app = face_app
        self.stop_event = stop_event
        self.emb_data_ref = emb_data_ref
        self.fc = cfg["face"]
        self.logger = logger
        self._pending: np.ndarray | None = None
        self._pending_lock = threading.Lock()
        self._result_lock = threading.Lock()
        self._result: tuple[bool, tuple | None, float] = (False, None, 0.0)
        self._result_ts: float = 0.0
        self._last_recog_ms: float = 0.0

    def submit(self, frame: np.ndarray):
        with self._pending_lock:
            if self._pending is not None:
                return False
            self._pending = frame.copy()
            return True

    def get_result(self) -> tuple[bool, tuple | None, float, float]:
        with self._result_lock:
            return self._result[0], self._result[1], self._result[2], self._result_ts

    def run(self):
        try:
            while not self.stop_event.is_set():
                with self._pending_lock:
                    frame = self._pending
                    self._pending = None
                if frame is None:
                    time.sleep(0.01)
                    continue
                t0 = time.time()
                found, bbox, sim = self._recognize(frame)
                elapsed_ms = (time.time() - t0) * 1000
                self._last_recog_ms = elapsed_ms
                with self._result_lock:
                    self._result = (found, bbox, sim)
                    self._result_ts = time.time()
        except Exception:
            self.logger.error(f"RecognitionWorker упал:\n{traceback.format_exc()}")
            os._exit(1)

    def _recognize(self, frame: np.ndarray) -> tuple[bool, tuple | None, float]:
        h, w = frame.shape[:2]
        small_w = 320
        scale = small_w / w
        small = cv2.resize(frame, (small_w, int(h * scale)))
        faces = self.face_app.get(small)
        for face in faces:
            x1, y1, x2, y2 = face.bbox
            fw = (x2 - x1) / scale
            if fw < self.fc["min_face_px"]:
                self.logger.info(f"Лицо слишком маленькое: {fw:.0f}px < {self.fc['min_face_px']} — пропуск")
                continue
            cx1, cy1, cx2, cy2 = [int(v / scale) for v in (x1, y1, x2, y2)]
            cx1, cy1 = max(0, cx1), max(0, cy1)
            crop = frame[cy1:cy2, cx1:cx2]
            if crop.size == 0:
                continue
            kf = self.face_app.get(crop)
            if not kf:
                continue
            emb = kf[0].normed_embedding
            best_name, best_sim = "?", -1.0
            for name, ref in self.emb_data_ref.items():
                sim = cosine_sim(emb, ref)
                if sim > best_sim:
                    best_name, best_sim = name, sim
            if best_sim >= self.fc["threshold"]:
                self.logger.info(
                    f"Лицо: {best_name}, скор={best_sim:.3f}, ширина={fw:.0f}px, СОВПАДЕНИЕ"
                )
                return True, (cx1, cy1, cx2, cy2), best_sim
            else:
                self.logger.info(f"Лицо: {best_name}, скор={best_sim:.3f}, ширина={fw:.0f}px, ниже порога")
        return False, None, 0.0

class PersonTracker(threading.Thread):
    """Трекинг людей (YOLOv8n + ByteTrack) в отдельном потоке."""

    def __init__(self, model_path: Path, cfg: dict, logger: logging.Logger, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.stop_event = stop_event
        from ultralytics import YOLO
        self.model = YOLO(str(model_path))
        self.person_fps = cfg["person_fps"]
        self.imgsz = cfg["imgsz"]
        self.conf = cfg["person_conf"]
        self.logger = logger
        self._pending: np.ndarray | None = None
        self._pending_lock = threading.Lock()
        self._result_lock = threading.Lock()
        self._tracks: dict[int, tuple] = {}
        self._last_track_ms: float = 0.0

    def submit(self, frame: np.ndarray):
        with self._pending_lock:
            if self._pending is not None:
                return False
            self._pending = frame.copy()
            return True

    def get_tracks(self) -> dict[int, tuple]:
        with self._result_lock:
            return dict(self._tracks)

    def run(self):
        try:
            interval = 1.0 / self.person_fps
            while not self.stop_event.is_set():
                with self._pending_lock:
                    frame = self._pending
                    self._pending = None
                if frame is None:
                    time.sleep(0.01)
                    continue
                t0 = time.time()
                results = self.model.track(
                    frame, classes=[0], persist=True, tracker="bytetrack.yaml",
                    imgsz=self.imgsz, conf=self.conf, verbose=False,
                )
                tracks = {}
                if results and results[0].boxes is not None:
                    boxes = results[0].boxes
                    for i in range(len(boxes)):
                        tid = int(boxes.id[i]) if boxes.id is not None else -1
                        x1, y1, x2, y2 = boxes.xyxy[i].tolist()
                        tracks[tid] = (x1, y1, x2, y2)
                with self._result_lock:
                    self._tracks = tracks
                self._last_track_ms = (time.time() - t0) * 1000
                dt = time.time() - t0
                if dt < interval:
                    time.sleep(interval - dt)
        except Exception:
            self.logger.error(f"PersonTracker упал:\n{traceback.format_exc()}")



class WriterThread(threading.Thread):
    """Асинхронная запись видео: кадры из очереди -> VideoWriter."""

    def __init__(self, out_dir: Path, cfg: dict, logger: logging.Logger, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.rec_cfg = cfg["recording"]
        self.logger = logger
        self.stop_event = stop_event
        self.q: queue.Queue = queue.Queue(maxsize=self.rec_cfg["queue_size"])
        self.writer: cv2.VideoWriter | None = None
        # Целевой FPS таймлайна: recording.fps (0 = авто, main выставит по камере).
        self.fps = float(self.rec_cfg.get("fps") or 30.0)
        self.path: Path | None = None
        self.timestamps: list[float] = []
        self.fps_window: deque[float] = deque(maxlen=60)
        self.write_times: deque[float] = deque(maxlen=120)
        self._lock = threading.Lock()
        self._recording = threading.Event()
        # Границы записи в wall-clock — нужны для синхронизации с аудио.
        self.first_ts: float | None = None
        self.last_ts: float | None = None
        self.window: tuple[float, float] | None = None
        # Колбэк (path, window) вызывается после финализации записи, когда очередь
        # уже слита -> границы точные. Так нотификатор не получает устаревший last_ts.
        self.on_finalize = None
        # Финализация предыдущей записи должна завершиться до старта новой,
        # иначе она закроет новый writer (гонка finish/start).
        self._finalize_done = threading.Event()
        self._finalize_done.set()

        # CFR-таймлайн: номера слотов, «последний кадр» для дублей, статистика.
        self._slots = 0
        self._last_frame: np.ndarray | None = None
        self._size: tuple[int, int] = (0, 0)      # (w, h) — чётные, под mp4v
        self.padded = 0                           # кадров добавлено дублями
        self.dropped = 0                          # кадров выкинуто из очереди
        self.MAX_PAD = 20                         # предел дублей подряд

    def start_recording(self, frame: np.ndarray):
        # Дождаться финализации предыдущей записи, чтобы она не закрыла наш writer.
        if not self._finalize_done.wait(timeout=10):
            self.logger.warning("Предыдущая запись не финализировалась за 10с")
        with self._lock:
            h, w = frame.shape[:2]
            # mp4v капризен к нечётным размерам — «битые» блоки у краёв кадра.
            w, h = w - (w % 2), h - (h % 2)
            self._size = (w, h)
            name = time.strftime("%Y-%m-%d_%H-%M-%S") + ".mp4"
            self.path = self.out_dir / name
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            self.writer = cv2.VideoWriter(str(self.path), fourcc, float(self.fps or 30.0), (w, h))
            if not self.writer.isOpened():
                raise RuntimeError("VideoWriter не открылся")
            self.timestamps = []
            self.first_ts = None
            self.last_ts = None
            self.window = None
            self._slots = 0
            self._last_frame = None
            self.padded = 0
            self.dropped = 0
            self._recording.set()

    def put(self, ts: float, frame: np.ndarray):
        try:
            self.q.put_nowait((ts, frame))
        except queue.Full:
            # Выкидываем самый старый кадр: таймлайн не рвётся — образовавшийся
            # пропуск писатель закроет дублем предыдущего кадра (см. run()).
            try:
                self.q.get_nowait()
            except queue.Empty:
                pass
            self.dropped += 1
            if self.dropped == 1:
                self.logger.warning(
                    "Очередь записи переполнена — пропуски закрываются дублями кадров"
                )
            try:
                self.q.put_nowait((ts, frame))
            except queue.Full:
                pass

    def finish(self):
        self._recording.clear()
        self._finalize_done.clear()
        self.q.put(None)

    def wait_finalized(self, timeout: float = 10.0) -> bool:
        """Дождаться завершения финализации (True — завершена или не начиналась)."""
        return self._finalize_done.wait(timeout)

    def run(self):
        try:
            while True:
                try:
                    item = self.q.get(timeout=0.1)
                except queue.Empty:
                    if self.stop_event.is_set():
                        break
                    continue
                if item is None:
                    self._finalize()
                    if self.stop_event.is_set() and self.q.empty():
                        break
                    continue
                ts, frame = item
                with self._lock:
                    if self.writer is None:
                        continue
                    w_t0 = time.time()
                    # CFR по слотам: видео обязано идти в реальном времени, даже если
                    # кадры доходят реже (детекция грузит главный цикл). Пропуски
                    # закрываем дублем последнего кадра — иначе таймлайн «сжимается»
                    # и звук (нарезанный по wall-clock) уезжает.
                    if self.first_ts is None:
                        self.first_ts = ts
                        self._slots = 0
                        target_slot = 0
                    else:
                        target_slot = int(round((ts - self.first_ts) * self.fps))
                    gap = target_slot - self._slots
                    if gap > 0 and self._last_frame is not None:
                        gap = min(gap, self.MAX_PAD)
                        for _ in range(gap):
                            self._write_frame(self._last_frame)
                        self.padded += gap
                        self._slots += gap
                    self._write_frame(frame)
                    self._last_frame = frame
                    self._slots += 1
                    w_ms = (time.time() - w_t0) * 1000
                    self.write_times.append(w_ms)
                    if w_ms > 33:
                        self.logger.warning(f"write() медленный: {w_ms:.1f} мс")
                    self.timestamps.append(ts)
                    self.fps_window.append(ts)
                    self.last_ts = ts
                # При остановке: доработать остаток очереди и финализировать запись.
                if self.stop_event.is_set() and self.q.empty():
                    self._finalize()
                    break
        except Exception:
            self.logger.error(f"WriterThread упал:\n{traceback.format_exc()}")

    def _write_frame(self, frame: np.ndarray):
        """Записать кадр ровно в размере writer'а (чётные w/h)."""
        if self.writer is None:
            return
        w, h = self._size
        if frame.shape[1] != w or frame.shape[0] != h:
            frame = frame[:h, :w]
        self.writer.write(frame)

    def _finalize(self):
        with self._lock:
            if self.writer is not None:
                self.writer.release()
                self.writer = None
            if self.first_ts is not None and self._slots > 0:
                # Длительность = слоты / fps: ровно столько же аудио в окне,
                # поэтому дорожки не могут разъехаться по длине.
                dur = self._slots / self.fps
                self.window = (self.first_ts, self.first_ts + dur)
                feed_fps = 0.0
                if len(self.timestamps) >= 2:
                    span = self.timestamps[-1] - self.timestamps[0]
                    if span > 0:
                        feed_fps = (len(self.timestamps) - 1) / span
                name = self.path.name if self.path else "?"
                self.logger.info(
                    f"Запись завершена: {name}: {self._slots} кадров, {dur:.1f}с @ "
                    f"{self.fps:.1f}fps (подача ≈{feed_fps:.1f}fps, "
                    f"дубли {self.padded}, дропы {self.dropped})"
                )
            path, window, cb = self.path, self.window, self.on_finalize
            self._enforce_disk_limit()
        # Разрешаем следующую запись только после завершения финализации этой.
        self._finalize_done.set()
        if cb is not None and path is not None:
            try:
                cb(path, window)
            except Exception:
                self.logger.error(f"on_finalize callback упал:\n{traceback.format_exc()}")

    def _enforce_disk_limit(self):
        max_gb = self.rec_cfg["max_recordings_gb"]
        files = sorted(self.out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        limit = max_gb * 1024**3
        while files and total > limit:
            victim = files.pop(0)
            total -= victim.stat().st_size
            victim.unlink(missing_ok=True)
            self.logger.info(f"Удалена старая запись (лимит диска): {victim.name}")

    def get_stats(self) -> tuple[float, float]:
        with self._lock:
            rec_fps = len(self.fps_window) / (self.fps_window[-1] - self.fps_window[0]) if len(self.fps_window) > 1 else 0
            avg_write = sum(self.write_times) / len(self.write_times) if self.write_times else 0
        return rec_fps, avg_write

class DiskCleaner(threading.Thread):
    """Фоновая очистка диска по лимиту max_recordings_gb."""

    def __init__(self, out_dir: Path, cfg: dict, logger: logging.Logger, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.stop_event = stop_event
        self.out_dir = out_dir
        self.max_gb = cfg["recording"]["max_recordings_gb"]
        self.interval = cfg["recording"]["disk_check_interval_seconds"]
        self.logger = logger

    def run(self):
        try:
            while not self.stop_event.is_set():
                self._check()
                self.stop_event.wait(self.interval)
        except Exception:
            self.logger.error(f"DiskCleaner упал:\n{traceback.format_exc()}")

    def _check(self):
        files = sorted(self.out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        limit = self.max_gb * 1024**3
        while files and total > limit:
            victim = files.pop(0)
            total -= victim.stat().st_size
            victim.unlink(missing_ok=True)
            self.logger.info(f"Удалена старая запись (лимит диска): {victim.name}")



class RingBuffer:
    """Кольцевой буфер кадров для pre-record и хвоста."""

    def __init__(self, seconds: float, fps: float):
        self.capacity = max(1, int(seconds * fps))
        # deque(maxlen) — O(1) на вставку и жёсткий предел памяти.
        # Было list + pop(0): O(n) на каждый кадр и без гарантии по памяти.
        self.frames: deque[tuple[float, np.ndarray]] = deque(maxlen=self.capacity)
        self.start_ts: float | None = None

    def add(self, ts: float, frame: np.ndarray):
        if self.start_ts is None:
            self.start_ts = ts
        self.frames.append((ts, frame))

    def release(self) -> list[tuple[float, np.ndarray]]:
        out = list(self.frames)
        self.frames.clear()
        self.start_ts = None
        return out


def find_person_for_face(tracks: dict[int, tuple], face_bbox: tuple) -> int | None:
    """Находит track_id человека, внутри которого лежит центр бокса лица."""
    fx = (face_bbox[0] + face_bbox[2]) / 2
    fy = (face_bbox[1] + face_bbox[3]) / 2
    for tid, (x1, y1, x2, y2) in tracks.items():
        if x1 <= fx <= x2 and y1 <= fy <= y2:
            return tid
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true", help="окно с рамкой и маской")
    ap.add_argument("--camera", type=int, default=None, help="id камеры (переопределяет конфиг)")
    ap.add_argument("--source", default=None, help="источник: индекс камеры или путь к видеофайлу (тест без камеры)")
    args = ap.parse_args()

    logger = setup_logger(BASE_DIR / "logs")

    if not check_pid_file(logger):
        sys.exit(1)
    write_pid_file()
    atexit.register(remove_pid_file)

    cfg = load_config()
    # ffmpeg: ищем рядом с exe/внутри бандла, иначе берём из PATH
    cfg["telegram"]["ffmpeg_path"] = resolve_ffmpeg(cfg["telegram"].get("ffmpeg_path", "ffmpeg"))

    # Не давать ПК уйти в сон, пока работает камера (system.prevent_sleep)
    keep_awake = KeepAwake(cfg.get("system", {}) or {}, logger)
    keep_awake.start()

    if args.camera is not None:
        cfg["camera"]["device_id"] = args.camera
    if args.source is not None:
        cfg["camera"]["device_id"] = int(args.source) if str(args.source).isdigit() else args.source

    emb_path = BASE_DIR / "known" / "embedding.npy"
    if not emb_path.exists():
        # Авто-регистрация: если в known/ лежат фото — строим эмбеддинги сами,
        # чтобы пользователю не нужно было запускать enroll вручную.
        known_dir = BASE_DIR / "known"
        photos = [
            p for p in known_dir.iterdir()
            if p.suffix.lower() in (".jpg", ".jpeg", ".png")
        ] if known_dir.exists() else []
        if photos:
            logger.info(f"Найдено фото лиц: {len(photos)} — строю эмбеддинги (один раз)...")
            try:
                import enroll
                enroll.main()
            except Exception:
                logger.error(f"Авто-регистрация не удалась:\n{traceback.format_exc()}")
        if not emb_path.exists():
            logger.error("Эмбеддинг не найден. Положите фото в known/ и запустите снова.")
            sys.exit(1)
    emb_data = np.load(emb_path, allow_pickle=True).item()
    logger.info(f"Загружено эмбеддингов: {len(emb_data)}")

    rec_dir = BASE_DIR / "recordings"
    try:
        rec_dir.mkdir(parents=True, exist_ok=True)
        test_file = rec_dir / ".write_test"
        test_file.touch()
        test_file.unlink()
    except OSError as e:
        logger.error(f"Папка recordings/ недоступна для записи: {e}")
        sys.exit(1)

    from insightface.app import FaceAnalysis

    if not (Path.home() / ".insightface" / "models" / "buffalo_l").exists():
        logger.info("Модель лиц buffalo_l не найдена — скачается автоматически (~300 МБ)")
    face_app = FaceAnalysis(
        name="buffalo_l",
        allowed_modules=["detection", "recognition"],
        providers=["CPUExecutionProvider"],
    )
    face_app.prepare(ctx_id=-1, det_size=tuple(cfg["face"]["det_size"]))

    stop_event = threading.Event()
    capture = CaptureThread(cfg["camera"]["device_id"], logger, cfg, stop_event)
    capture.start()
    logger.info(f"Камера открыта (backend={'DSHOW' if sys.platform == 'win32' else 'default'})")

    # LED-подсветка камеры Logitech: сразу после инициализации камеры сообщаем
    # ей, что она в работе (UVC XU, режим из config.yaml, по умолчанию 1 —
    # «включено»; подробности и отладка — в led_control.py).
    led = LogitechLEDController(logger, cfg.get("led", {}) or {})
    led.apply_startup_mode()

    worker = RecognitionWorker(face_app, emb_data, cfg, logger, stop_event)
    worker.start()

    pt_cfg = cfg["person_tracking"]
    tracker: PersonTracker | None = None
    if pt_cfg["enabled"]:
        model_path = find_resource(pt_cfg["model"]) or (BASE_DIR / pt_cfg["model"])
        if not model_path.exists():
            logger.info(f"{pt_cfg['model']} не найден рядом с проектом — ultralytics докачает автоматически")
        tracker = PersonTracker(model_path, pt_cfg, logger, stop_event)
        tracker.start()
        logger.info("PersonTracker запущен (YOLOv8n + ByteTrack)")

    audio = AudioRecorder(cfg.get("audio", {}) or {}, logger, BASE_DIR, stop_event)
    audio.start()
    if audio.enabled:
        logger.info(
            f"AudioRecorder запущен ({audio.samplerate} Гц, буфер {audio.buffer_seconds:.0f}с)"
        )
    else:
        logger.info("AudioRecorder выключен — видео уйдёт без звука")

    notifier = TelegramNotifier(cfg["telegram"], logger, BASE_DIR, stop_event, audio=audio)

    # Выгрузка готовых записей на сервер-архив (веб-галерея). Опция server.*
    archive = ArchiveUploader(cfg.get("server", {}) or {}, logger, stop_event)
    notifier.on_ready = archive.upload_video

    # Наблюдение: кто ещё на этом ПК держит камеру (журнал доступа Windows)
    CameraWatcher(logger, stop_event).start()

    mot = cfg["motion"]
    bg = cv2.createBackgroundSubtractorMOG2(
        history=mot["history"], varThreshold=mot["var_threshold"], detectShadows=True
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    writer_thread = WriterThread(rec_dir, cfg, logger, stop_event)
    writer_thread.start()

    # Финализированные записи кладём в очередь, а главный цикл сам решит, когда
    # перейти PROCESSING -> SENDING_TELEGRAM (никаких внешних вызовов отправки).
    finalized_q: queue.Queue = queue.Queue()

    def _on_recording_done(path, window):
        """Колбэк WriterThread: запись закрыта, точное окно аудио известно."""
        finalized_q.put((path, window))

    writer_thread.on_finalize = _on_recording_done
    disk_cleaner = DiskCleaner(rec_dir, cfg, logger, stop_event)
    disk_cleaner.start()

    pre_buf = RingBuffer(cfg["recording"]["pre_seconds"], 30.0)

    active = False
    writing = False
    rec_start_ts = 0.0
    tail_frames = 0
    consecutive = 0
    last_target_seen = 0.0
    last_known_emb_time = emb_path.stat().st_mtime
    last_recog_submit = 0.0
    warmup_left = mot["warmup_frames"]
    last_global_reset = 0.0
    last_light_log = 0.0
    last_stats_log = 0.0
    last_preview = 0.0
    last_motion_ts = 0.0
    manual_until = 0.0                 # ручной триггер: до какого времени пишем
    trigger_path = APP_DIR / "trigger.now"
    last_seq = 0
    motion_on = False
    changed = 0.0
    frame_interval = 1.0 / mot["process_fps"]
    small_w = mot["resize_width"]
    ratio_min_w = mot["motion_min_area"]
    global_ratio = mot["global_change_ratio"]
    reset_cooldown = mot["global_reset_cooldown"]
    last_disk_check = 0.0
    disk_ok = True

    # Стейт-машина пайплайна: всё про запись/склейку/отправку живёт в Pipeline
    # (класс в этом же файле). Главный цикл только сообщает события и «тикает».
    pipeline = Pipeline(cfg, logger, notifier, audio, finalized_q)

    target_track_id: int | None = None
    track_lost_since: float | None = None
    last_face_bbox: tuple | None = None
    grace_seconds = pt_cfg["track_lost_grace_seconds"] if pt_cfg["enabled"] else 0.0

    shutdown_event = threading.Event()

    def graceful_shutdown(signum, frame):
        logger.info(f"Сигнал {signum}, начинаю завершение...")
        stop_event.set()
        shutdown_event.set()

    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)

    logger.info(
        f"HomeCamera v{__version__} — старт цикла "
        f"(стейт-машина: {pipeline.state.value}). Ctrl+C для остановки."
    )
    try:
        while not stop_event.is_set():
            capture.new_frame.wait(0.1)
            if capture.frame_seq == last_seq:
                continue
            capture.new_frame.clear()
            got = capture.get_frame()
            if got is None:
                continue
            ts, frame = got
            last_seq = capture.frame_seq

            # ───────────── СТЕЙТ-МАШИНА: сопровождение конвейера ─────────────
            # Переходы и отправка — внутри Pipeline (class Pipeline выше);
            # цикл лишь отдаёт ей события и «тикает» каждый кадр.
            writing = pipeline.writing
            pipeline.tick()

            h, w = frame.shape[:2]
            if w != capture.start_w or h != capture.start_h:
                logger.warning(
                    f"Разрешение камеры изменилось: {capture.start_w}x{capture.start_h} -> {w}x{h}. "
                    "Текущая запись закроется как есть, новая — под новый размер."
                )
                if writing:
                    writer_thread.finish()
                    pipeline.on_recording_finished("смена разрешения камеры")
                    writing = False
                capture.start_w = w
                capture.start_h = h

            # Pre-buffer кормим ВСЕГДА (в т.ч. без движения) — иначе рвётся pre-roll.
            pre_buf.add(ts, frame)

            if emb_path.exists() and emb_path.stat().st_mtime > last_known_emb_time:
                emb_data = np.load(emb_path, allow_pickle=True).item()
                worker.emb_data_ref = emb_data
                last_known_emb_time = emb_path.stat().st_mtime
                logger.info(f"Эмбеддинги обновлены: {len(emb_data)}")

            # Motion gate: ресайз под MOG2 и сам MOG2 — только на process_fps,
            # а не на каждом кадре (раньше cv2.resize крутился вхолостую всегда).
            if warmup_left > 0:
                small = cv2.resize(frame, (small_w, int(h * (small_w / w))))
                bg.apply(small, learningRate=-1)
                warmup_left -= 1
            elif ts - last_motion_ts >= frame_interval:
                last_motion_ts = ts
                small = cv2.resize(frame, (small_w, int(h * (small_w / w))))
                fg = bg.apply(small, learningRate=0 if (writing or active) else -1)
                fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, kernel)
                fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, kernel)
                changed = float(np.count_nonzero(fg)) / fg.size

                if changed > global_ratio and (ts - last_global_reset > reset_cooldown):
                    if ts - last_light_log > reset_cooldown:
                        logger.info(f"Смена света ({changed:.2f} > {global_ratio}) — сброс фона")
                        last_light_log = ts
                    bg = cv2.createBackgroundSubtractorMOG2(
                        history=mot["history"], varThreshold=mot["var_threshold"], detectShadows=True
                    )
                    warmup_left = mot["warmup_frames"]
                    last_global_reset = ts
                    continue

                motion_on = changed > ratio_min_w

            # Тяжёлые модели (InsightFace / YOLO) — ТОЛЬКО при движении,
            # активном трекинге цели или записи. Нет движения -> кадр скипается,
            # модели не запускаются. Прежний «safety check» (каждые 2с без движения)
            # убран — он постоянно жёг CPU на распознавании.
            # Ручной триггер: появился файл trigger.now рядом с программой —
            # запускаем запись, как будто сработала детекция.
            if trigger_path.exists():
                try:
                    trigger_path.unlink()
                except OSError:
                    pass
                manual_until = ts + float(cfg["recording"]["post_seconds"]) + 1.0
                logger.info("Ручной триггер: запись по команде (как при сработке)")
            manual_now = ts < manual_until

            heavy_active = motion_on or target_track_id is not None or writing or manual_now
            if heavy_active:
                interval = cfg["face"]["recognition_interval_recording"] if writing else 0.0
                if ts - last_recog_submit >= interval:
                    if worker.submit(frame):
                        last_recog_submit = ts
                if tracker is not None:
                    tracker.submit(frame)

            found, face_bbox, last_sim, _ = worker.get_result()

            if tracker is not None:
                tracks = tracker.get_tracks()
            else:
                tracks = {}

            if found and face_bbox is not None:
                last_face_bbox = face_bbox
                if target_track_id is not None:
                    tid = find_person_for_face(tracks, face_bbox)
                    if tid is not None and tid != target_track_id:
                        logger.info(f"Перепривязка: трек {target_track_id} -> {tid} (по лицу)")
                        target_track_id = tid
                        track_lost_since = None

            if found:
                consecutive += 1
            else:
                consecutive = 0

            target_hit = consecutive >= cfg["face"]["consecutive_frames"]

            if target_hit and target_track_id is None and last_face_bbox is not None:
                tid = find_person_for_face(tracks, last_face_bbox)
                if tid is not None:
                    target_track_id = tid
                    logger.info(f"Привязка к треку {tid} (по лицу)")

            if target_track_id is not None:
                if target_track_id in tracks:
                    track_lost_since = None
                    last_target_seen = ts
                else:
                    if track_lost_since is None:
                        track_lost_since = ts
                        logger.info(f"Трек {target_track_id} потерян, grace {grace_seconds}с")
                    elif ts - track_lost_since > grace_seconds:
                        logger.info("Цель ушла (grace истёк)")
                        target_track_id = None
                        track_lost_since = None
                        last_face_bbox = None

            target_present = found or target_track_id is not None or manual_now

            if target_present and not writing:
                if ts - last_disk_check > 30:
                    last_disk_check = ts
                    free_gb = shutil.disk_usage(rec_dir).free / 1024**3
                    disk_ok = free_gb >= cfg["resilience"]["min_free_gb"]
                if not disk_ok:
                    logger.error(f"Недостаточно места на диске (нужно {cfg['resilience']['min_free_gb']} ГБ). Запись пропущена.")
                else:
                    rec_start_ts = ts
                    # Целевой FPS таймлайна: recording.fps из конфига, иначе — по
                    # камере (округлённо). Пропуски кадров писатель закрывает
                    # дублями, так что «медленная подача» не ломает синхрон.
                    target_fps = int(cfg["recording"].get("fps", 0) or 0)
                    if target_fps <= 0:
                        measured = capture.current_fps
                        target_fps = int(round(measured)) if 10.0 <= measured <= 60.0 else 30
                    writer_thread.fps = float(target_fps)
                    writer_thread.start_recording(frame)
                    for bts, bf in pre_buf.release():
                        writer_thread.put(bts, bf)
                    active = True
                    tail_frames = 0
                    # Переход IDLE -> RECORDING и снимок «цель в кадре» — целиком
                    # внутри стейт-машины (снимок уедет в фазе SENDING_TELEGRAM).
                    pipeline.on_recording_started(frame, ts)
                    writing = True

            elif writing:
                writer_thread.put(ts, frame)
                if target_present:
                    active = True
                    tail_frames = 0
                else:
                    tail_frames += 1
                # Останавливаем запись, когда цель ушла (хвост записан) ИЛИ достигнут лимит.
                max_sec = cfg["recording"].get("max_seconds", 0)
                tail_done = (not target_present) and tail_frames > cfg["recording"]["post_seconds"] * 30
                cap_hit = bool(max_sec) and (ts - rec_start_ts) > max_sec
                if tail_done or cap_hit:
                    active = False
                    consecutive = 0
                    writer_thread.finish()
                    pipeline.on_recording_finished(
                        "лимит длительности" if cap_hit else "цель ушла"
                    )
                    writing = False
                    logger.info("<<< Запись завершена: " + ("лимит длительности" if cap_hit else "цель ушла"))
                    # Файл и окно аудио придут в finalized_q; Pipeline сам переведёт
                    # PROCESSING -> SENDING_TELEGRAM и отправит видео со звуком.

            if args.preview and (ts - last_preview > 1.0 / 15):
                last_preview = ts
                vis = frame.copy()
                color = (0, 0, 255) if writing else (0, 255, 0)
                cv2.putText(vis, "REC" if writing else "LIVE", (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 2)
                cv2.putText(vis, f"motion={changed:.3f}" if warmup_left == 0 else "motion=warmup",
                            (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
                if tracker is not None:
                    for tid, (x1, y1, x2, y2) in tracks.items():
                        is_target = tid == target_track_id
                        box_color = (0, 255, 0) if is_target else (128, 128, 128)
                        label = f"TARGET #{tid}" if is_target else f"#{tid}"
                        cv2.rectangle(vis, (int(x1), int(y1)), (int(x2), int(y2)), box_color, 2)
                        cv2.putText(vis, label, (int(x1), int(y1) - 5),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, box_color, 2)
                cv2.imshow("Security Camera", vis)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

            if ts - last_stats_log > 5:
                last_stats_log = ts
                cap_fps = len(capture.fps_window) / (capture.fps_window[-1] - capture.fps_window[0]) if len(capture.fps_window) > 1 else 0
                rec_fps, avg_write = writer_thread.get_stats()
                logger.info(
                    f"Статистика: захват={cap_fps:.1f} fps, запись={rec_fps:.1f} fps, "
                    f"распознавание={worker._last_recog_ms:.0f} мс, write={avg_write:.1f} мс"
                )

    except Exception:
        logger.error(f"Необработанное исключение:\n{traceback.format_exc()}")
        # Финализацию и доотправку последней записи сделает finally — здесь
        # finish() НЕ вызываем, иначе on_finalize отработает дважды на один файл.
        sys.exit(1)
    finally:
        # Сначала просим writer финализировать текущую запись, ПОТОМ гасим потоки.
        # Иначе запись, идущая в момент Ctrl+C, остаётся недописанной (без звука/индекса).
        timeout = cfg["resilience"]["graceful_shutdown_timeout"]
        # Дождаться финализации, упаковать остаток и разрешить отправку:
        # notifier.shutdown() ниже доработает очередь (склейка + отправка + архив).
        pipeline.shutdown(writer_thread, timeout)
        stop_event.set()
        if writer_thread.is_alive():
            writer_thread.join(timeout=timeout)
            if writer_thread.is_alive():
                logger.warning("WriterThread не завершился вовремя, закрываю принудительно")
        notifier.shutdown()
        archive.shutdown()
        audio.shutdown()
        keep_awake.stop()
        led.shutdown()          # гасим подсветку Logitech и отпускаем USB
        cv2.destroyAllWindows()
        remove_pid_file()


if __name__ == "__main__":
    main()
