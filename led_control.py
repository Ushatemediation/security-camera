"""led_control.py — управление светодиодом (LED) веб-камер Logitech через UVC.

Как это работает
================
У камер Logitech подсветка управляется вендорским UVC-контролом внутри USB
Video Class **Extension Unit** (XU). Команда — обычный class-specific
USB-контроль-трансфер:

    SET_CUR:  bmRequestType = 0x21          (host→device, class, interface)
              bRequest      = 0x01          (SET_CUR)
              wValue        = 0x09 << 8     (control selector «LED1»)
              wIndex        = 0x0B << 8     (XU Unit ID 0x0B, interface 0)
              payload       = 5 байт:  [0x03, 0x00, 0x01, MODE, 0x01]

    где MODE: 0x00 — выключено, 0x01 — включено, 0x03 — авто.

Формат восстановлен по дампам SET_CUR для C920 (рассылка linux-uvc, тред
«Logitech C920 and the LED»): на камерах поколения C920/C930e/C270 «авто»
кодируется байтом 0x03. У очень старых моделей XU другой (control selector 1,
1 байт) — этот модуль умеет и его (см. _detect_led_control).

Публичный API (режимы: 0 — выключено, 1 — включено, 2 — авто):

    led = LogitechLEDController(logger, cfg.get("led"))
    led.apply_startup_mode()     # стартовая установка из config.yaml
    led.set_led_mode(1)          # «включено»
    led.get_led_mode()           # прочитать текущий режим (best-effort)
    led.close()                  # отпустить USB при завершении

Зависимости
===========
* pip:    pyusb (см. requirements.txt)
* system: libusb
    - macOS:   brew install libusb
    - Linux:   sudo apt install libusb-1.0-0
    - Windows: сырой доступ к USB возможен только если для камеры привязан
      WinUSB-драйвер (Zadig). НА БОЕВОЙ машине так делать нельзя: камера
      перестанет работать в обычных приложениях. Поэтому на Windows фича
      деградирует мягко — в лог уйдёт объяснение, программа продолжит работу.
      (Штатный Windows-путь к XU — через драйвер камеры: DirectShow IKsControl;
      это отдельная задача, здесь намеренно не реализуется.)

Отладка (запуск напрямую):
    python led_control.py --list         # перечислить USB-устройства Logitech
    python led_control.py --probe        # XU-юнит, selector, текущий payload
    python led_control.py --set 1        # включить LED (0/1/2)
    python led_control.py --set 2 --raw-state 0x02   # принудительный байт
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid

try:
    import usb.core
    import usb.util
    import usb.backend.libusb1  # noqa: F401 — прогреваем поиск libusb
except ImportError:  # pyusb не установлен: модуль остаётся импортируемым
    usb = None

LOGITECH_VENDOR_ID = 0x046D
C922_PRODUCT_ID = 0x085C  # Logitech C922 Pro Stream Webcam

# GUID'ы XU, в которых живёт управление LED1 (как печатают дампы дескрипторов).
LED_XU_FFE52D21 = uuid.UUID("ffe52d21-8030-4e2c-82d9-f587d00540bd")  # C920-поколение
LED_XU_63610682 = uuid.UUID("63610682-5070-49ab-b8cc-b3855e8d221f")  # старые модели

# mode (API) -> байт состояния «на проводе».
# У Logitech «авто» обычно кодируется 0x03 (2 = мигание; для него --raw-state 0x02).
# Если на вашей модели авто = 0x02 — переопределите mapping через mode_map={...}.
DEFAULT_MODE_MAP = {0: 0x00, 1: 0x01, 2: 0x03}
MODE_NAMES = {0: "выключено", 1: "включено", 2: "авто"}

# UVC class-specific запросы.
_REQ_SET = 0x21   # host->device | class | interface
_REQ_GET = 0xA1   # device->host | class | interface
_B_SET_CUR = 0x01
_B_GET_CUR = 0x81


def _to_int(value, default=None):
    """'0x082d' / 2093 / None -> int (или default)."""
    if value is None:
        return default
    try:
        return int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return default


class LogitechLEDController:
    """LED-подсветка камеры Logitech через UVC Extension Unit (pyusb).

    Пример:
        led = LogitechLEDController(logger)
        if led.connect():
            led.set_led_mode(1)       # включить подсветку
    """

    def __init__(self, logger=None, cfg=None, vendor_id=LOGITECH_VENDOR_ID,
                 product_id=None, mode_map=None):
        self.logger = logger or logging.getLogger("led")
        self.cfg = cfg or {}
        self.vendor_id = _to_int(vendor_id, LOGITECH_VENDOR_ID)
        # product_id можно задать явно или через config: led.device (напр. 0x082d)
        self.product_id = _to_int(product_id) or _to_int(self.cfg.get("device"))
        self.mode_map = dict(mode_map or DEFAULT_MODE_MAP)

        self.dev = None                  # usb.core.Device
        self.unit_id = 0x0B              # XU Unit ID (как у C920)
        self.interface = 0x00            # bInterfaceNumber VideoControl
        self.selector = 0x09             # control selector «LED1»
        self.payload_len = 5
        self.variant = "logitech_xu_v2"  # или "legacy_led1"
        self._current_raw = None         # последний прочитанный payload (GET_CUR)
        self._connected = False

    # ---------------------------------------------------------------- API --

    @property
    def available(self) -> bool:
        return self._connected

    def connect(self) -> bool:
        """Найти камеру Logitech и определить контрол LED. True при успехе."""
        if usb is None:
            self.logger.warning(
                "LED Logitech: pyusb не установлен (pip install pyusb) — "
                "управление подсветкой пропущено"
            )
            return False
        dev = self._find_camera()
        if dev is None:
            self.logger.info(
                "LED Logitech: камера Logitech не найдена — пропускаю управление подсветкой"
            )
            return False
        self.dev = dev
        self._detect_led_control()
        self._connected = True
        self.logger.info(
            f"LED Logitech: камера {dev.idVendor:04x}:{dev.idProduct:04x}, "
            f"XU unit=0x{self.unit_id:02x}, selector=0x{self.selector:02x} "
            f"(вариант {self.variant})"
        )
        return True

    def set_led_mode(self, mode: int, raw_state: int | None = None) -> bool:
        """Установить режим подсветки.

        mode: 0 — выключено, 1 — включено, 2 — авто.
        raw_state: принудительный байт «на проводе» (для отладки конкретной модели).
        """
        if not self._connected:
            return False
        if raw_state is None and mode not in self.mode_map:
            raise ValueError(
                f"mode должен быть одним из {sorted(self.mode_map)}, а не {mode!r}"
            )

        # «Авто» на разных моделях кодируется по-разному (C920/C270: 0x03,
        # отдельные прошивки: 0x02) — пробуем варианты и проверяем чтением.
        if raw_state is not None:
            candidates = [raw_state]
        elif mode == 2 and self.mode_map.get(2) == 0x03:
            candidates = [0x03, 0x02]
        else:
            candidates = [self.mode_map[mode]]

        ok = False
        for state_byte in candidates:
            payload = self._build_payload(state_byte)
            if not self._set_cur(payload):
                continue
            ok = True
            self.logger.info(
                f"LED Logitech: SET_CUR mode={mode} ({MODE_NAMES.get(mode, '?')}), "
                f"payload=[{payload.hex(' ')}]"
            )
            time.sleep(0.05)
            now = self._get_cur()
            if now is None:
                break                          # камера не читается — доверяем записи
            self._current_raw = now
            if self.payload_len == 5:
                got_byte = now[3] if len(now) >= 4 else None
            else:
                got_byte = now[0] if now else None
            if got_byte == state_byte:
                break                          # значение реально применилось
            self.logger.debug(
                f"LED Logitech: readback 0x{got_byte:02x} != 0x{state_byte:02x}, "
                "пробую следующий вариант"
            )
        return ok

    def set_led(self, state: int, raw_state: int | None = None) -> bool:
        """Алиас set_led_mode (state: 0 — выкл, 1 — вкл, 2 — авто)."""
        return self.set_led_mode(state, raw_state=raw_state)

    def get_led_mode(self):
        """Текущий режим (0/1/2) или None, если прочитать не удалось."""
        if not self._connected:
            return None
        raw = self._get_cur()
        if raw is None:
            return None
        self._current_raw = raw
        return self._decode_mode(raw)

    def apply_startup_mode(self) -> bool:
        """Стартовая установка из config.yaml (секция led) — вызов из main.py.

            led:
              enabled: true   # false — вообще не трогать подсветку
              mode: 1         # 0 — выкл, 1 — вкл, 2 — авто
              device: null    # idProduct камеры, если их несколько (напр. 0x082d)
        """
        try:
            if not bool(self.cfg.get("enabled", True)):
                self.logger.info(
                    "LED Logitech: управление подсветкой отключено в конфиге (led.enabled: false)"
                )
                return False
            if not self.connect():
                return False
            mode = _to_int(self.cfg.get("mode"), 1)
            ok = self.set_led_mode(mode)
            if ok:
                self.logger.info(
                    f"LED Logitech: режим «{MODE_NAMES.get(mode, mode)}» (mode={mode}) "
                    "успешно установлен"
                )
            else:
                self.logger.warning("LED Logitech: не удалось установить режим подсветки (не критично)")
            return ok
        except Exception as e:  # фича не критичная — программу ронять нельзя
            self.logger.warning(f"LED Logitech: ошибка управления подсветкой: {e}")
            return False

    def close(self) -> None:
        """Отпустить USB-устройство (вызывать при завершении программы)."""
        if self.dev is not None and usb is not None:
            try:
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass
        self.dev = None
        self._connected = False

    def shutdown(self) -> None:
        """Завершение программы: гасим подсветку (best-effort) и отпускаем USB."""
        if self._connected:
            try:
                self.set_led_mode(0)
            except Exception:
                pass
        self.close()

    # ------------------------------------------------------- поиск камеры --

    def _find_camera(self):
        if usb is None:
            return None
        kwargs = {"find_all": True, "idVendor": self.vendor_id}
        if self.product_id:
            kwargs["idProduct"] = self.product_id
        try:
            devices = list(usb.core.find(**kwargs))
        except usb.core.NoBackendError:
            self.logger.warning(
                "LED Logitech: libusb не найден. macOS — «brew install libusb», "
                "Linux — пакет libusb-1.0-0. Управление подсветкой пропущено"
            )
            return None
        except Exception as e:
            self.logger.warning(f"LED Logitech: ошибка поиска USB-устройств: {e}")
            return None
        cameras = [d for d in devices if self._is_video_device(d)]
        if not cameras:
            return None
        if self.product_id is None:
            # Приоритет — C922 Pro Stream (0x085c), если она среди найденных.
            for d in cameras:
                if d.idProduct == C922_PRODUCT_ID:
                    return d
        if len(cameras) > 1:
            self.logger.info(
                f"LED Logitech: найдено камер Logitech: {len(cameras)} — беру первую "
                "(можно выбрать через led.device: idProduct)"
            )
        return cameras[0]

    @staticmethod
    def _is_video_device(dev) -> bool:
        """У UVC-камер есть интерфейс класса 0x0E (VIDEO)."""
        try:
            for cfg in dev:
                for intf in cfg:
                    if intf.bInterfaceClass == 0x0E:
                        return True
        except Exception:
            pass
        return False

    # ------------------------------------------------- определение контрола --

    def _detect_led_control(self) -> None:
        """Определить XU-юнит и рабочий selector (v2: 5 байт / legacy: 1 байт)."""
        found = self._find_xu_unit()
        candidates: list[tuple[int, int]] = []
        if found is not None:
            unit, guid = found
            if unit:
                self.unit_id = unit
            if guid == LED_XU_FFE52D21:
                candidates = [(0x09, 5)]
            elif guid == LED_XU_63610682:
                candidates = [(0x01, 1)]
        if not candidates:
            # GUID не распознали — пробуем оба известных варианта.
            candidates = [(0x09, 5), (0x01, 1)]

        # Отвечает только «настоящий» selector: используем GET_CUR как пробу.
        for selector, length in candidates:
            raw = self._get_cur(selector, length)
            if raw is not None:
                self.selector, self.payload_len = selector, length
                self.variant = "logitech_xu_v2" if (selector, length) == (0x09, 5) else "legacy_led1"
                self._current_raw = raw
                return
        self.logger.info(
            "LED Logitech: GET_CUR не ответил — шлю SET_CUR вслепую "
            f"(unit=0x{self.unit_id:02x}, selector=0x{self.selector:02x})"
        )

    def _find_xu_unit(self):
        """Найти Extension Unit с LED-контролом по дескриптору конфигурации.

        Читаем сырой дескриптор конфига (GET_DESCRIPTOR) и разбираем
        VideoControl-часть: ищем CS_INTERFACE / EXTENSION_UNIT (0x06) с
        известным GUID. Возвращает (unit_id, guid) или None.
        """
        try:
            raw = bytes(self.dev.ctrl_transfer(0x80, 0x06, 0x0200, 0, 4096))
        except Exception as e:
            self.logger.debug(f"LED Logitech: не удалось прочитать дескриптор конфигурации: {e}")
            return None
        i, n = 0, len(raw)
        in_vc = False
        while i + 1 < n:
            blen, btype = raw[i], raw[i + 1]
            if blen < 2:
                break
            if btype == 0x04 and i + 7 < n:                 # INTERFACE
                in_vc = (raw[i + 5] == 0x0E and raw[i + 6] == 0x01)  # VIDEO / VideoControl
                if in_vc:
                    self.interface = raw[i + 2]             # bInterfaceNumber
            elif btype == 0x24 and in_vc and blen >= 20:    # CS_INTERFACE (VideoControl)
                if raw[i + 2] == 0x06:                      # EXTENSION_UNIT
                    unit_id = raw[i + 3]
                    guid_raw = raw[i + 4:i + 20]
                    for u in (LED_XU_FFE52D21, LED_XU_63610682):
                        # GUID в дескрипторе хранится как bytes_le (mix-endian USB).
                        if guid_raw in (u.bytes_le, u.bytes):
                            return unit_id, u
            i += blen
        return None

    # ------------------------------------------------------ UVC-трансферы --

    def _get_cur(self, selector: int | None = None, length: int | None = None):
        """GET_CUR: прочитать текущее значение контрола. None при ошибке."""
        selector = self.selector if selector is None else selector
        length = self.payload_len if length is None else length
        try:
            res = self.dev.ctrl_transfer(
                _REQ_GET, _B_GET_CUR, selector << 8,
                (self.unit_id << 8) | self.interface, length, timeout=1500,
            )
            return bytes(res)
        except Exception as e:
            self.logger.debug(f"LED Logitech: GET_CUR(sel=0x{selector:02x}) не удался: {e}")
            return None

    def _set_cur(self, payload: bytes) -> bool:
        """SET_CUR: отправить payload в XU-юнит камеры."""
        try:
            self.dev.ctrl_transfer(
                _REQ_SET, _B_SET_CUR, self.selector << 8,
                (self.unit_id << 8) | self.interface, payload, timeout=1500,
            )
            return True
        except Exception as e:
            self.logger.warning(f"LED Logitech: SET_CUR не удался: {e}")
            return False

    # ------------------------------------------------------------ payloads --

    def _build_payload(self, state_byte: int) -> bytes:
        """Собрать payload для SET_CUR, сохранив остальные поля состояния."""
        if self.payload_len == 5:
            base = bytearray(self._current_raw or b"\x03\x00\x01\x00\x01")
            if len(base) != 5:
                base = bytearray(b"\x03\x00\x01\x00\x01")
            base[3] = state_byte          # байт режима LED1 у C920-поколения
            return bytes(base)
        return bytes([state_byte])        # legacy: один байт

    def _decode_mode(self, raw: bytes):
        """payload -> режим API (0/1/2), best-effort."""
        rev = {v: k for k, v in self.mode_map.items()}
        if self.payload_len == 5:
            return rev.get(raw[3]) if len(raw) >= 4 else None
        return rev.get(raw[0]) if raw else None


# --------------------------------------------------------------------- CLI --

def _cli() -> int:
    ap = argparse.ArgumentParser(description="LED-подсветка камеры Logitech (UVC XU)")
    ap.add_argument("--list", action="store_true", help="перечислить USB-устройства Logitech")
    ap.add_argument("--probe", action="store_true", help="найти XU и показать текущий payload")
    ap.add_argument("--set", type=int, metavar="MODE", help="установить режим: 0 выкл, 1 вкл, 2 авто")
    ap.add_argument("--raw-state", type=lambda s: int(s, 0), default=None,
                    help="принудительный байт состояния, напр. 0x02")
    ap.add_argument("--device", type=lambda s: int(s, 0), default=None,
                    help="idProduct камеры, напр. 0x082d")
    ap.add_argument("--verbose", action="store_true", help="подробный лог (debug)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    log = logging.getLogger("led")

    if usb is None:
        log.error("pyusb не установлен: pip install -r requirements.txt (pyusb)")
        return 1

    if args.list:
        try:
            devices = list(usb.core.find(find_all=True, idVendor=LOGITECH_VENDOR_ID))
        except usb.core.NoBackendError:
            log.error("libusb не найден: macOS — brew install libusb, Linux — libusb-1.0-0")
            return 1
        if not devices:
            log.info("Устройства Logitech не найдены")
            return 0
        for d in devices:
            try:
                name = usb.util.get_string(d, d.iProduct) or ""
            except Exception:
                name = ""
            video = "  [video]" if LogitechLEDController._is_video_device(d) else ""
            log.info(f"  {d.idVendor:04x}:{d.idProduct:04x}  {name}{video}")
        return 0

    led = LogitechLEDController(log, {"enabled": True}, product_id=args.device)
    if not led.connect():
        return 1

    if args.set is not None:
        ok = led.set_led_mode(args.set, raw_state=args.raw_state)
        cur = led.get_led_mode()
        log.info(f"Результат: {'OK' if ok else 'НЕУДАЧА'}, режим после установки: {cur}")
        led.close()
        return 0 if ok else 1

    # по умолчанию — probe
    cur = led.get_led_mode()
    raw = led._current_raw.hex(" ") if led._current_raw else "—"
    log.info(
        f"XU unit=0x{led.unit_id:02x}, selector=0x{led.selector:02x}, "
        f"payload_len={led.payload_len}, вариант={led.variant}"
    )
    log.info(f"Текущий payload: [{raw}]")
    log.info(f"Текущий режим: {cur} ({MODE_NAMES.get(cur, '?') if cur is not None else '—'})")
    led.close()
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
