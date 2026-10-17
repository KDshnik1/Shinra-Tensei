"""Питание Windows во время выкачки и экспорта.

Пока идёт хотя бы одна задача:
  * если пользователь включил галочку — Windows не уходит в сон по бездействию
    (PowerSetRequest; видно в `powercfg /requests` с причиной). Монитор при этом гаснуть может,
    а закрытие крышки или «Пуск → Спящий режим» по-прежнему усыпляют компьютер;
  * всегда — процесс не замедляется режимом экономии Windows 11 (EcoQoS), когда окно свёрнуто.
Когда задач нет, всё возвращается как было. На других системах модуль ничего не делает.
"""
import sys
import threading

IS_WIN = sys.platform == "win32"

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _REASON_CONTEXT(ctypes.Structure):
        _fields_ = [("Version", wintypes.ULONG), ("Flags", wintypes.DWORD),
                    ("SimpleReasonString", wintypes.LPWSTR)]

    class _THROTTLING_STATE(ctypes.Structure):
        _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                    ("StateMask", wintypes.ULONG)]

    _k32.PowerCreateRequest.argtypes = [ctypes.POINTER(_REASON_CONTEXT)]
    _k32.PowerCreateRequest.restype = wintypes.HANDLE
    _k32.PowerSetRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
    _k32.PowerSetRequest.restype = wintypes.BOOL
    _k32.PowerClearRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
    _k32.PowerClearRequest.restype = wintypes.BOOL
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
    _k32.SetThreadExecutionState.restype = wintypes.DWORD

    _POWER_REQUEST_CONTEXT_VERSION = 0
    _POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x1
    _PowerRequestSystemRequired = 1
    _ProcessPowerThrottling = 4
    _PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
    _ES_CONTINUOUS, _ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    _INVALID_HANDLE = wintypes.HANDLE(-1).value


class PowerKeeper:
    """Счётчик задач: hold(key) при старте задачи, release(key) при окончании."""

    def __init__(self, reason: str = "Shinra Tensei"):
        self._lock = threading.Lock()
        self._keys = set()
        self._reason = reason
        self.keep_awake = True       # галочка «не давать Windows уходить в сон»
        self._req = None             # дескриптор запроса питания
        self._awake = False          # запрос сейчас установлен
        self._unthrottled = False

    def set_keep_awake(self, on: bool):
        with self._lock:
            self.keep_awake = bool(on)
            self._apply()

    def hold(self, key):
        with self._lock:
            self._keys.add(key)
            self._apply()

    def release(self, key):
        with self._lock:
            self._keys.discard(key)
            self._apply()

    @property
    def busy(self) -> bool:
        return bool(self._keys)

    @property
    def awake(self) -> bool:
        return self._awake

    # ---------- Windows ----------
    def _apply(self):
        if not IS_WIN:
            return
        busy = bool(self._keys)
        try:
            self._set_awake(busy and self.keep_awake)
        except Exception:
            pass
        try:
            self._set_throttling(not busy)
        except Exception:
            pass

    def _set_awake(self, on: bool):
        if on == self._awake:
            return
        if self._req is None and on:
            ctx = _REASON_CONTEXT(_POWER_REQUEST_CONTEXT_VERSION, _POWER_REQUEST_CONTEXT_SIMPLE_STRING,
                                  self._reason)
            h = _k32.PowerCreateRequest(ctypes.byref(ctx))
            self._req = h if h and h != _INVALID_HANDLE else 0
        if self._req:
            ok = (_k32.PowerSetRequest if on else _k32.PowerClearRequest)(
                self._req, _PowerRequestSystemRequired)
        else:
            # запасной путь (не должен понадобиться на Windows 7+): флаг действует для того
            # потока, который его поставил, поэтому hold/release зовутся из потока окна
            ok = _k32.SetThreadExecutionState(
                _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if on else 0)) != 0
        if ok:
            self._awake = on

    def _set_throttling(self, allowed: bool):
        """allowed=False — запретить EcoQoS; True — вернуть решение системе."""
        if (not allowed) == self._unthrottled:
            return
        st = _THROTTLING_STATE(1, 0 if allowed else _PROCESS_POWER_THROTTLING_EXECUTION_SPEED, 0)
        fn = getattr(_k32, "SetProcessInformation", None)      # нет на Windows 7
        if fn is None:
            return
        fn.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        fn.restype = wintypes.BOOL
        if fn(_k32.GetCurrentProcess(), _ProcessPowerThrottling, ctypes.addressof(st), ctypes.sizeof(st)):
            self._unthrottled = not allowed


keeper = PowerKeeper()
