"""Индекс писем архива для просмотрщика (кэшируется в index.json каждой папки)."""
import json
import os
from pathlib import Path

from .mailparse import summarize
from .storage import MailboxArchive

INDEX_VERSION = 2


def list_mailboxes(archive_root) -> list[tuple[str, Path]]:
    """[(имя ящика, папка ящика)] — все ящики, по которым есть загруженные письма."""
    root = Path(archive_root)
    out = []
    if root.exists():
        for d in sorted(root.iterdir()):
            if (d / MailboxArchive.META).exists():
                out.append((d.name, d))
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
        count = sum(1 for _ in fdir.glob("*.eml")) if fdir.exists() else 0
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
    except Exception:
        pass
    rows, items, changed = [], {}, False
    for p in fdir.glob("*.eml"):
        if stop is not None and stop():
            return []
        try:
            st = p.stat()
            c = cache.get(p.name)
            if c and c.get("size") == st.st_size and c.get("mtime") == int(st.st_mtime):
                row = c
            else:
                row = summarize(p, st)
                changed = True
        except Exception as e:
            row = {"file": p.name, "from": "", "from_name": "", "from_addr": "", "to": "",
                   "cc": "", "subject": f"<не удалось прочитать: {e}>", "msgid": "",
                   "sent": None, "received": 0, "size": 0, "mtime": 0, "attach": False}
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
