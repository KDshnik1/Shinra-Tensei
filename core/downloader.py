"""Загрузка одного почтового ящика. Каждый ящик работает в своём потоке."""
import threading
import traceback
from dataclasses import dataclass, field
from datetime import date

from .imap_client import ImapConnection, ImapError, is_connection_error
from . import msgfile
from .storage import MailboxArchive

MAX_AUTO_RECONNECTS = 3


class StopRequested(Exception):
    pass


@dataclass
class ErrorQuestion:
    """Вопрос пользователю «пропустить письмо?». Поток ждёт ответа на event."""
    account_id: str
    email: str
    folder: str
    uid: str
    error: str
    details: str
    event: threading.Event = field(default_factory=threading.Event)
    answer: str = "skip"            # retry | skip | skip_all | stop
    cancelled: bool = False


def _d(s: str):
    return date.fromisoformat(s) if s else None


class Downloader:
    """cb — объект с методами status, progress, log, counters, ask, finished."""

    def __init__(self, account, archive_root, cb, stop_event, semaphore, compress=False):
        self.acc = account
        self.compress = compress
        self.cb = cb
        self.stop = stop_event
        self.sem = semaphore
        self.archive = MailboxArchive(archive_root, account.email)
        self.conn = None
        self.current_folder = None
        self.skip_all = False
        self.downloaded = self.already = self.errors = 0

    # ---------- служебное ----------
    def _check_stop(self):
        if self.stop.is_set():
            raise StopRequested()

    def _counters(self):
        self.cb.counters(self.downloaded, self.already, self.errors)

    def _connect(self):
        if self.conn:
            self.conn.logout()
        self.conn = ImapConnection(self.acc)
        self.conn.connect()

    def _reconnect(self) -> bool:
        for attempt in range(1, MAX_AUTO_RECONNECTS + 1):
            self._check_stop()
            self.cb.log(f"Переподключение (попытка {attempt}/{MAX_AUTO_RECONNECTS})…")
            try:
                self._connect()
                if self.current_folder:
                    self.conn.select(self.current_folder)
                self.cb.log("Соединение восстановлено")
                return True
            except Exception as e:
                self.cb.log(f"Не удалось переподключиться: {e}")
                if self.stop.wait(min(5 * attempt, 15)):
                    raise StopRequested()
        return False

    def _with_reconnect(self, fn):
        try:
            return fn()
        except Exception as e:
            if not is_connection_error(e):
                raise
            self.cb.log(f"Обрыв соединения: {e}")
            if not self._reconnect():
                raise
            return fn()

    def _ask(self, folder, uid, exc, details) -> str:
        q = ErrorQuestion(self.acc.id, self.acc.email, folder.display, uid,
                          f"{type(exc).__name__}: {exc}", details)
        self.cb.ask(q)
        while not q.event.wait(0.3):
            if self.stop.is_set():
                q.cancelled = True
                return "stop"
        return q.answer

    # ---------- основной цикл ----------
    def run(self):
        acquired = False
        try:
            self.cb.status("В очереди")
            while not self.sem.acquire(timeout=0.3):
                self._check_stop()
            acquired = True
            self._run()
            if self.errors:
                self.cb.finished("warn", f"Завершено с ошибками: загружено {self.downloaded}, "
                                         f"пропущено из-за ошибок {self.errors}")
            else:
                self.cb.finished("ok", f"Готово: загружено {self.downloaded}, "
                                       f"уже было в архиве {self.already}")
        except StopRequested:
            self.cb.finished("stopped", "Остановлено пользователем")
        except Exception as e:
            self.cb.log("Критическая ошибка:\n" + traceback.format_exc())
            self.cb.finished("error", f"Ошибка: {e}")
        finally:
            if self.conn:
                self.conn.logout()
            if acquired:
                self.sem.release()

    def _run(self):
        a = self.acc
        self.cb.status("Подключение")
        self.cb.log(f"Подключение к {a.host}:{a.port} ({a.security_text()})")
        self._connect()
        self.cb.log("Вход выполнен")

        folders = [f for f in self.conn.list_folders() if f.selectable]
        if a.folder_mode == "one":
            folders = [f for f in folders if f.raw == a.folder_raw]
            if not folders:
                raise ImapError(f"Папка «{a.folder_display or a.folder_raw}» не найдена на сервере")

        dfrom = _d(a.date_from) if a.date_mode == "range" else None
        dto = _d(a.date_to) if a.date_mode == "range" else None

        # 1. Анализ: сколько писем и какие ещё не скачаны
        self.cb.status("Анализ папок")
        plan = []
        for f in folders:
            self._check_stop()

            def scan(f=f):
                self.current_folder = f
                uv = self.conn.select(f)
                return uv, self.conn.search(dfrom, dto)
            try:
                uv, uids = self._with_reconnect(scan)
            except StopRequested:
                raise
            except Exception as e:
                self.errors += 1
                self._counters()
                self.cb.log(f"Папка «{f.display}» пропущена: {e}")
                continue
            fdir = self.archive.folder_dir(f)
            have = msgfile.existing_keys(fdir)
            todo = [u for u in uids if MailboxArchive.message_key(uv, u) not in have]
            self.already += len(uids) - len(todo)
            self.cb.log(f"«{f.display}»: писем {len(uids)}, новых {len(todo)}")
            if todo:
                plan.append((f, uv, fdir, todo))
        self._counters()

        # 2. Загрузка
        total = sum(len(p[3]) for p in plan)
        done = 0
        self.cb.progress(0, total)
        for f, uv, fdir, todo in plan:
            self._check_stop()
            self.cb.status(f"Загрузка: {f.display}")

            def open_folder(f=f):
                self.current_folder = f
                return self.conn.select(f)
            try:
                cur_uv = self._with_reconnect(open_folder)
            except StopRequested:
                raise
            except Exception as e:
                self.errors += 1
                self._counters()
                self.cb.log(f"Папка «{f.display}» пропущена: {e}")
                done += len(todo)
                self.cb.progress(done, total)
                continue
            if cur_uv != uv:
                self.cb.log(f"«{f.display}»: сервер сменил UIDVALIDITY, папка будет "
                            f"перекачана при следующем запуске")
                done += len(todo)
                self.cb.progress(done, total)
                continue
            for uid in todo:
                self._check_stop()
                self._download_one(f, uv, fdir, uid)
                done += 1
                self.cb.progress(done, total)

    def _download_one(self, folder, uv, fdir, uid):
        auto = 0
        while True:
            self._check_stop()
            try:
                body, flags, idate = self.conn.fetch(uid)
                self.archive.save_message(fdir, uv, uid, body, flags, idate, self.compress)
                self.downloaded += 1
                self._counters()
                return
            except StopRequested:
                raise
            except Exception as e:
                conn_err = is_connection_error(e)
                if conn_err and auto < MAX_AUTO_RECONNECTS:
                    auto += 1
                    self.cb.log(f"UID {uid}: обрыв соединения ({e})")
                    if self._reconnect():
                        continue
                if self.skip_all:
                    answer = "skip"
                else:
                    answer = self._ask(folder, uid, e, traceback.format_exc())
                if answer == "stop":
                    raise StopRequested()
                if answer == "retry":
                    auto = 0
                    if conn_err:
                        self._reconnect()
                    continue
                if answer == "skip_all":
                    self.skip_all = True
                self.errors += 1
                self._counters()
                self.cb.log(f"Письмо UID {uid} в «{folder.display}» пропущено: {e}")
                return
