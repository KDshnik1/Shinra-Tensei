"""Экспорт локального архива (.eml) в Outlook .pst.

Основной движок — встроенный (core/pst_writer.py): PST-файл собирается
программой по спецификации [MS-PST]. Outlook не нужен ни запущенный,
ни установленный. После записи файл проверяется независимым читателем.

Если собран нативный модуль shinra_core (Rust, папка native/), письма
разбираются и записываются им — тот же результат, но в разы быстрее.
"""
import email.utils
import json
import os
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import mailparse, msgfile, pst_writer
from .pst_reader import verify
from .pst_writer import NATIVE, NID_IPM_SUBTREE, PstFile, filetime
from .storage import load_flags, safe_name
from .i18n import DATE_FMT, DATETIME_FMT, dec, num, tr

GB = 1024 ** 3
# Нативное ядро разбирает письма в нескольких потоках (без GIL), а пишет их по порядку.
PARALLEL_PARSE = NATIVE and hasattr(pst_writer._native, "parse_eml")
PARSE_THREADS = max(1, min(4, (os.cpu_count() or 2) - 1))
PARSE_AHEAD = 4 * PARSE_THREADS     # столько писем разбирается заранее (держатся в памяти)
PARSE_AHEAD_BYTES = 64 * 1024 * 1024  # и не больше стольких байт файлов писем
DEFAULT_SPLIT_GB = 10               # размер одной части по умолчанию
MIN_SPLIT_GB, MAX_SPLIT_GB = 0.5, 50   # 50 ГБ — предел Unicode PST в Outlook


class PstExportError(Exception):
    pass


def check_password(pw: str) -> str | None:
    if not pw:
        return None
    if len(pw) > 15:
        return tr('Пароль PST — не длиннее 15 символов (ограничение Outlook).')
    if not all(32 < ord(c) < 127 for c in pw):
        return tr('Пароль PST — только латинские буквы, цифры и знаки, без пробелов и кириллицы.')
    return None


def _mtime(p) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def collect_tasks(mailbox_dir: Path):
    meta_path = mailbox_dir / "folders.json"
    if not meta_path.exists():
        return []
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    tasks = []
    for rel, info in meta.items():
        fdir = mailbox_dir.joinpath(*rel.split("/"))
        emls = msgfile.list_messages(fdir)
        # по дате получения: тогда каждая часть PST покрывает непрерывный период
        emls.sort(key=lambda p: (_mtime(p), msgfile.msg_key(p)))
        tasks.append((info, fdir, emls))
    tasks.sort(key=lambda t: (t[0]["display"].upper() != "INBOX", t[0]["display"].lower()))
    return tasks


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
    text = body.text if body.text is not None else (mailparse.html_to_text(body.html) if body.html else "")

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
    return {"props": props, "recipients": recipients, "attachments": attachments, "read": read,
            "received": info["received"]}


def _pst_folder_name(seg: str, top_level: bool) -> str:
    if top_level and seg.upper() == "INBOX":
        return tr('Входящие')
    return seg.strip() or "_"


def _fmt_n(n: int) -> str:
    return num(n)


def _fmt_size(n: int) -> str:
    t = tr('{0:.2f} ГБ').format(n / GB) if n >= GB else tr('{0:.1f} МБ').format(n / 1024 ** 2)
    return dec(t)


def _fmt_gb(x: float) -> str:
    return dec(f"{x:g}")


def _fmt_d(ts) -> str:
    return datetime.fromtimestamp(ts).strftime(DATE_FMT) if ts else "—"


def _period(lo, hi) -> str:
    return f"{_fmt_d(lo)} – {_fmt_d(hi)}" if lo else "—"


class _PartStats:
    def __init__(self, path: Path):
        self.path = path
        self.folders = {}           # путь папки в PST -> [писем, мин. дата, макс. дата]
        self.count = 0
        self.lo = self.hi = None
        self.size = 0

    def add(self, folder: str, ts: float):
        f = self.folders.setdefault(folder, [0, ts, ts])
        f[0] += 1
        f[1] = min(f[1], ts)
        f[2] = max(f[2], ts)
        self.count += 1
        self.lo = ts if self.lo is None else min(self.lo, ts)
        self.hi = ts if self.hi is None else max(self.hi, ts)


def _aligned(rows, indent=""):
    w = max(len(k) for k, _v in rows) + 2
    return [indent + (k + ":").ljust(w + 1) + v for k, v in rows]


def write_report(report_path: Path, display_name: str, parts: list, split_bytes: int,
                 password: bool, errors: int, stopped: bool) -> Path:
    total = sum(p.count for p in parts)
    lo = min((p.lo for p in parts if p.lo), default=None)
    hi = max((p.hi for p in parts if p.hi), default=None)
    title = tr('Отчёт об экспорте в Outlook PST — Shinra Tensei')
    pw_text = tr('установлен') if password else tr('нет')
    gb = _fmt_gb(split_bytes / GB)
    rows = [(tr('Почтовый ящик'), display_name),
            (tr('Дата экспорта'), datetime.now().strftime(DATETIME_FMT)),
            (tr('Всего писем'), _fmt_n(total)),
            (tr('Период писем'), _period(lo, hi)),
            (tr('Файлов PST'), tr('{0} (размер одной части — до {gb} ГБ)').format(len(parts), gb=gb)),
            (tr('Пароль на PST'), pw_text)]
    if errors:
        rows.append((tr('Ошибок'), tr('{errors} (подробности — в журнале экспорта)').format(errors=errors)))
    out = [title, "=" * max(60, len(title))] + _aligned(rows)
    if stopped:
        out.append(tr('ВНИМАНИЕ: экспорт был остановлен, в файлы попали не все письма.'))
    out += ["", tr('Даты указаны по дате получения письма почтовым сервером.'), ""]
    h_folder, h_count, h_from, h_to = tr('Папка'), tr('Писем'), tr('С'), tr('По')
    for i, p in enumerate(parts, 1):
        out += ["-" * 60, tr('Часть {i}: {0}').format(p.path.name, i=i)]
        out += _aligned([(tr('Размер файла'), _fmt_size(p.size)),
                         (tr('Писем'), _fmt_n(p.count)),
                         (tr('Период'), _period(p.lo, p.hi))], "  ")
        out.append("")
        if not p.folders:
            out += [tr('  (писем нет)'), ""]
            continue
        w = max(16, len(h_folder) + 2, max(len(f) for f in p.folders) + 2)
        cw = max(10, len(h_count) + 2)
        out.append("  " + h_folder.ljust(w) + h_count.rjust(cw) + "    " + h_from.ljust(12) + h_to)
        for name, (n, a, b) in p.folders.items():
            out.append("  " + name.ljust(w) + _fmt_n(n).rjust(cw) + "    "
                       + _fmt_d(a).ljust(12) + _fmt_d(b))
        out.append("")
    report_path.write_text("\n".join(out) + "\n", encoding="utf-8-sig")
    return report_path


def _parse_ahead(pool, emls, flags, stop_event):
    """(eml, прочитано, future с разобранным письмом | None) по порядку. С нативным ядром
    следующие PARSE_AHEAD писем разбираются в пуле, пока текущее пишется в PST."""
    items = ((eml, "\\seen" in flags.get(msgfile.msg_key(eml), "").lower()) for eml in emls)
    if pool is None:
        for eml, read in items:
            yield eml, read, None
        return
    ahead = deque()
    size = 0
    try:
        for eml, read in items:
            try:
                n = eml.stat().st_size
            except OSError:
                n = 0
            ahead.append((eml, read, pool.submit(pst_writer._native.parse_eml, eml, read), n))
            size += n
            while len(ahead) > PARSE_AHEAD or (len(ahead) > 1 and size > PARSE_AHEAD_BYTES):
                eml0, read0, f0, n0 = ahead.popleft()
                size -= n0
                yield eml0, read0, f0
            if stop_event.is_set():
                break
        while ahead:
            eml0, read0, f0, _n = ahead.popleft()
            yield eml0, read0, f0
    finally:
        for _e, _r, f, _n in ahead:
            f.cancel()


def export_mailbox(mailbox_dir: Path, pst_path: Path, display_name: str,
                   cb, stop_event, password: str = "", split_gb: float = DEFAULT_SPLIT_GB):
    """cb: log(str), progress(done, total), stage(str) — необязательно.
    Возвращает (успешно, ошибок, [файлы PST], путь к отчёту)."""
    err = check_password(password)
    if err:
        raise PstExportError(err)
    split_bytes = int(min(max(float(split_gb), MIN_SPLIT_GB), MAX_SPLIT_GB) * GB)
    tasks = collect_tasks(mailbox_dir)
    total = sum(len(t[2]) for t in tasks)
    if not tasks:
        raise PstExportError(tr('В архиве нет писем для этого ящика — сначала выполните загрузку'))
    pst_path.parent.mkdir(parents=True, exist_ok=True)

    stage = getattr(cb, "stage", None) or (lambda _t: None)
    files = []
    parts = []
    state = {"raw": 0}

    def open_part():
        n = len(files) + 1
        path = pst_path if n == 1 else pst_path.with_name(
            tr('{0}_часть{n}{1}').format(pst_path.stem, pst_path.suffix, n=n))
        name = display_name if n == 1 else f"{display_name} ({n})"
        state["pst"] = PstFile(path, name, password)
        state["folders"] = {}
        state["raw"] = 0
        state["base"] = state["pst"].size()
        files.append(path)
        parts.append(_PartStats(path))
        if n > 1:
            cb.log(tr('Часть {0} достигла лимита {1} ГБ — начата часть {n}: {2}').format(n - 1, _fmt_gb(split_bytes / GB), path.name, n=n))

    def close_part():
        state["pst"].close()
        state["pst"] = None         # закрытую часть abort() уже не трогает
        p = parts[-1]
        p.size = p.path.stat().st_size
        if p.size > split_bytes:
            cb.log(tr('Часть {0} ({1}) больше лимита: в ней есть письмо крупнее заданного размера части').format(len(parts), _fmt_size(p.size)))

    def folder_nid(segs):
        cache = state["folders"]
        parent = NID_IPM_SUBTREE
        for i, seg in enumerate(segs):
            key = tuple(segs[:i + 1])
            if key not in cache:
                cache[key] = state["pst"].add_folder(parent, _pst_folder_name(seg, i == 0))
            parent = cache[key]
        return parent

    def needs_new_part(raw_next: int) -> bool:
        pst = state["pst"]
        if parts[-1].count == 0:
            return False
        grown = pst.size() - state["base"]
        ratio = max(1.0, grown / state["raw"]) if state["raw"] else 2.5
        est_next = int(raw_next * ratio * 1.5) + 65536
        return pst.size() + pst.close_overhead() + est_next > split_bytes

    open_part()
    ok = errors = done = 0
    cb.progress(0, total)
    pool = ThreadPoolExecutor(PARSE_THREADS, thread_name_prefix="parse") if PARALLEL_PARSE else None
    try:
        for info, fdir, emls in tasks:
            if stop_event.is_set():
                break
            delim = info.get("delimiter") or ""
            segs = [s for s in (info["display"].split(delim) if delim else [info["display"]]) if s]
            label = " / ".join(_pst_folder_name(sg, i == 0) for i, sg in enumerate(segs))
            folder_nid(segs)
            cb.log(tr('Папка «{0}»: {1} писем').format(info['display'], _fmt_n(len(emls))))
            flags = load_flags(fdir)
            for eml, read, parsed in _parse_ahead(pool, emls, flags, stop_event):
                if stop_event.is_set():
                    break
                try:
                    st = eml.stat()
                    raw = msgfile.raw_size(eml, st)
                except OSError:
                    st, raw = None, 0
                if needs_new_part(raw):
                    close_part()
                    open_part()
                try:
                    if parsed is not None:
                        received = state["pst"].add_parsed(folder_nid(segs), parsed.result())
                    elif NATIVE:
                        received = state["pst"].add_eml(folder_nid(segs), eml, read)
                    else:
                        msg = eml_to_message(eml, read)
                        state["pst"].add_message(folder_nid(segs), msg)
                        received = msg["received"]
                    state["raw"] += raw
                    # в отчёт — та же дата получения, что записана в PST
                    parts[-1].add(label, received or (st.st_mtime if st else 0))
                    ok += 1
                except Exception as e:
                    errors += 1
                    cb.log(tr('Ошибка в письме {0}: {e}').format(eml.name, e=e))
                done += 1
                cb.progress(done, total)
        stage(tr('Запись структуры PST…'))
        cb.log(tr('Запись структуры PST…'))
        close_part()
    except BaseException:
        if state.get("pst") is not None:
            state["pst"].abort()
        raise
    finally:
        if pool is not None:
            pool.shutdown(cancel_futures=True)

    for path in files:
        stage(tr('Проверка файла {0}…').format(path.name))
        cb.log(tr('Проверка файла {0}…').format(path.name))
        try:
            summary = verify(path)
            n = sum(v for k, v in summary.items() if not k.startswith("_"))
            cb.log(tr('Проверка пройдена: {0} писем, структура PST корректна').format(_fmt_n(n))
                   + (tr(', установлен пароль') if summary["_password"] else ""))
        except Exception as e:      # PstCheckError, а также ошибки чтения файла
            errors += 1
            cb.log(tr('ВНИМАНИЕ: самопроверка файла {0} не пройдена: {e}').format(path.name, e=e))

    report = write_report(pst_path.with_name(tr('{0}_отчёт.txt').format(pst_path.stem)), display_name,
                          parts, split_bytes, bool(password), errors, stop_event.is_set())
    for i, p in enumerate(parts, 1):
        cb.log(tr('Часть {i}: {0} — {1}, {2} писем, {3}').format(p.path.name, _fmt_size(p.size), _fmt_n(p.count), _period(p.lo, p.hi), i=i))
    cb.log(tr('Отчёт: {report}').format(report=report))
    return ok, errors, files, report
