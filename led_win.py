"""led_win.py — гашение светодиода камеры Logitech на Windows **через драйвер**.

Зачем отдельный модуль
======================
На macOS/Linux UVC-команда уходит «сырым» USB-трансфером (pyusb). На Windows так
можно только после замены драйвера камеры на WinUSB — после этого камера
перестаёт работать в Zoom/браузере/нашем софте. Это неприемлемо.

Правильный путь: **DirectShow → KS-свойства (`IKsControl`)**. Мы создаём фильтр
камеры тем же драйвером, что и любое приложение, и шлём `SET_CUR` в Extension
Unit камеры. Драйвер остаётся штатным, картинка работает везде, а запись
свойства принимается даже когда поток уже открыт (в т.ч. другим приложением).

Соответствие параметров (те же, что в led_control.py):

    XU GUID : ffe52d21-8030-4e2c-82d9-f587d00540bd   (C920-поколение)
              63610682-5070-49ab-b8cc-b3855e8d221f   (старые модели)
    unit    : 0x0B (11)
    selector: 0x09
    payload : [0x03, 0x00, 0x01, MODE, 0x01]

Использование:

    import led_win
    led_win.set_led_mode(0, logger)        # погасить индикатор
    led_win.get_led_mode(logger)           # прочитать текущий режим (0/1/2/None)
    led_win.probe(logger)                  # есть ли управление на этой машине

Всё обёрнуто в try/except: любая ошибка COM/драйвера возвращает False/None и
никогда не роняет приложение.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import sys
import uuid

log_default = logging.getLogger("led.win")

# ---------------------------------------------------------------- константы --

XU_GUID_C920 = "ffe52d21-8030-4e2c-82d9-f587d00540bd"
XU_GUID_OLD = "63610682-5070-49ab-b8cc-b3855e8d221f"
XU_UNIT_ID = 0x0B
XU_SELECTOR = 0x09
PAYLOAD_LEN = 5
MODE_TO_BYTE = {0: 0x00, 1: 0x01, 2: 0x03}
BYTE_TO_MODE = {v: k for k, v in MODE_TO_BYTE.items()}

CLSID_SystemDeviceEnum = "62BE5D10-60EB-11D0-BD3B-00A0C911CE86"
IID_ICreateDevEnum = "29840822-5B84-11D0-BD3B-00A0C911CE86"
CLSID_VideoInputDeviceCategory = "860BB310-5D01-11D0-BD3B-00A0C911CE86"
IID_IBaseFilter = "56A86895-0AD4-11CE-B03A-0020AF0BA770"
IID_IKsControl = "28F54685-06FD-11D2-B27A-00A0C9223196"

KSPROPERTY_TYPE_GET = 0x00000001
KSPROPERTY_TYPE_SET = 0x00000002
CLSCTX_INPROC_SERVER = 0x1
COINIT_APARTMENTTHREADED = 0x2
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106

IS_WINDOWS = os.name == "nt"


# ------------------------------------------------------------------- структуры

class GUID(ctypes.Structure):
    # Явные c_uint32/c_uint16, а не wintypes.DWORD/WORD: на Unix c_ulong 8-байтовый,
    # и структура «поехала» бы (24 vs 40 байт). На Windows размеры те же.
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class KSPROPERTY(ctypes.Structure):
    _fields_ = [("Set", GUID), ("Id", ctypes.c_uint32), ("Flags", ctypes.c_uint32)]


class KSP_NODE(ctypes.Structure):
    _fields_ = [("Property", KSPROPERTY), ("NodeId", ctypes.c_uint32)]


def _guid(text: str) -> GUID:
    u = uuid.UUID(text)
    return GUID(u.time_low, u.time_mid, u.time_hi_version,
                (ctypes.c_ubyte * 8)(*u.bytes[8:]))


# ----------------------------------------------------------------- COM-хелперы

if IS_WINDOWS:  # на macOS/Linux WINFUNCTYPE/windll отсутствуют — модуль всё равно импортируем

    _proto_qi = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)
    )
    _proto_release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
    _proto_create_class_enum = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32
    )
    _proto_next = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32)
    )
    _proto_bind = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)
    )
    _proto_ks = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(KSPROPERTY), ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)
    )
else:  # заглушки: код ниже всё равно не вызывается (IS_WINDOWS == False)
    _proto_qi = _proto_release = _proto_create_class_enum = None
    _proto_next = _proto_bind = _proto_ks = None


def _vtable(obj: ctypes.c_void_p, index: int, proto):
    """Взять метод из vtable COM-объекта."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    func = ctypes.cast(vtbl[index], ctypes.c_void_p)
    return ctypes.cast(func, proto)


def _release(obj):
    if obj:
        try:
            _vtable(obj, 2, _proto_release)(obj)
        except Exception:
            pass


def _com_init():
    """CoInitializeEx для текущего потока (идемпотентно)."""
    if not IS_WINDOWS:
        return False
    try:
        hr = ctypes.windll.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        return hr >= 0 or hr == RPC_E_CHANGED_MODE
    except Exception:
        return False


# ------------------------------------------------------------------- backend --

class _XuControl:
    """Один video-фильтр + его IKsControl (контекстный менеджер)."""

    def __init__(self, filter_ptr, ks_ptr):
        self.filter = filter_ptr
        self.ks = ks_ptr

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        _release(self.ks)
        _release(self.filter)
        return False

    def call(self, xu_guid: str, payload: bytes, kind: int, out_len: int = 5,
             selector: int = XU_SELECTOR, unit_id: int = XU_UNIT_ID, use_node: bool = True):
        """Отправить SET_CUR/GET_CUR в XU. Возвращает (hr, bytes|None)."""
        prop = KSP_NODE() if use_node else KSPROPERTY()
        base = prop.Property if use_node else prop
        base.Set = _guid(xu_guid)
        base.Id = selector
        base.Flags = kind
        if use_node:
            prop.NodeId = unit_id
        in_buf = ctypes.create_string_buffer(payload, len(payload)) if payload else None
        out_buf = ctypes.create_string_buffer(out_len) if kind == KSPROPERTY_TYPE_GET else None
        returned = ctypes.c_uint32(0)
        data_ptr = out_buf if out_buf is not None else in_buf
        data_len = out_len if out_buf is not None else len(payload)
        hr = _vtable(self.ks, 3, _proto_ks)(
            self.ks,
            ctypes.cast(ctypes.byref(prop), ctypes.POINTER(KSPROPERTY)),
            ctypes.sizeof(prop),
            ctypes.cast(data_ptr, ctypes.c_void_p) if data_ptr is not None else None,
            data_len,
            ctypes.byref(returned),
        )
        data = out_buf.raw[:out_len] if (out_buf is not None and hr >= 0) else None
        return hr, data


def _iter_video_filters(logger):
    """Перебрать все видеоустройства: отдаёт _XuControl с IKsControl.

    IKsControl есть не у всех фильтров — такие молча пропускаем.
    """
    if not IS_WINDOWS:
        return
    ole32 = ctypes.windll.ole32
    p_enum = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(
        ctypes.byref(_guid(CLSID_SystemDeviceEnum)), None, CLSCTX_INPROC_SERVER,
        ctypes.byref(_guid(IID_ICreateDevEnum)), ctypes.byref(p_enum),
    )
    if hr < 0:
        logger.debug(f"CoCreateInstance(SystemDeviceEnum) hr=0x{hr & 0xFFFFFFFF:08x}")
        return

    p_moniker_enum = ctypes.c_void_p()
    try:
        hr = _vtable(p_enum, 3, _proto_create_class_enum)(
            p_enum, ctypes.byref(_guid(CLSID_VideoInputDeviceCategory)),
            ctypes.byref(p_moniker_enum), 0,
        )
        if hr < 0 or not p_moniker_enum:
            return
        while True:
            p_moniker = ctypes.c_void_p()
            fetched = ctypes.c_uint32(0)
            hr = _vtable(p_moniker_enum, 3, _proto_next)(
                p_moniker_enum, 1, ctypes.byref(p_moniker), ctypes.byref(fetched)
            )
            if hr < 0 or not fetched.value:
                break
            p_filter = ctypes.c_void_p()
            hr = _vtable(p_moniker, 8, _proto_bind)(
                p_moniker, None, None,
                ctypes.byref(_guid(IID_IBaseFilter)), ctypes.byref(p_filter),
            )
            _release(p_moniker)
            if hr < 0 or not p_filter:
                continue
            p_ks = ctypes.c_void_p()
            hr = _vtable(p_filter, 0, _proto_qi)(
                p_filter, ctypes.byref(_guid(IID_IKsControl)), ctypes.byref(p_ks)
            )
            if hr < 0 or not p_ks:
                _release(p_filter)
                continue
            yield _XuControl(p_filter, p_ks)
    finally:
        _release(p_moniker_enum)
        _release(p_enum)


def _try_all(payload: bytes | None, kind: int, logger, out_len: int = PAYLOAD_LEN):
    """Применить SET_CUR/GET_CUR на первом фильтре, который ответил успехом."""
    if not IS_WINDOWS:
        return None
    for xu in (XU_GUID_C920, XU_GUID_OLD):
        for use_node in (True, False):
            for ctrl in _iter_video_filters(logger):
                with ctrl:
                    try:
                        hr, data = ctrl.call(xu, payload or b"", kind, out_len=out_len,
                                             use_node=use_node)
                    except Exception as e:      # битый драйвер/фильтр — идём дальше
                        logger.debug(f"KsProperty: {e}")
                        continue
                    if hr >= 0:
                        logger.debug(
                            f"LED: ok xu={xu[:8]} node={use_node} kind={kind} hr=0x{hr:08x}"
                        )
                        return data if kind == KSPROPERTY_TYPE_GET else b""
    return None


def probe(logger=None) -> bool:
    """Есть ли на этой машине управление LED через DirectShow."""
    logger = logger or log_default
    if not IS_WINDOWS:
        return False
    if not _com_init():
        return False
    return _try_all(_payload(0x00), KSPROPERTY_TYPE_SET, logger) is not None


def set_led_mode(mode: int, logger=None) -> bool:
    """mode: 0 — выключить индикатор, 1 — включить, 2 — авто."""
    logger = logger or log_default
    if not IS_WINDOWS:
        return False
    if mode not in MODE_TO_BYTE:
        raise ValueError(f"mode должен быть 0/1/2, а не {mode!r}")
    if not _com_init():
        return False
    return _try_all(_payload(MODE_TO_BYTE[mode]), KSPROPERTY_TYPE_SET, logger) is not None


def get_led_mode(logger=None):
    """Текущий режим (0/1/2) или None."""
    logger = logger or log_default
    if not IS_WINDOWS or not _com_init():
        return None
    data = _try_all(b"", KSPROPERTY_TYPE_GET, logger)
    if not data or len(data) < 4:
        return None
    return BYTE_TO_MODE.get(data[3])


def _payload(mode_byte: int) -> bytes:
    """Payload SET_CUR: [03 00 01 MODE 01]."""
    return bytes([0x03, 0x00, 0x01, mode_byte & 0xFF, 0x01])


# ---------------------------------------------------------------- CLI (отладка)

def _cli() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="LED камеры Logitech на Windows (DirectShow/KsProperty)")
    ap.add_argument("--set", type=int, metavar="MODE", help="0 выкл, 1 вкл, 2 авто")
    ap.add_argument("--probe", action="store_true", help="проверить доступность управления")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    if not IS_WINDOWS:
        logging.info("led_win: только для Windows (на macOS/Linux используйте led_control.py)")
        return 2
    if args.set is not None:
        ok = set_led_mode(args.set)
        logging.info(f"SET mode={args.set}: {'OK' if ok else 'НЕУДАЧА'}")
        return 0 if ok else 1
    ok = probe()
    cur = get_led_mode()
    logging.info(f"Управление LED доступно: {'да' if ok else 'нет'}; текущий режим: {cur}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_cli())
