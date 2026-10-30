"""Охранная камера: motion gate -> распознавание лица -> трекинг -> запись + Telegram."""

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
from logging.handlers import RotatingFileHandler
from pathlib import Path

import cv2
import numpy as np
import yaml

from audio import AudioRecorder
from keepawake import KeepAwake
from notifier import TelegramNotifier
from paths import APP_DIR, find_resource, resolve_ffmpeg
from uploader import ArchiveUploader

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
    except (ProcessLookupError, PermissionError, OSError):
        PID_FILE.unlink(missing_ok=True)
        return True


def write_pid_file():
    PID_FILE.write_text(str(os.getpid()))


def remove_pid_file():
    PID_FILE.unlink(missing_ok=True)


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

    def start_recording(self, frame: np.ndarray):
        # Дождаться финализации предыдущей записи, чтобы она не закрыла наш writer.
        if not self._finalize_done.wait(timeout=10):
            self.logger.warning("Предыдущая запись не финализировалась за 10с")
        with self._lock:
            h, w = frame.shape[:2]
            name = time.strftime("%Y-%m-%d_%H-%M-%S") + ".mp4"
            self.path = self.out_dir / name
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            self.writer = cv2.VideoWriter(str(self.path), fourcc, 25.0, (w, h))
            if not self.writer.isOpened():
                raise RuntimeError("VideoWriter не открылся")
            self.timestamps = []
            self.first_ts = None
            self.last_ts = None
            self.window = None
            self._recording.set()

    def put(self, ts: float, frame: np.ndarray):
        try:
            self.q.put_nowait((ts, frame))
        except queue.Full:
            try:
                self.q.get_nowait()
            except queue.Empty:
                pass
            self.logger.warning("Очередь записи переполнена, пропущен кадр")
            try:
                self.q.put_nowait((ts, frame))
            except queue.Full:
                pass

    def finish(self):
        self._recording.clear()
        self._finalize_done.clear()
        self.q.put(None)

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
                    self.writer.write(frame)
                    w_ms = (time.time() - w_t0) * 1000
                    self.write_times.append(w_ms)
                    if w_ms > 33:
                        self.logger.warning(f"write() медленный: {w_ms:.1f} мс")
                    self.timestamps.append(ts)
                    self.fps_window.append(ts)
                    if self.first_ts is None:
                        self.first_ts = ts
                    self.last_ts = ts
                # При остановке: доработать остаток очереди и финализировать запись.
                if self.stop_event.is_set() and self.q.empty():
                    self._finalize()
                    break
        except Exception:
            self.logger.error(f"WriterThread упал:\n{traceback.format_exc()}")

    def _finalize(self):
        with self._lock:
            if self.writer is not None:
                self.writer.release()
                self.writer = None
            if len(self.timestamps) >= 2:
                span = self.timestamps[-1] - self.timestamps[0]
                if span > 0 and self.path and self.path.exists():
                    real_fps = (len(self.timestamps) - 1) / span
                    self.logger.info(f"Запись завершена: {self.path.name}, реальный FPS={real_fps:.1f}")
            if self.first_ts is not None and self.last_ts is not None:
                self.window = (self.first_ts, self.last_ts)
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

    mot = cfg["motion"]
    bg = cv2.createBackgroundSubtractorMOG2(
        history=mot["history"], varThreshold=mot["var_threshold"], detectShadows=True
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    writer_thread = WriterThread(rec_dir, cfg, logger, stop_event)
    writer_thread.start()

    def _on_recording_done(path, window):
        """Финализация записи: вшиваем звук в саму запись и ставим видео в Telegram."""
        aw = window if (audio.enabled and window) else None
        # Локально вшиваем звук прямо в файл записи (работает и без Telegram).
        want_local = bool(cfg["recording"].get("mux_audio", True)) and aw is not None
        if not notifier.enabled and not want_local:
            # Telegram выключен и мукс не нужен — но архив должен получить файл.
            archive.upload_video(path)
            return
        notifier.enqueue_video(
            path,
            f"Запись завершена: {path.name}",
            audio_window=aw,
            replace_source=want_local,
        )

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
    last_notify = 0.0
    last_known_emb_time = emb_path.stat().st_mtime
    last_recog_submit = 0.0
    warmup_left = mot["warmup_frames"]
    last_global_reset = 0.0
    last_light_log = 0.0
    last_stats_log = 0.0
    last_preview = 0.0
    last_motion_ts = 0.0
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

    logger.info("Старт цикла. Ctrl+C для остановки.")
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

            h, w = frame.shape[:2]
            if w != capture.start_w or h != capture.start_h:
                logger.warning(
                    f"Разрешение камеры изменилось: {capture.start_w}x{capture.start_h} -> {w}x{h}. "
                    "Текущая запись закроется как есть, новая — под новый размер."
                )
                if writing:
                    writer_thread.finish()
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
            heavy_active = motion_on or target_track_id is not None or writing
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

            target_present = found or target_track_id is not None

            if target_present and not writing:
                if ts - last_disk_check > 30:
                    last_disk_check = ts
                    free_gb = shutil.disk_usage(rec_dir).free / 1024**3
                    disk_ok = free_gb >= cfg["resilience"]["min_free_gb"]
                if not disk_ok:
                    logger.error(f"Недостаточно места на диске (нужно {cfg['resilience']['min_free_gb']} ГБ). Запись пропущена.")
                else:
                    writing = True
                    rec_start_ts = ts
                    writer_thread.start_recording(frame)
                    for bts, bf in pre_buf.release():
                        writer_thread.put(bts, bf)
                    active = True
                    tail_frames = 0
                    logger.info(">>> ТРИГГЕР: цель в кадре, запись началась")
                    if notifier.enabled and (ts - last_notify > cfg["telegram"]["cooldown_seconds"]):
                        last_notify = ts
                        # Быстрое уведомление: JPEG-снимок сразу.
                        notifier.send_snapshot(frame, f"Цель в кадре: {time.strftime('%H:%M:%S')}")

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
                    writing = False
                    consecutive = 0
                    writer_thread.finish()
                    logger.info("<<< Запись завершена: " + ("лимит длительности" if cap_hit else "цель ушла"))
                    # Видео+аудио отправит колбэк on_finalize — после слива очереди,
                    # с точным окном (first_ts..last_ts).

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
        if writing:
            try:
                writer_thread.finish()
            except Exception:
                pass
        sys.exit(1)
    finally:
        # Сначала просим writer финализировать текущую запись, ПОТОМ гасим потоки.
        # Иначе запись, идущая в момент Ctrl+C, остаётся недописанной (без звука/индекса).
        if writing:
            writer_thread.finish()
        stop_event.set()
        if writing:
            timeout = cfg["resilience"]["graceful_shutdown_timeout"]
            writer_thread.join(timeout=timeout)
            if writer_thread.is_alive():
                logger.warning("WriterThread не завершился вовремя, закрываю принудительно")
        notifier.shutdown()
        archive.shutdown()
        audio.shutdown()
        keep_awake.stop()
        cv2.destroyAllWindows()
        remove_pid_file()


if __name__ == "__main__":
    main()
