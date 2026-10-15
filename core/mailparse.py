"""Разбор .eml: заголовки, тело, вложения. Устойчив к кривым кодировкам
(windows-1251 / koi8-r без объявления кодировки и т.п.)."""
import email.utils
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.header import decode_header
from email.parser import BytesParser
from pathlib import Path

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
    with open(path, "rb") as fh:
        data = fh.read(limit)
    cuts = [i for i in (data.find(b"\r\n\r\n"), data.find(b"\n\n")) if i >= 0]
    return (data[:min(cuts)] if cuts else data) + b"\r\n\r\n"


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
        "size": st.st_size,
        "mtime": int(st.st_mtime),
        "attach": ctype in ("multipart/mixed", "multipart/report"),
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
    with open(path, "rb") as fh:
        msg = BytesParser(policy=policy.default).parse(fh)
    body = Body()
    n = 0
    for part in msg.walk():
        try:
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            disp = part.get_content_disposition()
            try:
                fname = part.get_filename()
            except Exception:
                fname = None
            fname = decode_hdr(fname) if fname else ""
            cid = str(part.get("Content-ID") or "").strip().strip("<>")
            is_text = ctype in ("text/plain", "text/html")
            if disp != "attachment" and is_text and not fname:
                if ctype == "text/html" and body.html is None:
                    body.html = _part_text(part)
                    continue
                if ctype == "text/plain" and body.text is None:
                    body.text = _part_text(part)
                    continue
            n += 1
            if not fname:
                ext = {"text/plain": ".txt", "text/html": ".html", "message/rfc822": ".eml"}.get(
                    ctype, "." + ctype.split("/")[-1] if "/" in ctype else ".bin")
                fname = f"attachment_{n}{ext}"
            data = part.get_payload(decode=True)
            if data is None:                 # например, message/rfc822
                data = part.as_bytes() if hasattr(part, "as_bytes") else b""
            body.attachments.append(Attachment(
                fname, ctype, data, cid,
                inline=bool(cid) and disp != "attachment" and ctype.startswith("image/")))
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
