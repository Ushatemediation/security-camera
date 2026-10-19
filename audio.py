"""Фоновый рекордер аудио: ведёт «катящееся» окно микрофона.

Два хранилища:

* **Кольцо в памяти** (`buffer_seconds` секунд) — основной источник для
  `extract_window`. Важно: оно включает ещё НЕ закрытый (текущий) блок, поэтому
  хвост события не теряется. 60с моно 16 кГц int16 ≈ 2 МБ.
* **WAV-сегменты на диске** (`_audio/seg_*.wav`) — для более длинной истории и
  диагностики; используются как запасной источник, если окно не помещается в кольцо.

Захват — через `sounddevice`, запись WAV — через стандартный модуль `wave`.
Модуль импортируется без ошибок даже при отсутствии аудиоустройства:
устройство открывается только внутри `run()`.
"""

import logging
import threading
import time
import wave
from collections import deque
from pathlib import Path

import numpy as np

try:
    import sounddevice as sd
except Exception:  # звуковая библиотека может быть не установлена
    sd = None


class AudioRecorder(threading.Thread):
    """Пишет аудио с микрофона в кольцо (RAM) и WAV-сегменты (диск)."""

    def __init__(
        self,
        cfg: dict,
        logger: logging.Logger,
        base_dir: Path,
        stop_event: threading.Event,
    ):
        super().__init__(daemon=True, name="AudioRecorder")
        self.cfg = cfg or {}
        self.logger = logger
        self.base_dir = base_dir
        self.stop_event = stop_event

        self.seg_dir = base_dir / "_audio"

        self.samplerate = int(self.cfg.get("samplerate", 16000))
        self.channels = int(self.cfg.get("channels", 1))
        self.device = self.cfg.get("device", None)
        self.segment_seconds = float(self.cfg.get("segment_seconds", 5.0))
        self.buffer_seconds = float(self.cfg.get("buffer_seconds", 60.0))

        want_enabled = bool(self.cfg.get("enabled", True))
        self.enabled = want_enabled and (sd is not None)
        if not want_enabled:
            self.logger.info("AudioRecorder отключён в конфиге")
        elif sd is None:
            self.logger.warning(
                "sounddevice недоступен — запись аудио отключена (установите sounddevice)"
            )

        # Закрытые сегменты на диске: (start_ts, путь) под блокировкой.
        self._segments: list[tuple[float, Path]] = []
        self._lock = threading.Lock()

        # Кольцо последних buffer_seconds аудио в памяти (включая текущий блок).
        self._ring: deque[tuple[float, np.ndarray]] = deque()
        self._ring_frames = 0
        self._ring_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Захват
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """Основной цикл: читает блоки звука, пишет их на диск и в кольцо."""
        if not self.enabled:
            return

        try:
            self.seg_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.logger.error(f"Не удалось создать папку аудио {self.seg_dir}: {e}")
            self.enabled = False
            return

        blocksize = 1024
        frames_per_seg = max(1, int(self.segment_seconds * self.samplerate))

        stream = None
        wav: wave.Wave_write | None = None
        seg_start_ts = 0.0
        seg_path: Path | None = None
        seg_frames = 0

        try:
            self.logger.info(
                f"AudioRecorder запущен: {self.samplerate} Гц, каналов {self.channels}, "
                f"сегмент {self.segment_seconds}с, буфер {self.buffer_seconds}с"
            )
            stream = sd.InputStream(
                samplerate=self.samplerate,
                channels=self.channels,
                dtype="int16",
                device=self.device,
                blocksize=blocksize,
            )
            stream.start()

            while not self.stop_event.is_set():
                data, overflowed = stream.read(blocksize)
                if overflowed:
                    self.logger.warning("Переполнение входного аудиобуфера (overflow)")

                block = np.array(data, dtype=np.int16, copy=True)  # (frames, channels)

                # --- на диск ---
                if wav is None:
                    seg_start_ts = time.time()
                    seg_path = self.seg_dir / f"seg_{seg_start_ts:.3f}.wav"
                    wav = wave.open(str(seg_path), "wb")
                    wav.setnchannels(self.channels)
                    wav.setsampwidth(2)  # int16
                    wav.setframerate(self.samplerate)
                    seg_frames = 0
                wav.writeframes(block.tobytes())
                seg_frames += int(block.shape[0])

                if seg_frames >= frames_per_seg:
                    wav.close()
                    wav = None
                    with self._lock:
                        self._segments.append((seg_start_ts, seg_path))
                    self._prune()

                # --- в кольцо памяти (точный хвост, включая текущий блок) ---
                self._push_ring(time.time(), block)

        except Exception as e:  # ошибка sounddevice и прочее — не валим процесс
            self.logger.error(f"AudioRecorder остановлен из-за ошибки: {e}")
            self.enabled = False
        finally:
            if wav is not None:
                try:
                    wav.close()
                except Exception:
                    pass
                if seg_path is not None:
                    with self._lock:
                        self._segments.append((seg_start_ts, seg_path))
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass

    def _push_ring(self, ts: float, block: np.ndarray) -> None:
        """Добавить блок в кольцо, удерживая не более buffer_seconds."""
        max_frames = max(1, int(self.buffer_seconds * self.samplerate))
        with self._ring_lock:
            self._ring.append((ts, block))
            self._ring_frames += int(block.shape[0])
            while self._ring and self._ring_frames > max_frames:
                old = self._ring.popleft()
                self._ring_frames -= int(old[1].shape[0])

    def _prune(self) -> None:
        """Удаляет сегменты на диске старше now - buffer_seconds."""
        if self.buffer_seconds <= 0:
            return
        cutoff = time.time() - self.buffer_seconds
        victims: list[Path] = []
        with self._lock:
            kept: list[tuple[float, Path]] = []
            for start_ts, path in self._segments:
                if start_ts >= cutoff:
                    kept.append((start_ts, path))
                else:
                    victims.append(path)
            self._segments = kept
        for path in victims:
            try:
                path.unlink(missing_ok=True)
            except OSError as e:
                self.logger.warning(f"Не удалось удалить старый сегмент {path.name}: {e}")

    # ------------------------------------------------------------------ #
    # Извлечение окна
    # ------------------------------------------------------------------ #

    def extract_window(self, start_ts: float, end_ts: float) -> Path | None:
        """Собирает клип за [start_ts, end_ts]. Возвращает путь к WAV или None."""
        if end_ts <= start_ts:
            return None

        pieces = self._collect_from_ring(start_ts, end_ts)
        if not pieces:
            # Запасной путь: диск (если окно старше кольца памяти).
            pieces = self._collect_from_disk(start_ts, end_ts)
        if not pieces:
            self.logger.info("extract_window: аудио для окна не найдено")
            return None

        out_arr = np.concatenate(pieces, axis=0)
        if out_arr.size == 0:
            return None

        try:
            self.seg_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.logger.error(f"Не удалось создать папку аудио {self.seg_dir}: {e}")
            return None

        out_path = self.seg_dir / f"_clip_{start_ts:.3f}.wav"
        try:
            with wave.open(str(out_path), "wb") as w:
                w.setnchannels(self.channels)
                w.setsampwidth(2)
                w.setframerate(self.samplerate)
                w.writeframes(np.ascontiguousarray(out_arr).tobytes())
        except (OSError, wave.Error) as e:
            self.logger.error(f"Не удалось записать клип {out_path.name}: {e}")
            return None

        self.logger.info(f"Аудиоклип собран: {out_path.name} ({out_arr.shape[0]} кадров)")
        return out_path

    def _collect_from_ring(self, start_ts: float, end_ts: float) -> list[np.ndarray]:
        """Собрать окно из кольца памяти (включая ещё не закрытый блок)."""
        with self._ring_lock:
            ring = list(self._ring)
        if not ring:
            return []
        # Кольцо должно покрывать начало окна; иначе доверимся диску.
        earliest = ring[0][0]
        if earliest > start_ts + 1.0:
            return []
        pieces: list[np.ndarray] = []
        for ts, block in ring:
            n = int(block.shape[0])
            seg_end = ts + n / float(self.samplerate)
            if seg_end <= start_ts or ts >= end_ts:
                continue
            a = int(round(max(0.0, start_ts - ts) * self.samplerate))
            b = int(round(min(n, (end_ts - ts) * self.samplerate)))
            a = max(0, min(a, n))
            b = max(a, min(b, n))
            if b > a:
                pieces.append(block[a:b])
        return pieces

    def _collect_from_disk(self, start_ts: float, end_ts: float) -> list[np.ndarray]:
        """Запасной путь: собрать окно из закрытых WAV-сегментов на диске."""
        with self._lock:
            segments = list(self._segments)
        segments.sort(key=lambda item: item[0])
        pieces: list[np.ndarray] = []
        for seg_start, path in segments:
            try:
                with wave.open(str(path), "rb") as w:
                    nframes = w.getnframes()
                    ch = w.getnchannels()
                    sr = w.getframerate()
                    if nframes <= 0:
                        continue
                    raw = w.readframes(nframes)
            except (OSError, wave.Error, EOFError) as e:
                self.logger.warning(f"Сегмент {path.name} не читается: {e}")
                continue
            if not raw:
                continue
            arr = np.frombuffer(raw, dtype=np.int16)
            if ch > 1:
                arr = arr.reshape(-1, ch)
            sr = sr or self.samplerate
            seg_end = seg_start + nframes / float(sr)
            if seg_end <= start_ts or seg_start >= end_ts:
                continue
            a = int(round(max(0.0, start_ts - seg_start) * sr))
            b = int(round(min(nframes, (end_ts - seg_start) * sr)))
            a = max(0, min(a, nframes))
            b = max(a, min(b, nframes))
            if b > a:
                pieces.append(arr[a:b])
        return pieces

    def shutdown(self) -> None:
        """Безопасное завершение: поток выходит по stop_event. Можно звать многократно."""
        self.logger.info("AudioRecorder: завершение (ожидание stop_event)")
