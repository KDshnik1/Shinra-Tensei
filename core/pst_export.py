"""Экспорт локального архива (.eml) в Outlook .pst.

Основной движок — встроенный (core/pst_writer.py): PST-файл собирается
программой по спецификации [MS-PST]. Outlook не нужен ни запущенный,
ни установленный. После записи файл проверяется независимым читателем.
"""
import email.utils
import html as html_mod
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import mailparse
from .pst_reader import PstCheckError, verify
from .pst_writer import NID_IPM_SUBTREE, PstFile, filetime
from .storage import load_flags, safe_name

SPLIT_SIZE = 1_900_000_000          # PST делится на части по ~1,9 ГБ


class PstExportError(Exception):
    pass


def check_password(pw: str) -> str | None:
    if not pw:
        return None
    if len(pw) > 15:
        return "Пароль PST — не длиннее 15 символов (ограничение Outlook)."
    if not all(32 < ord(c) < 127 for c in pw):
        return "Пароль PST — только латинские буквы, цифры и знаки, без пробелов и кириллицы."
    return None


def collect_tasks(mailbox_dir: Path):
    meta_path = mailbox_dir / "folders.json"
    if not meta_path.exists():
        return []
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    tasks = []
    for rel, info in meta.items():
        fdir = mailbox_dir.joinpath(*rel.split("/"))
        emls = sorted(fdir.glob("*.eml")) if fdir.exists() else []
        tasks.append((info, fdir, emls))
    tasks.sort(key=lambda t: (t[0]["display"].upper() != "INBOX", t[0]["display"].lower()))
    return tasks


def _html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", h)
    h = re.sub(r"<[^>]+>", "", h)
    return re.sub(r"\n{3,}", "\n\n", html_mod.unescape(h)).strip()


def _addr_list(s: str):
    return [(n, a) for n, a in email.utils.getaddresses([s]) if a or n]


def eml_to_message(eml: Path, read: bool) -> dict:
    info = mailparse.summarize(eml)
    body = mailparse.parse_full(eml)
    sent = mailparse.sent_datetime(info)
    sent_ft = filetime(sent.timestamp()) if sent else None
    recv_ft = filetime(info["received"]) if info.get("received") else sent_ft
    sender_name = info["from_name"] or info["from_addr"]
    subject = info["subject"]
    text = body.text if body.text is not None else (_html_to_text(body.html) if body.html else "")

    props = {
        0x001A001F: "IPM.Note",
        0x0037001F: subject, 0x0E1D001F: subject, 0x0070001F: subject,
        0x00170003: 1, 0x00360003: 0, 0x00260003: 0,
        0x0E080003: info["size"], 0x0E170003: 0,
        0x00390040: sent_ft, 0x0E060040: recv_ft,
        0x30070040: recv_ft, 0x30080040: recv_ft,
        0x0C1A001F: sender_name, 0x0C1F001F: info["from_addr"], 0x0C1E001F: "SMTP",
        0x5D01001F: info["from_addr"],
        0x0042001F: sender_name, 0x0065001F: info["from_addr"], 0x0064001F: "SMTP",
        0x5D02001F: info["from_addr"],
        0x1035001F: info["msgid"] or None,
        0x007D001F: mailparse.head_text(eml),
        0x1000001F: text,
        0x300B0102: os.urandom(16),
    }
    if body.html:
        props[0x10130102] = body.html.encode("utf-8")
        props[0x3FDE0003] = 65001

    recipients, disp = [], {1: [], 2: []}
    for rtype, field in ((1, "to"), (2, "cc")):
        for name, addr in _addr_list(info[field]):
            nm = name or addr
            disp[rtype].append(nm)
            recipients.append({0x0C150003: rtype, 0x0E0F000B: True, 0x0FFE0003: 6,
                               0x39000003: 0, 0x3001001F: nm, 0x3002001F: "SMTP",
                               0x3003001F: addr, 0x39FE001F: addr, 0x39FF001F: nm,
                               0x0FF90102: os.urandom(16), 0x300B0102: ("SMTP:" + addr.upper()).encode() + b"\0",
                               0x3A40000B: False})
    props[0x0E04001F] = "; ".join(disp[1])
    props[0x0E03001F] = "; ".join(disp[2])

    attachments = []
    for a in body.attachments:
        ext = Path(a.name).suffix
        short = (safe_name(Path(a.name).stem, 8) + ext[:4]) if ext else safe_name(a.name, 8)
        ap = {0x37050003: 1, 0x0E200003: len(a.data), 0x3707001F: a.name, 0x3704001F: short,
              0x3001001F: a.name, 0x370E001F: a.ctype, 0x370B0003: 0xFFFFFFFF,
              0x0FFE0003: 7, 0x37010102: a.data, 0x30070040: recv_ft, 0x30080040: recv_ft}
        if a.cid:
            ap[0x3712001F] = a.cid
        if a.inline:
            ap[0x7FFE000B] = True
        row = {0x0E200003: len(a.data), 0x3704001F: short, 0x37050003: 1,
               0x370B0003: 0xFFFFFFFF}
        attachments.append({"props": ap, "row": row})
    return {"props": props, "recipients": recipients, "attachments": attachments, "read": read}


def _pst_folder_name(seg: str, top_level: bool) -> str:
    if top_level and seg.upper() == "INBOX":
        return "Входящие"
    return seg.strip() or "_"


def export_mailbox(mailbox_dir: Path, pst_path: Path, display_name: str,
                   cb, stop_event, password: str = ""):
    """cb: log(str), progress(done, total). Возвращает (успешно, ошибок, [файлы])."""
    err = check_password(password)
    if err:
        raise PstExportError(err)
    tasks = collect_tasks(mailbox_dir)
    total = sum(len(t[2]) for t in tasks)
    if not tasks:
        raise PstExportError("В архиве нет писем для этого ящика — сначала выполните загрузку")
    pst_path.parent.mkdir(parents=True, exist_ok=True)

    files = []
    part = [0]
    state = {}

    def open_part():
        part[0] += 1
        path = pst_path if part[0] == 1 else pst_path.with_name(
            f"{pst_path.stem}_часть{part[0]}{pst_path.suffix}")
        name = display_name if part[0] == 1 else f"{display_name} ({part[0]})"
        state["pst"] = PstFile(path, name, password)
        state["folders"] = {}
        files.append(path)
        if part[0] > 1:
            cb.log(f"Файл достиг ~1,9 ГБ — начата часть {part[0]}: {path.name}")

    def folder_nid(segs):
        cache = state["folders"]
        parent = NID_IPM_SUBTREE
        for i, seg in enumerate(segs):
            key = tuple(segs[:i + 1])
            if key not in cache:
                cache[key] = state["pst"].add_folder(parent, _pst_folder_name(seg, i == 0))
            parent = cache[key]
        return parent

    open_part()
    ok = errors = done = 0
    cb.progress(0, total)
    try:
        for info, fdir, emls in tasks:
            if stop_event.is_set():
                break
            delim = info.get("delimiter") or ""
            segs = [s for s in (info["display"].split(delim) if delim else [info["display"]]) if s]
            folder_nid(segs)
            cb.log(f"Папка «{info['display']}»: {len(emls)} писем")
            flags = load_flags(fdir)
            for eml in emls:
                if stop_event.is_set():
                    break
                if state["pst"].size() > SPLIT_SIZE:
                    state["pst"].close()
                    open_part()
                try:
                    read = "\\seen" in flags.get(eml.stem, "").lower()
                    state["pst"].add_message(folder_nid(segs), eml_to_message(eml, read))
                    ok += 1
                except Exception as e:
                    errors += 1
                    cb.log(f"Ошибка в письме {eml.name}: {e}")
                done += 1
                cb.progress(done, total)
        cb.log("Запись структуры PST…")
        state["pst"].close()
    except BaseException:
        state["pst"].abort()
        raise

    for path in files:
        cb.log(f"Проверка файла {path.name}…")
        try:
            summary = verify(path)
            n = sum(v for k, v in summary.items() if not k.startswith("_"))
            cb.log(f"Проверка пройдена: {n} писем, структура PST корректна"
                   + (", установлен пароль" if summary["_password"] else ""))
        except PstCheckError as e:
            errors += 1
            cb.log(f"ВНИМАНИЕ: самопроверка файла {path.name} не пройдена: {e}")
    return ok, errors, files
