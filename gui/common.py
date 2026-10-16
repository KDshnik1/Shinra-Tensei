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


# ---------- форматирование прогресса ----------
def fmt_num(n: int) -> str:
    """1234567 -> «1 234 567» (неразрывные пробелы)."""
    return f"{n:,}".replace(",", " ")


def percent(done: int, total: int) -> int:
    if total <= 0:
        return 0
    if done >= total:
        return 100
    return done * 100 // total            # 100% только когда действительно всё


def progress_text(done: int, total: int) -> str:
    if total <= 0:
        return ""
    return f"{fmt_num(done)} / {fmt_num(total)} ({percent(done, total)}%)"


def set_progress(bar, done: int, total: int, prefix: str = ""):
    """Заполнить QProgressBar: значение + текст «[prefix]done / total (NN%)»."""
    bar.setRange(0, max(total, 1))
    bar.setValue(min(done, total) if total > 0 else 0)
    text = progress_text(done, total)
    bar.setFormat(f"{prefix}{text}" if text else prefix.rstrip(": "))
