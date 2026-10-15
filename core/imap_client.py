"""Работа с IMAP-сервером: подключение, список папок, поиск и загрузка писем."""
import imaplib
import re
import ssl
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import imap_utf7

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


def parse_fetch_response(data):
    """Возвращает (тело письма, флаги, internaldate struct_time | None)."""
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
        raise ImapError("Сервер не вернул содержимое письма "
                        "(письмо удалено, повреждено или недоступно)")
    m = re.search(rb"FLAGS \(([^)]*)\)", meta)
    flags = m.group(1).decode("utf-8", "replace") if m else ""
    idate = imaplib.Internaldate2tuple(meta)
    return body, flags, idate


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
            raise ImapError(f"Сертификат сервера не прошёл проверку ({e.verify_message}). "
                            "Включите «Принимать все сертификаты».") from e
        except ssl.SSLError as e:
            raise ImapError(f"Ошибка SSL/TLS: {e}. Проверьте тип шифрования и порт.") from e
        except OSError as e:
            raise ImapError(f"Не удалось подключиться к {a.host}:{a.port} — {e}") from e
        self.M = M
        self._login()

    def _login(self):
        user = self.acc.login or self.acc.email
        pw = self.acc.password
        try:
            try:
                self.M.login(user, pw)
            except UnicodeEncodeError:
                # Пароль/логин не в ASCII — используем AUTHENTICATE PLAIN в UTF-8
                self.M.authenticate("PLAIN", lambda _: f"\0{user}\0{pw}".encode("utf-8"))
        except imaplib.IMAP4.error as e:
            msg = e.args[0].decode() if e.args and isinstance(e.args[0], bytes) else str(e)
            raise ImapError(f"Ошибка авторизации: {msg}") from e

    def logout(self):
        if self.M is not None:
            try:
                self.M.logout()
            except Exception:
                pass
            self.M = None

    # ---------- операции ----------
    def list_folders(self) -> list[Folder]:
        typ, data = self.M.list()
        if typ != "OK":
            raise ImapError(f"Не удалось получить список папок: {data}")
        return parse_list_response(data)

    def select(self, folder: Folder) -> str:
        typ, data = self.M.select(quote(folder.raw), readonly=True)
        if typ != "OK":
            raise ImapError(f"Не удалось открыть папку «{folder.display}»: {data}")
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
            raise ImapError(f"Ошибка поиска писем: {data}")
        uids = []
        for part in data:
            if part:
                uids += part.decode().split()
        return uids

    def fetch(self, uid: str):
        typ, data = self.M.uid("FETCH", uid, "(UID INTERNALDATE FLAGS BODY.PEEK[])")
        if typ != "OK":
            raise ImapError(f"Сервер отказал в выдаче письма: {data}")
        return parse_fetch_response(data)
