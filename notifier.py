"""Уведомления в Telegram через Bot API (requests), отправка в фоновом потоке.

Все публичные методы неблокирующие: они только кладут задание в очередь, а
реальную отправку и обработку видео выполняет отдельный поток-воркер.

Ключевая особенность — «микширование» аудио: если у нас есть запись с камеры
(обычно без звуковой дорожки) и доступен AudioRecorder, то по временному окну
вырезается аудиоклип и склеивается с видео через ffmpeg. Затем видео приводится
к H.264/AAC (Telegram не проигрывает mp4v) и при необходимости сжимается под
ограничение размера.
"""

import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import requests

from paths import resolve_ffmpeg

# Базовый адрес Telegram Bot API (метод добавляется к нему).
TG_API = "https://api.telegram.org"

# На Windows дочерний процесс (ffmpeg) из приложения БЕЗ консоли открывает
# собственное окно консоли. CREATE_NO_WINDOW это подавляет.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# Префиксы временных файлов, которые создаёт нотификатор и должен удалять сам.
TEMP_PREFIXES = ("_h264_", "_av_", "_h264_low_")


class TelegramNotifier:
    """Отправляет текст, снимки и видео в Telegram в фоновом потоке."""

    def __init__(
        self,
        tg_cfg: dict,
        logger: logging.Logger,
        base_dir: Path,
        stop_event: threading.Event,
        audio=None,
    ):
        # Конфигурация и внешние зависимости.
        self.tg_cfg = tg_cfg or {}
        self.logger = logger
        self.base_dir = base_dir
        self.stop_event = stop_event
        # audio — опциональный AudioRecorder (см. audio.py). Импорт модуля audio
        # здесь намеренно не делается, чтобы не тянуть sounddevice.
        self.audio = audio

        self.token = str(self.tg_cfg.get("token", "") or "")

        # chat_ids (список) или одиночный chat_id.
        chat_ids = self.tg_cfg.get("chat_ids") or []
        if not chat_ids and self.tg_cfg.get("chat_id"):
            chat_ids = [self.tg_cfg["chat_id"]]
        self.chat_ids = [str(c) for c in chat_ids]

        # Параметры поведения.
        self.cooldown = float(self.tg_cfg.get("cooldown_seconds", 30))
        self.retry_base = float(self.tg_cfg.get("retry_base_seconds", 5))
        self.retry_max = max(1, int(self.tg_cfg.get("retry_max_attempts", 3)))
        self.ffmpeg_path = resolve_ffmpeg(self.tg_cfg.get("ffmpeg_path", "ffmpeg"))
        self.telegram_max_mb = float(self.tg_cfg.get("telegram_max_mb", 49))
        self.send_photo = bool(self.tg_cfg.get("send_photo", True))
        self.send_video = bool(self.tg_cfg.get("send_video", True))

        # Внешний потребитель готового файла (например, выгрузка на сервер-архив).
        # Вызывается после того, как в запись вшит звук.
        self.on_ready = None

        # Нотификатор активен только если есть токен и хотя бы один чат.
        self.enabled = bool(self.token and self.chat_ids)
        if not self.enabled:
            logger.warning(
                "Telegram отключён: не заданы token или chat_ids — уведомления не отправляются"
            )

        # Очередь заданий и служебное состояние воркера.
        self._queue: queue.Queue = queue.Queue()
        self._last_notify_ts: float = 0.0
        self._temp_files: set[Path] = set()
        self._ffmpeg_warned = False
        # Временные файлы ffmpeg кладём в отдельную подпапку, чтобы они НЕ попадали
        # под glob *.mp4 очистки записей (DiskCleaner) и не удалялись в момент
        # перекодирования/отправки.
        self.tmp_dir = Path(base_dir) / "_tmp"

        # Поток-воркер живёт до взведения stop_event.
        self._thread = threading.Thread(
            target=self._worker, daemon=True, name="TelegramNotifier"
        )
        self._thread.start()

        # Подчистить временные файлы, оставшиеся от предыдущего запуска.
        try:
            if self.tmp_dir.exists():
                for f in self.tmp_dir.glob("*"):
                    self._unlink(f)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # Публичный интерфейс (неблокирующий)
    # ------------------------------------------------------------------ #

    def send_message(self, text: str) -> None:
        """Поставить текстовое сообщение в очередь (с учётом cooldown)."""
        if not self.enabled:
            return
        now = time.time()
        if now - self._last_notify_ts < self.cooldown:
            return
        self._last_notify_ts = now
        self._queue.put(("message", text, None))

    def send_snapshot(self, frame: np.ndarray, caption: str) -> None:
        """Закодировать кадр (numpy BGR) в JPEG в памяти и поставить в очередь."""
        if not self.enabled or not self.send_photo:
            return
        if frame is None:
            return
        try:
            ok, buf = cv2.imencode(
                ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85]
            )
            if not ok:
                self.logger.warning("Не удалось закодировать снимок в JPEG")
                return
            self._queue.put(("photo", caption, buf.tobytes()))
        except Exception as e:  # никогда не роняем вызывающий поток
            self.logger.warning(f"Ошибка кодирования снимка: {e}")

    def enqueue_video(
        self,
        path: Path,
        caption: str,
        audio_window: tuple[float, float] | None = None,
        replace_source: bool = False,
    ) -> None:
        """Поставить видео в очередь.

        audio_window — (start_ts, end_ts) или None.
        replace_source=True — вшить звук в саму запись на диске (работает даже если
        Telegram отключён), чтобы локальный mp4 тоже был со звуком.
        """
        if path is None:
            return
        if not self.enabled and not replace_source:
            return
        if self.enabled and not self.send_video and not replace_source:
            return
        self._queue.put(("video", caption, (Path(path), audio_window, replace_source)))

    def shutdown(self) -> None:
        """Дождаться, пока воркер доработает очередь (мукс/отправку) и выйдет."""
        try:
            if self._thread.is_alive():
                # Мукс длинной записи может занять время — ждём щедро.
                self._thread.join(timeout=180)
                if self._thread.is_alive():
                    self.logger.warning("TelegramNotifier: воркер не успел завершиться")
        except Exception:
            pass
        self.logger.info("TelegramNotifier: завершение")

    # ------------------------------------------------------------------ #
    # Воркер
    # ------------------------------------------------------------------ #

    def _worker(self) -> None:
        """Разбирает очередь; при stop_event дорабатывает оставшиеся задания и выходит."""
        while True:
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self.stop_event.is_set():
                    break
                continue

            try:
                kind, payload, extra = item
                if kind == "message":
                    self._broadcast_message(payload)
                elif kind == "photo":
                    self._broadcast_photo(payload, extra)
                elif kind == "video":
                    path, audio_window, replace_source = extra
                    self._broadcast_video(payload, path, audio_window, replace_source)
                else:
                    self.logger.warning(f"Неизвестный тип задания: {kind}")
            except Exception as e:
                # Ни одно одиночное задание не должно убивать воркер.
                self.logger.error(f"Ошибка обработки задания Telegram: {e}")

            # При остановке дожидаемся пустой очереди (доделываем мукс/отправку).
            if self.stop_event.is_set() and self._queue.empty():
                break

    def _broadcast_message(self, text: str) -> None:
        """Отправить текст во все чаты с повторами."""
        for chat_id in self.chat_ids:
            self._send_with_retry(
                "sendMessage", chat_id, data={"chat_id": chat_id, "text": text}
            )

    def _broadcast_photo(self, caption: str, jpeg_bytes: bytes) -> None:
        """Отправить снимок во все чаты с повторами."""
        for chat_id in self.chat_ids:
            self._send_with_retry(
                "sendPhoto",
                chat_id,
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": ("snapshot.jpg", jpeg_bytes)},
                timeout=60,
            )

    def _broadcast_video(self, caption: str, path: Path, audio_window, replace_source: bool = False) -> None:
        """Полный конвейер видео: микширование аудио -> H.264 -> лимит размера."""
        self._temp_files = set()
        parent: Path | None = None
        try:
            path = Path(path)
            parent = path.parent
            if not path.exists():
                self.logger.error(f"Видеофайл не найден: {path}")
                return

            send_path = self._build_send_file(path, audio_window)

            # Вшить звук в саму запись на диске (если удалось собрать новую версию).
            if (
                replace_source
                and send_path is not None
                and send_path != path
                and send_path.parent == self.tmp_dir
                and send_path.exists()
            ):
                try:
                    os.replace(send_path, path)
                    send_path = path
                    self.logger.info(f"Звук вшит в запись: {path.name}")
                except OSError as e:
                    self.logger.warning(f"Не удалось вшить звук в {path.name}: {e}")

            # Файл на диске готов (со звуком) — отдаём внешнему потребителю (выгрузка).
            if self.on_ready is not None:
                try:
                    self.on_ready(path)
                except Exception as e:
                    self.logger.warning(f"on_ready упал: {e}")

            if send_path is None:
                # Файл не удалось уложить в лимит — отправляем текстом.
                self._broadcast_message(
                    f"{caption}\nВидео слишком большое для Telegram, файл сохранён: {path.name}"
                )
                return

            # Если Telegram выключен — ограничиваемся локальным муксом.
            if not (self.enabled and self.send_video):
                return

            for chat_id in self.chat_ids:
                self._send_video_with_retry(chat_id, send_path, caption)
        except Exception as e:
            self.logger.error(f"Ошибка обработки видео {path}: {e}")
        finally:
            # Всегда подчищаем созданные временные файлы.
            self._cleanup_temp(parent)

    # ------------------------------------------------------------------ #
    # Конвейер обработки видео
    # ------------------------------------------------------------------ #

    def _build_send_file(self, video_path: Path, audio_window) -> Path | None:
        """Готовит файл к отправке или None, если он не влезает в лимит.

        1) исходный mp4 (может быть без звука);
        2) микширование аудио по окну (если доступно);
        3) гарантия H.264/AAC;
        4) сжатие под telegram_max_mb.
        """
        # Без ffmpeg ничего обработать не можем — шлём как есть.
        if not self._has_ffmpeg():
            return video_path

        # 2. Микширование аудио. _mux_audio возвращает исходный путь, если
        # аудио нет/недоступно.
        muxed = self._mux_audio(video_path, audio_window)

        # 3. Гарантируем H.264/AAC. После микширования файл уже закодирован
        # libx264+aac, поэтому повторно не перекодируем (избегаем потери качества).
        if muxed != video_path:
            current = muxed
        else:
            current = self._transcode_h264(video_path)

        try:
            size_mb = current.stat().st_size / 1024**2
        except OSError as e:
            self.logger.error(f"Не удалось прочитать размер {current}: {e}")
            return video_path

        if size_mb <= self.telegram_max_mb:
            return current

        # 4. Слишком большой файл — перекодируем под расчётный битрейт.
        duration = self._get_duration(current)
        if duration <= 0:
            self.logger.warning(
                "Не удалось определить длительность — сжатие по битрейту пропущено"
            )
            return None

        low = self._reencode_low(current, duration)
        if low is None:
            return None

        try:
            low_mb = low.stat().st_size / 1024**2
        except OSError:
            return None

        if low_mb <= self.telegram_max_mb:
            self.logger.info(f"Видео сжато: {size_mb:.1f} МБ -> {low_mb:.1f} МБ")
            return low

        self.logger.warning(
            f"Файл всё ещё {low_mb:.1f} МБ > {self.telegram_max_mb} МБ — отправлю текст"
        )
        return None

    def _mux_audio(self, video_path: Path, audio_window) -> Path:
        """Склеить видео с вырезанным аудиоклипом. При неудаче вернуть оригинал."""
        # Нет окна, нет рекордера или он выключен — микшировать нечего.
        if (
            audio_window is None
            or self.audio is None
            or not getattr(self.audio, "enabled", False)
        ):
            return video_path

        if not self._has_ffmpeg():
            return video_path

        try:
            start_ts, end_ts = audio_window
        except (TypeError, ValueError):
            self.logger.warning("Некорректное окно аудио — микширование пропущено")
            return video_path

        wav: Path | None = None
        try:
            wav = self.audio.extract_window(start_ts, end_ts)
        except Exception as e:
            self.logger.warning(f"Не удалось вырезать аудио: {e}")
            return video_path

        if wav is None:
            self.logger.info("Аудиоклип для окна не найден — шлю видео без звука")
            return video_path

        out = self._tmp_path("_av_" + video_path.stem + ".mp4")
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", str(video_path),
            "-i", str(wav),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "64k",
            "-shortest", "-movflags", "+faststart",
            str(out),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=180, creationflags=_NO_WINDOW)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            self.logger.error(f"Микширование аудио не удалось: {e}")
            return video_path
        finally:
            # Клип больше не нужен в любом случае.
            self._unlink(wav)

        self._temp_files.add(out)
        self.logger.info(f"Аудио смикшировано с видео: {out.name}")
        return out

    def _transcode_h264(self, path: Path) -> Path:
        """Перекодировать в H.264/AAC (Telegram не проигрывает mp4v)."""
        if not self._has_ffmpeg():
            return path

        out = self._tmp_path("_h264_" + path.name)
        cmd = [
            self.ffmpeg_path, "-y", "-i", str(path),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "64k",
            str(out),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=180, creationflags=_NO_WINDOW)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            self.logger.error(f"Перекодирование в H.264 не удалось: {e}")
            return path

        self._temp_files.add(out)
        self.logger.info(f"H.264-версия готова: {out.name}")
        return out

    def _reencode_low(self, path: Path, duration: float) -> Path | None:
        """Перекодировать под целевой битрейт, чтобы уложиться в лимит."""
        if not self._has_ffmpeg():
            return None

        target_kbps = int(self.telegram_max_mb * 8192 / duration * 0.92)
        target_kbps = max(50, target_kbps)
        self.logger.info(
            f"Сжатие по битрейту: {target_kbps} kbps (длительность {duration:.1f}с)"
        )

        out = self._tmp_path("_h264_low_" + path.name)
        cmd = [
            self.ffmpeg_path, "-y", "-i", str(path),
            "-c:v", "libx264", "-b:v", f"{target_kbps}k",
            "-maxrate", f"{target_kbps}k", "-bufsize", f"{target_kbps * 2}k",
            "-preset", "veryfast", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "64k",
            str(out),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=180, creationflags=_NO_WINDOW)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            self.logger.error(f"Сжатие по битрейту не удалось: {e}")
            return None

        self._temp_files.add(out)
        return out

    def _get_duration(self, path: Path) -> float:
        """Длительность файла в секундах (парсим "Duration:" из stderr ffmpeg)."""
        if not shutil.which(self.ffmpeg_path):
            return 0.0
        cmd = [self.ffmpeg_path, "-i", str(path)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW)
            for line in r.stderr.splitlines():
                if "Duration:" in line:
                    part = line.split("Duration:")[1].split(",")[0].strip()
                    h, m, s = part.split(":")
                    return int(h) * 3600 + int(m) * 60 + float(s)
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
            pass
        return 0.0

    # ------------------------------------------------------------------ #
    # Отправка и ретраи
    # ------------------------------------------------------------------ #

    def _api(self, method: str, chat_id: str, **kwargs) -> dict | None:
        """Один вызов метода Bot API. При ошибке вернуть None."""
        timeout = kwargs.pop("timeout", 30)
        url = f"{TG_API}/bot{self.token}/{method}"
        try:
            r = requests.post(url, timeout=timeout, **kwargs)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            self.logger.warning(f"Telegram API {method} для {chat_id}: {e}")
            return None

    def _send_with_retry(self, method: str, chat_id: str, **kwargs) -> bool:
        """Отправить с экспоненциальным backoff (retry_base * 2**attempt)."""
        for attempt in range(self.retry_max):
            if self._api(method, chat_id, **kwargs):
                return True
            if attempt < self.retry_max - 1:
                delay = self.retry_base * (2 ** attempt)
                self.logger.warning(
                    f"Повтор {method} для {chat_id} через {delay:.0f}с..."
                )
                if self.stop_event.wait(delay):
                    return False
        self.logger.error(
            f"Не удалось выполнить {method} для {chat_id} за {self.retry_max} попыток"
        )
        return False

    def _send_video_with_retry(self, chat_id: str, path: Path, caption: str) -> bool:
        """Отправить видео с ретраями."""
        for attempt in range(self.retry_max):
            if self._send_video(chat_id, path, caption):
                return True
            if attempt < self.retry_max - 1:
                delay = self.retry_base * (2 ** attempt)
                self.logger.warning(
                    f"Повтор sendVideo для {chat_id} через {delay:.0f}с..."
                )
                if self.stop_event.wait(delay):
                    return False
        self.logger.error(
            f"Не удалось отправить видео {path.name} для {chat_id} за {self.retry_max} попыток"
        )
        return False

    def _send_video(self, chat_id: str, path: Path, caption: str) -> bool:
        """Одна попытка sendVideo."""
        try:
            with open(path, "rb") as f:
                r = requests.post(
                    f"{TG_API}/bot{self.token}/sendVideo",
                    data={"chat_id": chat_id, "caption": caption},
                    files={"video": f},
                    timeout=120,
                )
            r.raise_for_status()
            self.logger.info(f"Видео отправлено: {path.name} для {chat_id}")
            return True
        except Exception as e:
            self.logger.warning(f"sendVideo для {chat_id}: {e}")
            return False

    # ------------------------------------------------------------------ #
    # Вспомогательное
    # ------------------------------------------------------------------ #

    def _has_ffmpeg(self) -> bool:
        """Есть ли ffmpeg в PATH. Один раз пишет понятное предупреждение."""
        if shutil.which(self.ffmpeg_path):
            return True
        if not self._ffmpeg_warned:
            self._ffmpeg_warned = True
            self.logger.error(
                f"ffmpeg не найден ({self.ffmpeg_path}). Установите ffmpeg: "
                "macOS — brew install ffmpeg, Windows — скачайте с ffmpeg.org и "
                "добавьте в PATH. Отправляю видео без обработки."
            )
        return False

    def _tmp_path(self, name: str) -> Path:
        """Путь временного файла в отдельной папке _tmp (создаётся при надобности)."""
        try:
            self.tmp_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.logger.warning(f"Не удалось создать папку temp {self.tmp_dir}: {e}")
        return self.tmp_dir / name

    def _cleanup_temp(self, parent: Path | None = None) -> None:
        """Удалить созданные временные файлы (и подчистить остатки в _tmp)."""
        for p in list(self._temp_files):
            self._unlink(p)
        self._temp_files.clear()

        try:
            for prefix in TEMP_PREFIXES:
                for f in self.tmp_dir.glob(prefix + "*"):
                    self._unlink(f)
        except OSError as e:
            self.logger.warning(f"Не удалось подчистить временные файлы: {e}")

    def _unlink(self, path: Path | None) -> None:
        """Безопасно удалить файл (ошибки игнорируем)."""
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
