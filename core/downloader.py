"""Загрузка одного почтового ящика. Каждый ящик работает в своём потоке."""
import threading
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import date

from .imap_client import ImapConnection, ImapError, is_connection_error
from . import mailparse, msgfile
from .storage import MailboxArchive, is_all_mail
from .i18n import tr

MAX_AUTO_RECONNECTS = 3
BATCH_COUNT = 100              # писем в одной команде FETCH
BATCH_BYTES = 16 * 1024 * 1024 # и не больше стольких байт (письма держатся в памяти)
UNKNOWN_SIZE = 1024 * 1024     # размер, если сервер его не назвал
MAX_CONNECTIONS = 10           # соединений на один ящик, не больше


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


class _Conn:
    """Одно соединение с сервером и папка, открытая в нём. У ящика их может быть несколько."""

    def __init__(self, dl, n: int):
        self.dl = dl
        self.n = n                     # номер соединения (для журнала)
        self.conn = None
        self.folder = None

    def connect(self):
        if self.conn:
            self.conn.logout()
        self.conn = ImapConnection(self.dl.acc)
        self.conn.connect()

    def close(self):
        if self.conn:
            self.conn.logout()
            self.conn = None

    def log(self, text):
        self.dl.cb.log(text if self.n == 1 else f"[{self.n}] {text}")

    def reconnect(self) -> bool:
        for attempt in range(1, MAX_AUTO_RECONNECTS + 1):
            self.dl._check_stop()
            self.log(tr('Переподключение (попытка {attempt}/{MAX_AUTO_RECONNECTS})…').format(attempt=attempt, MAX_AUTO_RECONNECTS=MAX_AUTO_RECONNECTS))
            try:
                self.connect()
                if self.folder:
                    self.conn.select(self.folder)
                self.log(tr('Соединение восстановлено'))
                return True
            except Exception as e:
                self.log(tr('Не удалось переподключиться: {e}').format(e=e))
                if self.dl.stop.wait(min(5 * attempt, 15)):
                    raise StopRequested()
        return False

    def with_reconnect(self, fn):
        try:
            return fn()
        except Exception as e:
            if not is_connection_error(e):
                raise
            self.log(tr('Обрыв соединения: {e}').format(e=e))
            if not self.reconnect():
                raise
            return fn()

    def open(self, folder) -> str:
        """SELECT папки (с переподключением); возвращает UIDVALIDITY."""
        def sel():
            self.folder = folder
            return self.conn.select(folder)
        return self.with_reconnect(sel)


class Downloader:
    """cb — объект с методами status, progress, log, counters, ask, finished.

    Письма качаются пачками; при account.connections > 1 пачки раздаются нескольким
    соединениям с сервером (каждое — в своём потоке), очередь общая."""

    def __init__(self, account, archive_root, cb, stop_event, semaphore, compress=False):
        self.acc = account
        self.compress = compress
        self.cb = cb
        self.stop = stop_event
        self.sem = semaphore
        self.archive = MailboxArchive(archive_root, account.email)
        self.main = _Conn(self, 1)
        self.lock = threading.Lock()   # счётчики, ключи повторов, очередь — общие для соединений
        self.cv = threading.Condition(self.lock)
        self.pending = 0               # пачек обычных папок не закончено («Вся почта» ждёт их)
        self.skip_all = False
        self.batches = True            # качать пачками, пока сервер не откажет
        self.downloaded = self.already = self.errors = self.duplicates = 0
        self.done = self.total = 0
        self.known = set()             # (Message-ID, размер) писем, которые уже есть или качаются
        self._uv_logged = set()

    # ---------- служебное ----------
    def _check_stop(self):
        if self.stop.is_set():
            raise StopRequested()

    def _counters(self):
        self.cb.counters(self.downloaded, self.already, self.errors)

    def _add(self, downloaded=0, errors=0, done=0):
        with self.lock:
            self.downloaded += downloaded
            self.errors += errors
            self.done += done
            counts = (self.downloaded, self.already, self.errors)
            progress = (self.done, self.total)
        if downloaded or errors:
            self.cb.counters(*counts)
        if done:
            self.cb.progress(*progress)

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
            self.main.close()
            if acquired:
                self.sem.release()

    def _run(self):
        a = self.acc
        m = self.main
        self.cb.status(tr('Подключение'))
        self.cb.log(tr('Подключение к {0}:{1} ({2})').format(a.host, a.port, a.security_text()))
        m.connect()
        self.cb.log(tr('Вход выполнен'))

        folders = [f for f in m.conn.list_folders() if f.selectable]
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
            try:
                uv = m.open(f)
                uids = m.with_reconnect(lambda: m.conn.search(dfrom, dto))
            except StopRequested:
                raise
            except Exception as e:
                self._add(errors=1)
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

        # 2. Пачки писем. «Вся почта» Gmail — последней: её письма почти все есть в других
        # папках, и с пропуском повторов они остаются там, где лежат по ярлыкам.
        plan.sort(key=lambda p: is_all_mail(p[0]))
        if a.skip_duplicates and plan:
            self.cb.status(tr('Поиск повторов в архиве'))
            self.known = self._archive_keys()
        self.total = sum(len(p[3]) for p in plan)
        self.cb.progress(0, self.total)
        jobs = []
        for f, uv, fdir, todo in plan:
            self._check_stop()
            self.cb.status(tr('Подготовка: {0}').format(f.display))
            try:
                cur_uv = m.open(f)
            except StopRequested:
                raise
            except Exception as e:
                self.cb.log(tr('Папка «{0}» пропущена: {e}').format(f.display, e=e))
                self._add(errors=1, done=len(todo))
                continue
            if cur_uv != uv:
                self.cb.log(tr('«{0}»: сервер сменил UIDVALIDITY, папка будет перекачана при следующем запуске').format(f.display))
                self._add(done=len(todo))
                continue
            meta, sizes = self._meta_and_sizes(f, todo)
            if meta:
                dups = [u for u in todo if self._key(meta.get(u)) in self.known]
                if dups:
                    skip = set(dups)
                    todo = [u for u in todo if u not in skip]
                    self.duplicates += len(dups)
                    self._add(done=len(dups))
                    self.cb.log(tr('«{0}»: {1} писем уже есть в других папках — не загружаются').format(f.display, len(dups)))
            last = bool(meta) and is_all_mail(f)
            for batch in make_batches(todo, sizes):
                jobs.append({"folder": f, "uv": uv, "fdir": fdir, "batch": batch, "meta": meta,
                             "done": set(), "last": last})

        # 3. Загрузка — одним или несколькими соединениями
        if jobs:
            self._download(jobs)
        if self.duplicates:
            self.cb.log(tr('Повторов писем не загружено: {0}').format(self.duplicates))

    def _meta_and_sizes(self, f, todo):
        """(Message-ID и размеры — для поиска повторов, размеры — для пачек)."""
        m = self.main
        meta, sizes = {}, None
        if self.acc.skip_duplicates:
            try:
                meta = m.with_reconnect(lambda: m.conn.fetch_meta(todo))
                sizes = {u: x[0] for u, x in meta.items()}
            except StopRequested:
                raise
            except Exception as e:
                self.cb.log(tr('«{0}»: сервер не выдал Message-ID писем ({e}), повторы в этой папке не ищутся').format(f.display, e=e))
        if sizes is None:
            try:
                sizes = m.with_reconnect(lambda: m.conn.fetch_sizes(todo))
            except StopRequested:
                raise
            except Exception as e:
                self.cb.log(tr('«{0}»: сервер не сообщил размеры писем ({e}), качаю небольшими пачками').format(f.display, e=e))
                sizes = {}
        return meta, sizes

    @staticmethod
    def _key(m):
        """(Message-ID, размер) — ключ повтора; у письма без Message-ID ключа нет."""
        return (m[1], m[0]) if m and m[1] else None

    def _archive_keys(self) -> set:
        """Ключи писем, уже скачанных в архив ящика (по индексу просмотрщика)."""
        from .mail_index import load_folders, mailbox_folders
        try:
            fs = mailbox_folders(self.archive.dir)
            rows = load_folders([(f["dir"], f["display"]) for f in fs],
                                stop=self.stop.is_set)
        except Exception as e:
            self.cb.log(tr('Не удалось прочитать архив для поиска повторов: {e}').format(e=e))
            return set()
        self._check_stop()
        return {(mailparse.msgid_key(r.get("msgid")), r.get("size"))
                for r in rows if mailparse.msgid_key(r.get("msgid"))}

    # ---------- раздача пачек соединениям ----------
    def _download(self, jobs):
        n = max(1, min(int(self.acc.connections or 1), MAX_CONNECTIONS, len(jobs)))
        queue = deque(jobs)
        self.pending = sum(1 for j in jobs if not j["last"])
        conns = [self.main] + [_Conn(self, i) for i in range(2, n + 1)]
        failures = []
        if n > 1:
            self.cb.log(tr('Загрузка в {0} соединениях').format(n))

        def worker(c):
            try:
                if c is not self.main:
                    try:
                        c.connect()
                    except Exception as e:
                        if not self.stop.is_set():
                            self.cb.log(tr('Соединение {0} не открылось ({e}) — работают остальные').format(c.n, e=e))
                        return
                self._work(c, queue)
            except StopRequested:
                pass
            except Exception as e:
                failures.append(e)
                if c.n > 1 or n > 1:
                    self.cb.log(tr('Соединение {0} закрыто из-за ошибки: {e}').format(c.n, e=e))
            finally:
                if c is not self.main:
                    c.close()

        if n == 1:
            worker(self.main)
        else:
            threads = [threading.Thread(target=worker, args=(c,), daemon=True,
                                        name=f"dl-{self.acc.email}-{c.n}") for c in conns]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self._check_stop()
        if queue:                       # пачки остались: все соединения выбыли с ошибкой
            raise failures[-1] if failures else ImapError(tr('Нет соединения с сервером'))

    def _next_job(self, queue):
        """Следующая пачка. Пачки «Вся почта» (при пропуске повторов) ждут, пока докачаются
        обычные папки: письмо должно остаться в папке по ярлыку, а не во «Вся почта»."""
        with self.cv:
            while True:
                self._check_stop()
                if not queue:
                    return None
                if queue[0]["last"] and self.pending:
                    self.cv.wait(0.3)
                    continue
                return queue.popleft()

    def _work(self, c, queue):
        while (job := self._next_job(queue)) is not None:
            try:
                self._run_job(c, job)
            except StopRequested:
                raise
            except BaseException:
                # соединение выбывает, пачку доделают другие (уже скачанное не повторяется)
                with self.cv:
                    rest = [u for u in job["batch"] if u not in job["done"]]
                    for u in rest:
                        self._release(job, u)
                    job["batch"] = rest
                    queue.appendleft(job)
                    self.cv.notify_all()
                raise
            if not job["last"]:
                with self.cv:
                    self.pending -= 1
                    self.cv.notify_all()

    def _release(self, job, uid):
        """Письмо не скачалось — его копию в другой папке можно качать."""
        if job.get("claimed") and uid in job["claimed"]:
            self.known.discard(job["claimed"].pop(uid))

    def _run_job(self, c, job):
        f, uv = job["folder"], job["uv"]
        if c.folder is not f:
            self.cb.status(tr('Загрузка: {0}').format(f.display))
            cur_uv = c.open(f)
            if cur_uv != uv:
                with self.lock:
                    first = f.raw not in self._uv_logged
                    self._uv_logged.add(f.raw)
                if first:
                    c.log(tr('«{0}»: сервер сменил UIDVALIDITY, папка будет перекачана при следующем запуске').format(f.display))
                rest = [u for u in job["batch"] if u not in job["done"]]
                job["done"].update(rest)
                self._add(done=len(rest))
                return
        # повторы среди писем этого запуска: ключ занимает первое взявшееся соединение
        batch, dups = [], 0
        with self.lock:
            claimed = job.setdefault("claimed", {})
            for u in job["batch"]:
                if u in job["done"]:
                    continue
                k = self._key(job["meta"].get(u))
                if k is not None and k in self.known:
                    job["done"].add(u)
                    dups += 1
                    continue
                if k is not None:
                    self.known.add(k)
                    claimed[u] = k
                batch.append(u)
            self.duplicates += dups
        if dups:
            self._add(done=dups)
        for uid, saved in self._download_batch(c, f, uv, job["fdir"], batch):
            with self.lock:
                job["done"].add(uid)
                if not saved:
                    self._release(job, uid)
            self._add(done=1)

    def _download_batch(self, c, folder, uv, fdir, batch):
        """Качает пачку одной командой; после каждого письма отдаёт (UID, сохранено). Письма, которые
        не пришли в ответе или не сохранились, докачиваются по одному — с переподключением
        и вопросом пользователю, как раньше."""
        got = {}
        if len(batch) > 1 and self.batches:
            try:
                got = c.with_reconnect(lambda: c.conn.fetch_many(batch))
            except StopRequested:
                raise
            except Exception as e:
                if not is_connection_error(e) and self.batches:
                    # сервер не понимает FETCH нескольких писем — дальше по одному
                    self.batches = False
                    c.log(tr('Сервер не выдаёт письма пачками ({e}), качаю по одному').format(e=e))
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
                self._add(downloaded=1)
            else:
                saved = self._download_one(c, folder, uv, fdir, uid)
            yield uid, saved

    def _download_one(self, c, folder, uv, fdir, uid):
        auto = 0
        while True:
            self._check_stop()
            try:
                body, flags, idate = c.conn.fetch(uid)
                self.archive.save_message(fdir, uv, uid, body, flags, idate, self.compress)
                self._add(downloaded=1)
                return True
            except StopRequested:
                raise
            except Exception as e:
                conn_err = is_connection_error(e)
                if conn_err and auto < MAX_AUTO_RECONNECTS:
                    auto += 1
                    c.log(tr('UID {uid}: обрыв соединения ({e})').format(uid=uid, e=e))
                    if c.reconnect():
                        continue
                if self.skip_all:
                    answer = "skip"
                else:
                    answer = self._ask(folder, uid, e, traceback.format_exc())
                if answer == "stop":
                    self.stop.set()            # «Остановить ящик» — для всех его соединений
                    raise StopRequested()
                if answer == "retry":
                    auto = 0
                    if conn_err:
                        c.reconnect()
                    continue
                if answer == "skip_all":
                    self.skip_all = True
                self._add(errors=1)
                c.log(tr('Письмо UID {uid} в «{0}» пропущено: {e}').format(folder.display, uid=uid, e=e))
                return False
