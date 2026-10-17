"""Индекс писем архива для просмотрщика (кэшируется в index.json каждой папки)."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import mailparse, msgfile
from .mailparse import summarize
from .storage import MailboxArchive
from .i18n import tr

INDEX_VERSION = 5          # 5: дата получения по заголовкам Received/Date


def list_mailboxes(archive_root) -> list[tuple[str, Path]]:
    """[(имя ящика, папка ящика)] — все ящики, по которым есть загруженные письма."""
    root = Path(archive_root)
    out = []
    if root.exists():
        for d in sorted(root.iterdir()):
            if (d / MailboxArchive.META).exists():
                out.append((d.name, d))
    return out


def all_mailboxes(cfg) -> list[tuple[str, Path]]:
    """Ящики из общей папки архива + ящики со своей папкой (без повторов)."""
    out, seen = [], set()

    def add(name, d):
        try:
            key = os.path.normcase(str(Path(d).resolve()))
        except OSError:
            key = os.path.normcase(str(d))
        if key not in seen and (Path(d) / MailboxArchive.META).exists():
            seen.add(key)
            out.append((name, Path(d)))

    for name, d in list_mailboxes(cfg.archive_dir):
        add(name, d)
    for a in cfg.accounts:
        if (a.archive_dir or "").strip():
            add(a.email.strip().lower(), cfg.mailbox_dir(a))
    out.sort(key=lambda x: x[0])
    return out


def mailbox_folders(mailbox_dir: Path) -> list[dict]:
    """[{rel, display, delimiter, dir, count}] по folders.json."""
    try:
        meta = json.loads((mailbox_dir / MailboxArchive.META).read_text(encoding="utf-8"))
    except Exception:
        return []
    res = []
    for rel, info in meta.items():
        fdir = mailbox_dir.joinpath(*rel.split("/"))
        count = len(msgfile.list_messages(fdir))
        res.append({"rel": rel, "display": info.get("display", rel),
                    "delimiter": info.get("delimiter", "/"), "dir": fdir, "count": count})
    res.sort(key=lambda f: (f["display"].upper() != "INBOX", f["display"].lower()))
    return res


def load_folder(fdir: Path, folder_display: str, stop=None) -> list[dict]:
    idx_path = fdir / "index.json"
    cache = {}
    try:
        data = json.loads(idx_path.read_text(encoding="utf-8"))
        if data.get("v") == INDEX_VERSION:     # индекс другой версии строится заново
            cache = data.get("items", {})
    except Exception:
        pass
    files = msgfile.scan_messages(fdir)
    todo = [(Path(path), st) for name, path, st in files
            if not ((c := cache.get(name)) and c.get("fsize") == st.st_size
                    and c.get("mtime") == int(st.st_mtime))]
    fresh = _summarize_all(todo, stop)
    if fresh is None:
        return []
    changed = bool(todo)
    rows, items = [], {}
    for name, path, _st in files:
        row = fresh.get(name) or cache[name]
        items[name] = row
        rows.append(dict(row, folder=folder_display, path=path))
    if changed or len(items) != len(cache):
        try:
            tmp = idx_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"v": INDEX_VERSION, "items": items}, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, idx_path)
        except Exception:
            pass
    return rows


def _summary_or_error(p: Path, st) -> dict:
    try:
        return summarize(p, st)
    except Exception as e:
        return {"file": p.name, "from": "", "from_name": "", "from_addr": "", "to": "",
                "cc": "", "subject": tr('<не удалось прочитать: {e}>').format(e=e), "msgid": "",
                "sent": None, "received": 0, "size": 0, "fsize": -1, "mtime": 0,
                "attach": 0, "att_names": []}


def _summarize_all(todo, stop) -> dict | None:
    """{имя файла: сведения} для новых и изменённых писем; None — если остановили.
    С нативным ядром — в несколько потоков (разбор идёт без GIL)."""
    out = {}
    if not mailparse.NATIVE or len(todo) < 64:
        for p, st in todo:
            if stop is not None and stop():
                return None
            out[p.name] = _summary_or_error(p, st)
        return out
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as ex:
        futures = [(p.name, ex.submit(_summary_or_error, p, st)) for p, st in todo]
        for name, f in futures:
            if stop is not None and stop():
                for _n, g in futures:
                    g.cancel()
                return None
            out[name] = f.result()
    return out


def rename_in_index(fdir: Path, changes: dict):
    """changes: {старое имя: (новое имя, новый размер файла)} — после сжатия писем,
    чтобы не разбирать их заново."""
    idx_path = fdir / "index.json"
    try:
        data = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if data.get("v") != INDEX_VERSION:
        return
    items = data.get("items", {})
    for old, (new, fsize) in changes.items():
        it = items.pop(old, None)
        if it is None:
            continue
        it.setdefault("size", it.get("fsize"))
        it["file"], it["fsize"] = new, fsize
        items[new] = it
    for it in items.values():
        it.setdefault("fsize", it.get("size"))
    data["v"] = INDEX_VERSION
    try:
        tmp = idx_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, idx_path)
    except Exception:
        pass
