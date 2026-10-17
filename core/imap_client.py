"""Работа с IMAP-сервером: подключение, список папок, поиск и загрузка писем."""
import calendar
import imaplib
import re
import ssl
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import imap_utf7
from .mailparse import msgid_key
from .i18n import tr

# Некоторые серверы присылают очень длинные строки — снимаем ограничение imaplib.
imaplib._MAXLINE = 100_000_000

TIMEOUT = 90
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class ImapError(Exception):
    pass


def is_connection_error(e: BaseException) -> bool:
    """Обрыв связи / таймаут — такие ошибки лечатся переподключением."""
    return isinstance(e, (imaplib.IMAP4.abort, OSError, EOFError))


def imap_date(d: date) -> str:
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


def quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return re.sub(r"\\(.)", r"\1", s[1:-1])
    return s


def make_ssl_context(accept_all: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if accept_all:
        # Принимаем любые сертификаты: просроченные, самоподписанные, чужое имя хоста.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        # И старые серверы со слабыми шифрами / TLS 1.0.
        try:
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        except ssl.SSLError:
            pass
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1
        except (ValueError, AttributeError):
            pass
    return ctx


@dataclass
class Folder:
    raw: str                    # имя на сервере (mUTF-7)
    display: str                # человекочитаемое имя
    delimiter: str = "/"
    flags: list = field(default_factory=list)

    @property
    def selectable(self) -> bool:
        low = [f.lower() for f in self.flags]
        return "\\noselect" not in low and "\\nonexistent" not in low


_LIST_RE = re.compile(
    r'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.*)$', re.I)


def parse_list_response(data) -> list[Folder]:
    folders = []
    for item in data:
        if not item:
            continue
        if isinstance(item, tuple):          # имя пришло литералом {N}
            head = item[0].decode("utf-8", "replace")
            name = item[1].decode("utf-8", "replace")
            m = _LIST_RE.match(head)
            if not m:
                continue
        else:
            line = item.decode("utf-8", "replace")
            m = _LIST_RE.match(line)
            if not m:
                continue
            name = _unquote(m.group("name"))
        delim = m.group("delim")
        delim = "" if delim.upper() == "NIL" else _unquote(delim)
        flags = m.group("flags").split()
        folders.append(Folder(raw=name, display=imap_utf7.decode(name),
                              delimiter=delim, flags=flags))
    folders.sort(key=lambda f: (f.raw.upper() != "INBOX", f.display.lower()))
    return folders


_INTERNALDATE = re.compile(
    rb'INTERNALDATE\s+"\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})'
    rb'(?:\s*([-+])(\d{2}):?(\d{2}))?\s*"', re.IGNORECASE)
_MONTHS = {m.lower(): i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def parse_internaldate(meta: bytes):
    """INTERNALDATE -> секунды эпохи или None. Мягче imaplib.Internaldate2tuple: тот требует
    ровно «DD-Mon-YYYY HH:MM:SS +ZZZZ» и на любом отступлении молча возвращает None —
    тогда у файла оставалась дата загрузки вместо даты получения."""
    m = _INTERNALDATE.search(meta)
    if not m:
        return None
    day, mon, year, hh, mm, ss, sign, zh, zm = m.groups()
    month = _MONTHS.get(mon.decode("ascii").lower())
    if not month:
        return None
    try:
        utc = calendar.timegm((int(year), month, int(day), int(hh), int(mm), int(ss), 0, 0, 0))
    except (ValueError, OverflowError):
        return None
    if sign:
        zone = (int(zh) * 60 + int(zm)) * 60
        utc -= zone if sign == b"+" else -zone
    return float(utc)


def parse_fetch_response(data):
    """Возвращает (тело письма, флаги, internaldate в секундах эпохи | None)."""
    body = None
    meta = b""
    for part in data:
        if isinstance(part, tuple):
            meta += part[0] + b" "
            if body is None and b"BODY[" in part[0].upper():
                body = part[1]
        elif isinstance(part, bytes):
            meta += part + b" "
    if body is None:
        raise ImapError(tr('Сервер не вернул содержимое письма (письмо удалено, повреждено или недоступно)'))
    m = re.search(rb"FLAGS \(([^)]*)\)", meta)
    flags = m.group(1).decode("utf-8", "replace") if m else ""
    idate = parse_internaldate(meta)
    return body, flags, idate


def uid_set(uids) -> str:
    """«1:5,7,9:12» — UID подряд сворачиваются в диапазоны. В диапазон попадают только числа,
    которые все есть в списке, поэтому лишних писем сервер не пришлёт."""
    nums = sorted({int(u) for u in uids})
    out = []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}:{nums[j]}")
        i = j + 1
    return ",".join(out)


def _uid_chunks(uids, limit=4000):
    """UID-наборы не длиннее limit символов: длинную команду сервер может отвергнуть."""
    nums = sorted({int(u) for u in uids})
    chunk, size = [], 0
    for n in nums:
        chunk.append(n)
        size += len(str(n)) + 1
        if size >= limit:
            yield uid_set(chunk)
            chunk, size = [], 0
    if chunk:
        yield uid_set(chunk)


_RESP_START = re.compile(rb"\s*\d+\s+\(")
_UID = re.compile(rb"\bUID\s+(\d+)", re.IGNORECASE)
_MSGID = re.compile(rb"(?im)^message-id:[ \t]*(.*(?:\r?\n[ \t].*)*)")


def _split_fetch(data):
    """Ответ imaplib на FETCH нескольких писем -> [(метаданные, тело | None)] по письму.
    Каждое письмо начинается с «N (…»; литерал тела приходит кортежем, остаток строки
    (например, « FLAGS (\\Seen))») — отдельными байтами."""
    msgs = []
    for part in data:
        if isinstance(part, tuple):
            head, lit = part[0], part[1]
        elif isinstance(part, bytes):
            head, lit = part, None
        else:
            continue
        if _RESP_START.match(head) or not msgs:
            msgs.append([b"", None])
        m = msgs[-1]
        m[0] += head + b" "
        if lit is not None and m[1] is None and b"BODY[" in head.upper():
            m[1] = lit
    return msgs


def parse_fetch_many(data) -> dict:
    """{uid: (тело, флаги, internaldate)} для писем, у которых сервер прислал тело."""
    out = {}
    for meta, body in _split_fetch(data):
        m = _UID.search(meta)
        if body is None or not m:
            continue
        f = re.search(rb"FLAGS \(([^)]*)\)", meta)
        out[m.group(1).decode()] = (body, f.group(1).decode("utf-8", "replace") if f else "",
                                    parse_internaldate(meta))
    return out


class ImapConnection:
    def __init__(self, account):
        self.acc = account
        self.M = None

    # ---------- подключение ----------
    def connect(self):
        a = self.acc
        ctx = make_ssl_context(a.accept_all_certs)
        try:
            if a.security == "ssl":
                M = imaplib.IMAP4_SSL(a.host, a.port, ssl_context=ctx, timeout=TIMEOUT)
            else:
                M = imaplib.IMAP4(a.host, a.port, timeout=TIMEOUT)
                if a.security == "starttls":
                    M.starttls(ssl_context=ctx)
        except ssl.SSLCertVerificationError as e:
            raise ImapError(tr('Сертификат сервера не прошёл проверку ({0}). Включите «Принимать все сертификаты».').format(e.verify_message)) from e
        except ssl.SSLError as e:
            raise ImapError(tr('Ошибка SSL/TLS: {e}. Проверьте тип шифрования и порт.').format(e=e)) from e
        except OSError as e:
            raise ImapError(tr('Не удалось подключиться к {0}:{1} — {e}').format(a.host, a.port, e=e)) from e
        self.M = M
        self._login()

    def _login(self):
        user = self.acc.login or self.acc.email
        pw = self.acc.password
        try:
            try:
                # imaplib берёт пароль в кавычки, а логин — нет: с пробелом или кавычкой
                # в логине команда ломалась
                self.M.login(quote(user), pw)
            except UnicodeEncodeError:
                # Пароль/логин не в ASCII — используем AUTHENTICATE PLAIN в UTF-8
                self.M.authenticate("PLAIN", lambda _: f"\0{user}\0{pw}".encode("utf-8"))
        except imaplib.IMAP4.error as e:
            msg = e.args[0].decode() if e.args and isinstance(e.args[0], bytes) else str(e)
            raise ImapError(tr('Ошибка авторизации: {msg}').format(msg=msg)) from e

    def logout(self):
        if self.M is not None:
            try:
                self.M.logout()
            except Exception:
                try:                    # оборванное соединение: imaplib сам сокет не закроет
                    self.M.shutdown()
                except Exception:
                    pass
            self.M = None

    # ---------- операции ----------
    def list_folders(self) -> list[Folder]:
        typ, data = self.M.list()
        if typ != "OK":
            raise ImapError(tr('Не удалось получить список папок: {data}').format(data=data))
        return parse_list_response(data)

    def select(self, folder: Folder) -> str:
        typ, data = self.M.select(quote(folder.raw), readonly=True)
        if typ != "OK":
            raise ImapError(tr('Не удалось открыть папку «{0}»: {data}').format(folder.display, data=data))
        _, uv = self.M.response("UIDVALIDITY")
        return uv[0].decode() if uv and uv[0] else "0"

    def search(self, date_from: date | None, date_to: date | None) -> list[str]:
        crit = []
        if date_from:
            crit += ["SINCE", imap_date(date_from)]
        if date_to:
            crit += ["BEFORE", imap_date(date_to + timedelta(days=1))]  # BEFORE не включает день
        if not crit:
            crit = ["ALL"]
        typ, data = self.M.uid("SEARCH", *crit)
        if typ != "OK":
            raise ImapError(tr('Ошибка поиска писем: {data}').format(data=data))
        uids = []
        for part in data:
            if part:
                uids += part.decode().split()
        return uids

    def fetch(self, uid: str):
        typ, data = self.M.uid("FETCH", uid, "(UID INTERNALDATE FLAGS BODY.PEEK[])")
        if typ != "OK":
            raise ImapError(tr('Сервер отказал в выдаче письма: {data}').format(data=data))
        return parse_fetch_response(data)

    def fetch_sizes(self, uids) -> dict:
        """{uid: размер письма в байтах} одной-двумя командами — чтобы набирать пачки."""
        out = {}
        for chunk in _uid_chunks(uids):
            typ, data = self.M.uid("FETCH", chunk, "(UID RFC822.SIZE)")
            if typ != "OK":
                raise ImapError(tr('Сервер отказал в выдаче письма: {data}').format(data=data))
            for part in data:
                meta = part[0] if isinstance(part, tuple) else part
                if not isinstance(meta, bytes):
                    continue
                u = _UID.search(meta)
                z = re.search(rb"RFC822\.SIZE\s+(\d+)", meta, re.IGNORECASE)
                if u and z:
                    out[u.group(1).decode()] = int(z.group(1))
        return out

    def fetch_meta(self, uids) -> dict:
        """{uid: (размер, Message-ID)} — для пачек и поиска повторов, без тел писем."""
        out = {}
        for chunk in _uid_chunks(uids):
            typ, data = self.M.uid("FETCH", chunk,
                                   "(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            if typ != "OK":
                raise ImapError(tr('Сервер отказал в выдаче письма: {data}').format(data=data))
            for meta, head in _split_fetch(data):
                u = _UID.search(meta)
                z = re.search(rb"RFC822\.SIZE\s+(\d+)", meta, re.IGNORECASE)
                if not (u and z):
                    continue
                m = _MSGID.search(head or b"")
                msgid = msgid_key(m.group(1).decode("latin-1")) if m else ""
                out[u.group(1).decode()] = (int(z.group(1)), msgid)
        return out

    def fetch_many(self, uids) -> dict:
        """Несколько писем одной командой: {uid: (тело, флаги, internaldate)}. Письма, которых
        нет в ответе, вызывающий докачивает по одному."""
        typ, data = self.M.uid("FETCH", uid_set(uids), "(UID INTERNALDATE FLAGS BODY.PEEK[])")
        if typ != "OK":
            raise ImapError(tr('Сервер отказал в выдаче письма: {data}').format(data=data))
        return parse_fetch_many(data)


def server_params(account) -> dict:
    """Параметры подключения для ядра на Rust."""
    return {"host": account.host, "port": account.port, "security": account.security,
            "accept_all_certs": account.accept_all_certs,
            "user": account.login or account.email, "password": account.password}


def server_folders(account) -> list[Folder]:
    """Список папок сервера (окно ящика). Подключается ядро на Rust, а если оно не
    договорилось с сервером о TLS (сервер знает лишь TLS 1.0/1.1) — Python-код."""
    from . import mailparse
    native = getattr(mailparse, "_native", None) if mailparse.NATIVE else None
    if hasattr(native, "imap_list_folders"):
        try:
            return [Folder(raw=r, display=d, delimiter=dl, flags=list(fl))
                    for r, d, dl, fl in native.imap_list_folders(server_params(account))]
        except native.TlsError:
            pass
        except native.ImapError as e:
            raise ImapError(str(e)) from None
    c = ImapConnection(account)
    c.connect()
    try:
        return c.list_folders()
    finally:
        c.logout()
