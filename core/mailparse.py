"""Разбор .eml: заголовки, тело, вложения. Устойчив к кривым кодировкам
(windows-1251 / koi8-r без объявления кодировки и т.п.)."""
import email.utils
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.header import decode_header
from email.parser import BytesParser
from pathlib import Path

from . import msgfile

_FALLBACK = ("utf-8", "cp1251", "koi8-r")


def _decode_bytes(b: bytes, charset=None) -> str:
    encs = []
    if charset and charset.lower() not in ("unknown-8bit", "x-unknown", "unknown"):
        encs.append(charset)
    encs += _FALLBACK
    for e in encs:
        try:
            return b.decode(e)
        except (LookupError, UnicodeDecodeError):
            pass
    return b.decode("latin-1")


def _fix8(s: str) -> str:
    """Строка с суррогатами (сырые 8-битные байты в заголовке) -> нормальный текст."""
    if any(0xDC80 <= ord(c) <= 0xDCFF for c in s):
        return _decode_bytes(s.encode("utf-8", "surrogateescape"))
    return s


def decode_hdr(v) -> str:
    if v is None:
        return ""
    try:
        parts = decode_header(v)
    except Exception:
        return _fix8(str(v))
    out = []
    for val, cs in parts:
        out.append(_decode_bytes(val, cs) if isinstance(val, bytes) else _fix8(val))
    return " ".join("".join(out).split())


def _read_head(path: Path, limit=512 * 1024) -> bytes:
    with msgfile.open_message(path) as fh:
        data = fh.read(limit)
    cuts = [i for i in (data.find(b"\r\n\r\n"), data.find(b"\n\n")) if i >= 0]
    return (data[:min(cuts)] if cuts else data) + b"\r\n\r\n"


def _walk(part):
    """Листовые части письма; вложенное письмо (message/rfc822) — одна часть, без разбора."""
    if (part.get_content_type() or "").lower() == "message/rfc822":
        yield part
        return
    if part.is_multipart():
        for sub in part.get_payload():
            yield from _walk(sub)
    else:
        yield part


def iter_parts(msg):
    """Части письма: (part, ctype, disp, fname, cid, kind).
    kind: "body" — текст/HTML письма, "inline" — картинка внутри письма (cid:),
          "attachment" — настоящее вложение (его видно в списке вложений)."""
    seen_html = seen_text = False
    for part in _walk(msg):
        ctype = (part.get_content_type() or "").lower()
        try:
            disp = part.get_content_disposition()
        except Exception:
            disp = None
        try:
            fname = part.get_filename()
        except Exception:
            fname = None
        fname = decode_hdr(fname) if fname else ""
        cid = str(part.get("Content-ID") or "").strip().strip("<>")
        if ctype == "message/rfc822":
            if not fname:
                inner = part.get_payload()
                inner = inner[0] if isinstance(inner, list) and inner else inner
                subj = decode_hdr(inner.get("Subject")) if hasattr(inner, "get") else ""
                safe = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in subj)
                fname = (safe.strip(" .")[:80] or "Пересланное письмо") + ".eml"
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


def summarize(path: Path, st=None) -> dict:
    """Краткие сведения о письме (только заголовки — быстро)."""
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
        "msgid": (msg.get("Message-ID") or "").strip(),
        "sent": sent,                       # ISO с часовым поясом, из заголовка Date
        "received": int(st.st_mtime),       # дата получения сервером (IMAP INTERNALDATE)
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
        c = part.get_content()
        if isinstance(c, str):
            return _fix8(c)
    except Exception:
        pass
    payload = part.get_payload(decode=True) or b""
    return _decode_bytes(payload, part.get_content_charset())


def parse_full(path: Path) -> Body:
    with msgfile.open_message(path) as fh:
        msg = BytesParser(policy=policy.default).parse(fh)
    body = Body()
    n = 0
    for part, ctype, disp, fname, cid, kind in iter_parts(msg):
        try:
            if kind == "body":
                if ctype == "text/html":
                    body.html = _part_text(part)
                else:
                    body.text = _part_text(part)
                continue
            n += 1
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
    return body


def sent_datetime(row) -> datetime | None:
    if row.get("sent"):
        try:
            return datetime.fromisoformat(row["sent"])
        except ValueError:
            return None
    return None
