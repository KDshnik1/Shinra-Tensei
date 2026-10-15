"""Фоновые задачи для интерфейса."""
from PySide6.QtCore import QThread, Signal

_alive = set()   # держим ссылки на работающие потоки, чтобы их не удалил GC


class FuncThread(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        _alive.add(self)
        self.finished.connect(lambda: _alive.discard(self))

    def run(self):
        try:
            self.done.emit(self.fn())
        except Exception as e:
            self.failed.emit(str(e) or type(e).__name__)


def run_bg(fn, on_done, on_fail):
    t = FuncThread(fn)
    t.done.connect(on_done)
    t.failed.connect(on_fail)
    t.start()
    return t
