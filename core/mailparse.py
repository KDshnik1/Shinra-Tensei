"""Разбор .eml: заголовки, тело, вложения. Устойчив к кривым кодировкам
(windows-1251 / koi8-r без объявления кодировки и т.п.)."""
import codecs
import email.message
import email.utils
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.header import decode_header, ecre
from email.parser import BytesParser
from email.policy import Compat32
from pathlib import Path

from . import msgfile
from .i18n import tr

_FALLBACK = ("utf-8", "cp1251", "koi8-r")
_B64_OK = re.compile(r"[A-Za-z0-9+/=]*")


def _decode_bytes(b: bytes, charset=None) -> str:
    encs = []
    if charset and charset.lower() not in ("unknown-8bit", "x-unknown", "unknown"):
        encs.append(charset)
    encs += _FALLBACK
    for e in encs:
        try:
            return b.decode(e)
        except (LookupError, ValueError):     # ValueError: в имени кодировки \0 и т.п.
            pass
    return b.decode("latin-1")


def _fix8(s: str) -> str:
    """Строка с суррогатами (сырые 8-битные байты в заголовке) -> нормальный текст."""
    if any(0xDC80 <= ord(c) <= 0xDCFF for c in s):
        return _decode_bytes(s.encode("utf-8", "surrogateescape"))
    return s


def _bad_b64_word(s: str) -> bool:
    """Есть закодированное слово =?..?B?..?= с символами не из base64: такое слово
    декодировалось бы в бессмыслицу — заголовок лучше оставить как есть."""
    return any(m.group("encoding").lower() == "b" and not _B64_OK.fullmatch(m.group("encoded"))
               for m in ecre.finditer(s))


def decode_hdr(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str) and _bad_b64_word(v):
        return " ".join(_fix8(v).split())
    try:
        parts = decode_header(v)
    except Exception:
        return " ".join(_fix8(str(v)).split())
    out = []
    for val, cs in parts:
        out.append(_decode_bytes(val, cs) if isinstance(val, bytes) else _fix8(val))
    return " ".join("".join(out).split())


def _read_head(path: Path, limit=512 * 1024) -> bytes:
    with msgfile.open_message(path) as fh:
        data = fh.read(limit)
    cuts = [i for i in (data.find(b"\r\n\r\n"), data.find(b"\n\n")) if i >= 0]
    return (data[:min(cuts)] if cuts else data) + b"\r\n\r\n"


def _raw(part, name: str):
    """Исходное значение заголовка (первое с таким именем) или None."""
    name = name.lower()
    for k, v in part.raw_items():
        if k.lower() == name:
            return v
    return None


def _ctype(part) -> str:
    try:
        return (part.get_content_type() or "").lower()
    except Exception:       # разборщик заголовков policy.default падает на кривых параметрах
        v = _raw(part, "content-type")
        if v is None:
            return part.get_default_type()
        ct = _fix8(v).split(";")[0].strip().lower()
        return ct if ct.count("/") == 1 else "text/plain"


def _hdr(part, name: str) -> str:
    try:
        return str(part.get(name) or "")
    except Exception:
        v = _raw(part, name)
        return _fix8(v) if v else ""


def _walk(part):
    """Листовые части письма; вложенное письмо (message/rfc822) — одна часть, без разбора."""
    if _ctype(part) == "message/rfc822":
        yield part
        return
    if part.is_multipart():
        for sub in part.get_payload():
            yield from _walk(sub)
    else:
        yield part


class _RawPolicy(Compat32):
    """Заголовки как есть, со всеми 8-битными байтами (compat32 заменил бы их на «�»)."""

    def header_fetch_parse(self, name, value):
        return value


_RAW = _RawPolicy()


def _filename(part) -> str:
    try:
        fname = part.get_filename()
    except Exception:
        fname = None
    if fname is None or "\ufffd" in fname:
        # 8-битное имя без кодировки (или кривой заголовок): берём исходные байты
        m = email.message.Message(policy=_RAW)
        for k, v in part.raw_items():
            if k.lower() in ("content-disposition", "content-type"):
                m.set_raw(k, v)
        try:
            fname = m.get_filename() or fname
        except Exception:
            pass
    return decode_hdr(fname) if fname else ""


def iter_parts(msg):
    """Части письма: (part, ctype, disp, fname, cid, kind).
    kind: "body" — текст/HTML письма, "inline" — картинка внутри письма (cid:),
          "attachment" — настоящее вложение (его видно в списке вложений)."""
    seen_html = seen_text = False
    for part in _walk(msg):
        ctype = _ctype(part)
        try:
            disp = part.get_content_disposition()
        except Exception:
            disp = None
        fname = _filename(part)
        cid = _hdr(part, "Content-ID").strip().strip("<>")
        if ctype == "message/rfc822":
            if not fname:
                inner = part.get_payload()
                inner = inner[0] if isinstance(inner, list) and inner else inner
                subj = decode_hdr(_hdr(inner, "Subject")) if hasattr(inner, "raw_items") else ""
                safe = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in subj)
                fname = (safe.strip(" .")[:80] or tr('Пересланное письмо')) + ".eml"
            yield part, ctype, disp, fname, cid, "attachment"
            continue
        if disp != "attachment" and not fname:
            if ctype == "text/html" and not seen_html:
                seen_html = True
                yield part, ctype, disp, fname, cid, "body"
                continue
            if ctype == "text/plain" and not seen_text:
                seen_text = True
                yield part, ctype, disp, fname, cid, "body"
                continue
        inline = bool(cid) and disp != "attachment" and ctype.startswith("image/")
        yield part, ctype, disp, fname, cid, ("inline" if inline else "attachment")


def _attachment_names(path: Path) -> list:
    with msgfile.open_message(path) as fh:
        msg = BytesParser(policy=policy.compat32).parse(fh)
    names = []
    for _p, ctype, _d, fname, _c, kind in iter_parts(msg):
        if kind == "attachment":
            names.append(fname or ctype)
    return names


_MIN_TS = 315532800            # 01.01.1980: всё, что раньше, — заведомо неверная дата


def _hdr_ts(value):
    """Время из даты заголовка (секунды) или None, если дата не разобрана или неправдоподобна."""
    if not value or not value.isascii():
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except Exception:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    try:
        ts = d.timestamp()
    except (OverflowError, ValueError, OSError):
        return None
    return ts if _MIN_TS <= ts <= time.time() + 2 * 86400 else None


def header_received(msg):
    """Когда письмо получил почтовый сервер — по заголовкам: дата из верхнего Received
    (его добавляет сервер при приёме), для писем без Received (отправленные, черновики)
    — из Date. None, если в заголовках нет правдоподобной даты."""
    rcv = _raw(msg, "Received")
    ts = None
    if rcv and ";" in rcv:
        ts = _hdr_ts(rcv.rpartition(";")[2])
    if ts is None:
        ts = _hdr_ts(_raw(msg, "Date"))
    return ts


def received_from_bytes(data: bytes):
    """header_received() для письма в памяти (заголовки — до первой пустой строки)."""
    cuts = [i for i in (data.find(b"\r\n\r\n"), data.find(b"\n\n")) if i >= 0]
    head = data[:min(cuts)] if cuts else data[:512 * 1024]
    try:
        msg = BytesParser(policy=policy.compat32).parsebytes(head + b"\r\n\r\n", headersonly=True)
    except Exception:
        return None
    return header_received(msg)


# Нативное ядро (Rust): summarize() в разы быстрее и без GIL — индекс просмотрщика
# строится в несколько потоков. Без модуля или с SHINRA_PURE_PYTHON=1 — Python-код.
NATIVE = False
if not os.environ.get("SHINRA_PURE_PYTHON"):
    try:
        import shinra_core as _native
        _native.set_translator(tr)
        NATIVE = hasattr(_native, "summarize")
    except ImportError:
        pass


def summarize(path: Path, st=None) -> dict:
    """Краткие сведения о письме (только заголовки — быстро)."""
    if NATIVE:
        return _native.summarize(path)
    return _py_summarize(path, st)


def _py_summarize(path: Path, st=None) -> dict:
    st = st or path.stat()
    head = _read_head(path)
    msg = BytesParser(policy=policy.compat32).parsebytes(head, headersonly=True)
    frm = decode_hdr(msg.get("From"))
    name, addr = email.utils.parseaddr(frm)
    sent = None
    try:
        d = email.utils.parsedate_to_datetime(msg.get("Date"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        sent = d.isoformat()
    except Exception:
        pass
    ctype = (msg.get_content_type() or "").lower()
    # тело читаем только если в письме могут быть вложения (не простой текст/HTML)
    att_names = []
    if ctype.startswith("multipart/") or ctype not in ("text/plain", "text/html"):
        try:
            att_names = _attachment_names(path)
        except Exception:
            att_names = []
    return {
        "file": path.name,
        "from": frm,
        "from_name": name,
        "from_addr": addr,
        "to": decode_hdr(msg.get("To")),
        "cc": decode_hdr(msg.get("Cc")),
        "subject": decode_hdr(msg.get("Subject")),
        "msgid": _fix8(_raw(msg, "Message-ID") or "").strip(),
        "sent": sent,                       # ISO с часовым поясом, из заголовка Date
        # дата получения: по заголовкам письма; если их нет — дата файла (IMAP INTERNALDATE).
        # Дате файла одной верить нельзя: при сбое загрузки там оказывается день загрузки.
        "received": int(header_received(msg) or st.st_mtime),
        "size": msgfile.raw_size(path, st),     # размер письма без сжатия
        "fsize": st.st_size,                    # размер файла на диске (для кэша)
        "mtime": int(st.st_mtime),
        "attach": len(att_names),                 # число настоящих вложений
        "att_names": att_names[:10],
    }


def head_text(path: Path) -> str:
    return _decode_bytes(_read_head(path).rstrip(b"\r\n"))


@dataclass
class Attachment:
    name: str
    ctype: str
    data: bytes
    cid: str = ""
    inline: bool = False


@dataclass
class Body:
    text: str | None = None
    html: str | None = None
    attachments: list = field(default_factory=list)


def _part_text(part) -> str:
    try:
        payload = part.get_payload(decode=True) or b""
    except Exception:
        payload = b""
    try:
        charset = part.get_content_charset()
    except Exception:
        charset = None
    if charset:
        try:
            ascii_ = codecs.lookup(charset).name == "ascii"
        except (LookupError, ValueError):
            ascii_ = False
        if not ascii_:
            # кодировка объявлена — ей верим, отдельные битые байты заменяются
            try:
                return payload.decode(charset, "replace")
            except (LookupError, ValueError):
                pass
    # кодировка не указана (или ASCII, или неизвестная): письма без charset
    # нередко в windows-1251 / koi8-r — подбираем, а не заменяем всё на «�»
    return _decode_bytes(payload, charset)


def _safe_attach_name(name: str) -> str:
    """Имя вложения без пути («C:\\dir\\a.txt» -> «a.txt»)."""
    return name.replace("\\", "/").rsplit("/", 1)[-1].strip()


def parse_full(path: Path) -> Body:
    # compat32: разборщик заголовков policy.default падает на кривых параметрах
    # (IndexError и т.п. прямо внутри BytesParser), а compat32 берёт их как есть
    with msgfile.open_message(path) as fh:
        try:
            msg = BytesParser(policy=policy.compat32).parse(fh)
        except RecursionError:      # тысячи вложенных частей — тело не разобрать
            return Body()
    body = Body()
    n = 0
    try:
        for part, ctype, disp, fname, cid, kind in iter_parts(msg):
            try:
                if kind == "body":
                    if ctype == "text/html":
                        body.html = _part_text(part)
                    else:
                        body.text = _part_text(part)
                    continue
                n += 1
                fname = _safe_attach_name(fname)
                if not fname:
                    ext = {"text/plain": ".txt", "text/html": ".html", "message/rfc822": ".eml"}.get(
                        ctype, "." + ctype.split("/")[-1] if "/" in ctype else ".bin")
                    fname = f"attachment_{n}{ext}"
                if ctype == "message/rfc822":          # пересланное письмо -> .eml
                    inner = part.get_payload()
                    inner = inner[0] if isinstance(inner, list) and inner else inner
                    data = inner.as_bytes() if hasattr(inner, "as_bytes") else b""
                else:
                    data = part.get_payload(decode=True) or b""
                body.attachments.append(Attachment(fname, ctype, data, cid,
                                                   inline=(kind == "inline")))
            except Exception:
                continue
    except RecursionError:     # сотни вложенных частей: что успели — сохраняем
        pass
    return body


def sent_datetime(row) -> datetime | None:
    if row.get("sent"):
        try:
            return datetime.fromisoformat(row["sent"])
        except ValueError:
            return None
    return None
