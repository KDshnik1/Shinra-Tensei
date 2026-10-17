"""Загрузка одного почтового ящика. Каждый ящик работает в своём потоке."""
import threading
import traceback
from dataclasses import dataclass, field
from datetime import date

from .imap_client import ImapConnection, ImapError, is_connection_error
from . import msgfile
from .storage import MailboxArchive
from .i18n import tr

MAX_AUTO_RECONNECTS = 3
BATCH_COUNT = 100              # писем в одной команде FETCH
BATCH_BYTES = 16 * 1024 * 1024 # и не больше стольких байт (письма держатся в памяти)
UNKNOWN_SIZE = 1024 * 1024     # размер, если сервер его не назвал


def make_batches(uids, sizes):
    """Режет список UID на пачки по числу писем и суммарному размеру. Большое письмо
    уходит отдельной пачкой."""
    batch, total = [], 0
    for u in uids:
        z = sizes.get(u, UNKNOWN_SIZE)
        if batch and (len(batch) >= BATCH_COUNT or total + z > BATCH_BYTES):
            yield batch
            batch, total = [], 0
        batch.append(u)
        total += z
    if batch:
        yield batch


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
        self.batches = True            # качать пачками, пока сервер не откажет
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
            self.cb.log(tr('Переподключение (попытка {attempt}/{MAX_AUTO_RECONNECTS})…').format(attempt=attempt, MAX_AUTO_RECONNECTS=MAX_AUTO_RECONNECTS))
            try:
                self._connect()
                if self.current_folder:
                    self.conn.select(self.current_folder)
                self.cb.log(tr('Соединение восстановлено'))
                return True
            except Exception as e:
                self.cb.log(tr('Не удалось переподключиться: {e}').format(e=e))
                if self.stop.wait(min(5 * attempt, 15)):
                    raise StopRequested()
        return False

    def _with_reconnect(self, fn):
        try:
            return fn()
        except Exception as e:
            if not is_connection_error(e):
                raise
            self.cb.log(tr('Обрыв соединения: {e}').format(e=e))
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
            self.cb.status(tr('В очереди'))
            while not self.sem.acquire(timeout=0.3):
                self._check_stop()
            acquired = True
            self._run()
            if self.errors:
                self.cb.finished("warn", tr('Завершено с ошибками: загружено {0}, пропущено из-за ошибок {1}').format(self.downloaded, self.errors))
            else:
                self.cb.finished("ok", tr('Готово: загружено {0}, уже было в архиве {1}').format(self.downloaded, self.already))
        except StopRequested:
            self.cb.finished("stopped", tr('Остановлено пользователем'))
        except Exception as e:
            self.cb.log(tr('Критическая ошибка:\n') + traceback.format_exc())
            self.cb.finished("error", tr('Ошибка: {e}').format(e=e))
        finally:
            if self.conn:
                self.conn.logout()
            if acquired:
                self.sem.release()

    def _run(self):
        a = self.acc
        self.cb.status(tr('Подключение'))
        self.cb.log(tr('Подключение к {0}:{1} ({2})').format(a.host, a.port, a.security_text()))
        self._connect()
        self.cb.log(tr('Вход выполнен'))

        folders = [f for f in self.conn.list_folders() if f.selectable]
        if a.folder_mode == "one":
            folders = [f for f in folders if f.raw == a.folder_raw]
            if not folders:
                raise ImapError(tr('Папка «{0}» не найдена на сервере').format(a.folder_display or a.folder_raw))

        dfrom = _d(a.date_from) if a.date_mode == "range" else None
        dto = _d(a.date_to) if a.date_mode == "range" else None

        # 1. Анализ: сколько писем и какие ещё не скачаны
        self.cb.status(tr('Анализ папок'))
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
                self.cb.log(tr('Папка «{0}» пропущена: {e}').format(f.display, e=e))
                continue
            fdir = self.archive.folder_dir(f)
            have = msgfile.existing_keys(fdir)
            todo = [u for u in uids if MailboxArchive.message_key(uv, u) not in have]
            self.already += len(uids) - len(todo)
            self.cb.log(tr('«{0}»: писем {1}, новых {2}').format(f.display, len(uids), len(todo)))
            if todo:
                plan.append((f, uv, fdir, todo))
        self._counters()

        # 2. Загрузка
        total = sum(len(p[3]) for p in plan)
        done = 0
        self.cb.progress(0, total)
        for f, uv, fdir, todo in plan:
            self._check_stop()
            self.cb.status(tr('Загрузка: {0}').format(f.display))

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
                self.cb.log(tr('Папка «{0}» пропущена: {e}').format(f.display, e=e))
                done += len(todo)
                self.cb.progress(done, total)
                continue
            if cur_uv != uv:
                self.cb.log(tr('«{0}»: сервер сменил UIDVALIDITY, папка будет перекачана при следующем запуске').format(f.display))
                done += len(todo)
                self.cb.progress(done, total)
                continue
            try:
                sizes = self._with_reconnect(lambda: self.conn.fetch_sizes(todo))
            except StopRequested:
                raise
            except Exception as e:
                self.cb.log(tr('«{0}»: сервер не сообщил размеры писем ({e}), качаю небольшими пачками').format(f.display, e=e))
                sizes = {}
            for batch in make_batches(todo, sizes):
                self._check_stop()
                for _uid in self._download_batch(f, uv, fdir, batch):
                    done += 1
                    self.cb.progress(done, total)

    def _download_batch(self, folder, uv, fdir, batch):
        """Качает пачку одной командой; после каждого письма отдаёт его UID. Письма, которые
        не пришли в ответе или не сохранились, докачиваются по одному — с переподключением
        и вопросом пользователю, как раньше."""
        got = {}
        if len(batch) > 1 and self.batches:
            try:
                got = self._with_reconnect(lambda: self.conn.fetch_many(batch))
            except StopRequested:
                raise
            except Exception as e:
                if not is_connection_error(e):
                    # сервер не понимает FETCH нескольких писем — дальше по одному
                    self.batches = False
                    self.cb.log(tr('Сервер не выдаёт письма пачками ({e}), качаю по одному').format(e=e))
        for uid in batch:
            item = got.pop(uid, None)
            saved = False
            if item is not None:
                try:
                    self.archive.save_message(fdir, uv, uid, *item, self.compress)
                    saved = True
                except Exception:
                    pass
            if saved:
                self.downloaded += 1
                self._counters()
            else:
                self._download_one(folder, uv, fdir, uid)
            yield uid

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
                    self.cb.log(tr('UID {uid}: обрыв соединения ({e})').format(uid=uid, e=e))
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
                self.cb.log(tr('Письмо UID {uid} в «{0}» пропущено: {e}').format(folder.display, uid=uid, e=e))
                return
