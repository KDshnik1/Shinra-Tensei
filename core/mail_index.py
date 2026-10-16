"""Индекс писем архива для просмотрщика (кэшируется в index.json каждой папки)."""
import json
import os
from pathlib import Path

from . import msgfile
from .mailparse import summarize
from .storage import MailboxArchive

INDEX_VERSION = 4          # 4: сжатые письма .eml.gz, размер файла отдельно


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
        if data.get("v") == INDEX_VERSION:
            cache = data.get("items", {})
        elif data.get("v") == 3:          # индекс прошлой версии: файлы были только .eml
            cache = data.get("items", {})
            for it in cache.values():
                it.setdefault("fsize", it.get("size"))
    except Exception:
        pass
    rows, items, changed = [], {}, False
    for p in msgfile.list_messages(fdir):
        if stop is not None and stop():
            return []
        try:
            st = p.stat()
            c = cache.get(p.name)
            if c and c.get("fsize") == st.st_size and c.get("mtime") == int(st.st_mtime):
                row = c
            else:
                row = summarize(p, st)
                changed = True
        except Exception as e:
            row = {"file": p.name, "from": "", "from_name": "", "from_addr": "", "to": "",
                   "cc": "", "subject": f"<не удалось прочитать: {e}>", "msgid": "",
                   "sent": None, "received": 0, "size": 0, "fsize": -1, "mtime": 0,
                   "attach": 0,
                   "att_names": []}
        items[p.name] = row
        rows.append(dict(row, folder=folder_display, path=str(p)))
    if changed or len(items) != len(cache):
        try:
            tmp = idx_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"v": INDEX_VERSION, "items": items}, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, idx_path)
        except Exception:
            pass
    return rows


def rename_in_index(fdir: Path, changes: dict):
    """changes: {старое имя: (новое имя, новый размер файла)} — после сжатия писем,
    чтобы не разбирать их заново."""
    idx_path = fdir / "index.json"
    try:
        data = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if data.get("v") not in (3, INDEX_VERSION):
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
