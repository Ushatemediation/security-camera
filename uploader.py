"""Выгрузка записей на сервер HomeCamera (веб-галерея для просмотра).

Работает в отдельном потоке с очередью, чтобы не тормозить захват кадров.
Включается секцией ``server`` в config.yaml::

    server:
      enabled: true
      url: "https://193.8.215.5:8080"
      token: "…"
      verify_tls: false      # самоподписанный сертификат
      camera: "home"
"""

import datetime
import queue
import threading
import urllib3
from pathlib import Path

try:
    import requests
except ImportError:  # requests приходит с ultralytics, но проверим
    requests = None


class ArchiveUploader:
    """Ставит готовые записи в очередь и выгружает их на сервер."""

    def __init__(self, cfg: dict, logger, stop_event):
        self.cfg = cfg or {}
        self.logger = logger
        self.stop_event = stop_event
        self.url = (self.cfg.get("url") or "").rstrip("/")
        self.token = self.cfg.get("token") or ""
        self.verify_tls = bool(self.cfg.get("verify_tls", True))
        self.camera = self.cfg.get("camera") or ""
        self.timeout = int(self.cfg.get("timeout", 180))

        self.enabled = bool(self.cfg.get("enabled")) and bool(self.url) and bool(self.token)
        if self.enabled and requests is None:
            logger.warning("ArchiveUploader: модуль requests не найден — выгрузка отключена")
            self.enabled = False
        if self.enabled and not self.verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True, name="ArchiveUploader")
        if self.enabled:
            self._thread.start()
            logger.info(f"ArchiveUploader: выгрузка на {self.url} включена")
        else:
            logger.info("ArchiveUploader: выгрузка на сервер отключена")

    # ------------------------------------------------------------------ #
    def upload_video(self, path) -> None:
        """Поставить готовую запись в очередь на выгрузку."""
        if not self.enabled or path is None:
            return
        try:
            p = Path(path)
            if p.exists():
                self._queue.put(p)
        except Exception as e:
            self.logger.warning(f"ArchiveUploader: не удалось поставить в очередь: {e}")

    # ------------------------------------------------------------------ #
    def _worker(self) -> None:
        while True:
            try:
                path = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self.stop_event.is_set():
                    break
                continue
            try:
                self._send(path)
            except Exception as e:
                self.logger.warning(f"ArchiveUploader: ошибка: {e}")
            if self.stop_event.is_set() and self._queue.empty():
                break

    def _send(self, path: Path) -> None:
        if not path.exists():
            return
        ts = datetime.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        size_mb = path.stat().st_size / 1048576
        try:
            with open(path, "rb") as fh:
                r = requests.post(
                    f"{self.url}/api/upload",
                    headers={"X-Token": self.token},
                    files={"file": (path.name, fh, "video/mp4")},
                    data={"kind": "video", "camera": self.camera,
                          "note": path.stem, "ts": ts},
                    verify=self.verify_tls,
                    timeout=self.timeout,
                )
            if r.ok:
                self.logger.info(f"ArchiveUploader: загружено {path.name} ({size_mb:.1f} МБ)")
            else:
                self.logger.warning(
                    f"ArchiveUploader: сервер ответил {r.status_code} на {path.name}: {r.text[:200]}"
                )
        except Exception as e:
            self.logger.warning(f"ArchiveUploader: не удалось загрузить {path.name}: {e}")

    # ------------------------------------------------------------------ #
    def shutdown(self) -> None:
        try:
            if self.enabled and self._thread.is_alive():
                self._thread.join(timeout=60)
                if self._thread.is_alive():
                    self.logger.warning("ArchiveUploader: очередь не успела догрузиться")
        except Exception:
            pass
