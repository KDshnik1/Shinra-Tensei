"""Индекс писем архива для просмотрщика (кэшируется в index.json каждой папки)."""
import gzip
import json
import os
from bisect import bisect_right
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
    return load_folders([(fdir, folder_display)], stop)


def _scan_folder(fdir: Path):
    """(путь index.json, кэш, [(имя, путь, stat)], [(путь, stat)] для разбора)."""
    idx_path = fdir / "index.json"
    cache = {}
    try:
        data = json.loads(idx_path.read_text(encoding="utf-8"))
        if data.get("v") == INDEX_VERSION:     # индекс другой версии строится заново
            cache = data.get("items", {})
    except Exception:
        pass
    files = msgfile.scan_messages(fdir)
    todo = [(path, st) for name, path, st in files
            if not ((c := cache.get(name)) and c.get("fsize") == st.st_size
                    and c.get("mtime") == int(st.st_mtime))]
    return idx_path, cache, files, todo


def load_folders(folders, stop=None) -> list[dict]:
    """Строки просмотрщика для нескольких папок: folders — [(папка, имя для показа)].
    Новые письма всех папок разбираются одним пулом потоков — так ящик из множества
    небольших папок индексируется быстрее, чем папка за папкой."""
    # чтение папок — по очереди: в потоках оно медленнее (разбор index.json держит GIL)
    scans = []
    for f in folders:
        if stop is not None and stop():     # выбрали другую папку — эта загрузка не нужна
            return []
        scans.append(_scan_folder(Path(f[0])))
    if stop is not None and stop():
        return []
    todo = [t for sc in scans for t in sc[3]]
    fresh = _summarize_all(todo, stop)
    if fresh is None:
        return []
    rows = []
    for (_fdir, display), (idx_path, cache, files, part_todo) in zip(folders, scans):
        items = {}
        for name, path, _st in files:
            row = fresh.get(path) or cache[name]
            items[name] = row
            rows.append(dict(row, folder=display, path=path))
        if part_todo or len(items) != len(cache):
            try:
                tmp = idx_path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"v": INDEX_VERSION, "items": items}, ensure_ascii=False),
                               encoding="utf-8")
                os.replace(tmp, idx_path)
            except Exception:
                pass
    return rows


def _summary_or_error(path: str, st) -> dict:
    p = Path(path)
    try:
        return summarize(p, st)
    except Exception as e:
        return {"file": p.name, "from": "", "from_name": "", "from_addr": "", "to": "",
                "cc": "", "subject": tr('<не удалось прочитать: {e}>').format(e=e), "msgid": "",
                "sent": None, "received": 0, "size": 0, "fsize": -1, "mtime": 0,
                "attach": 0, "att_names": []}


def _summarize_all(todo, stop) -> dict | None:
    """{путь письма: сведения} для новых и изменённых писем; None — если остановили.
    С нативным ядром — в несколько потоков (разбор идёт без GIL)."""
    out = {}
    if not mailparse.NATIVE or len(todo) < 64:
        for p, st in todo:
            if stop is not None and stop():
                return None
            out[p] = _summary_or_error(p, st)
        return out
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as ex:
        futures = [(p, ex.submit(_summary_or_error, p, st)) for p, st in todo]
        for name, f in futures:
            if stop is not None and stop():
                for _n, g in futures:
                    g.cancel()
                return None
            out[name] = f.result()
    return out


TEXT_INDEX = "text_index.gz"
TEXT_VERSION = 1
TEXT_MAX = 100_000          # символов текста одного письма для поиска
_SEP = b"\x1e"              # между текстами писем; str.split() считает его пробелом,
                            # поэтому ни в тексте, ни в запросе его нет


def norm_text(t: str) -> str:
    """Нижний регистр и одиночные пробелы: фраза находится и через перенос строки."""
    return " ".join(t[:TEXT_MAX].split()).lower()


def _text_or_empty(path: str) -> bytes:
    try:
        return norm_text(mailparse.search_text(path)).encode("utf-8")
    except Exception:
        return b""


class TextIndex:
    """Тексты писем для поиска: по папке один блок байт UTF-8 и начала писем в нём.
    Поиск — bytes.find по блоку, без разбора на отдельные строки."""

    def __init__(self):
        self.parts = []             # (блок, [начало письма], [путь письма])

    def add(self, blob: bytes, sizes, paths):
        starts, pos = [], 0
        for n in sizes:
            starts.append(pos)
            pos += n + len(_SEP)
        self.parts.append((blob, starts, list(paths)))

    def search(self, text: str) -> set:
        """Пути писем, в тексте которых есть text."""
        q = norm_text(text).encode("utf-8")
        hits = set()
        if not q:
            return hits
        for blob, starts, paths in self.parts:
            pos = blob.find(q)
            while pos != -1:
                i = bisect_right(starts, pos) - 1
                hits.add(paths[i])
                if i + 1 >= len(starts):
                    break
                pos = blob.find(q, starts[i + 1])      # дальше — со следующего письма
        return hits


def _read_texts(d: str):
    """(блок, {имя файла: (размер, mtime, начало, длина)}) из text_index.gz папки.
    Формат: строка JSON со списком [имя, размер, mtime, длина текста в байтах],
    затем тексты через _SEP."""
    try:
        with gzip.open(os.path.join(d, TEXT_INDEX), "rb") as fh:
            meta = json.loads(fh.readline())
            blob = fh.read()
        if meta.get("v") != TEXT_VERSION:
            return b"", {}
        items, pos = {}, 0
        for name, fsize, mtime, n in meta["items"]:
            items[name] = (fsize, mtime, pos, n)
            pos += n + len(_SEP)
        if pos - len(_SEP) != len(blob) and items:
            return b"", {}
        return blob, items
    except Exception:
        return b"", {}


def _write_texts(d: str, names, metas, texts) -> bytes:
    blob = _SEP.join(texts)
    try:
        tmp = os.path.join(d, TEXT_INDEX + ".tmp")
        meta = {"v": TEXT_VERSION,
                "items": [[k, m[0], m[1], len(t)] for k, m, t in zip(names, metas, texts)]}
        with gzip.open(tmp, "wb", compresslevel=1) as fh:
            fh.write(json.dumps(meta, ensure_ascii=False).encode("utf-8") + b"\n")
            fh.write(blob)
        os.replace(tmp, os.path.join(d, TEXT_INDEX))
    except Exception:
        pass
    return blob


def load_texts(rows, stop=None, progress=None) -> TextIndex | None:
    """Тексты писем из строк просмотрщика для поиска. Кэшируются в text_index.gz каждой
    папки; заново разбираются только новые и изменённые письма (в несколько потоков).
    progress(сделано, всего) — по ходу разбора. None — если остановили."""
    by_dir = {}
    for r in rows:
        by_dir.setdefault(os.path.dirname(r["path"]), []).append(r)
    caches, todo = {}, []
    for d, rs in by_dir.items():
        blob, items = caches[d] = _read_texts(d)
        for r in rs:
            c = items.get(r["file"])
            if not (c and c[0] == r.get("fsize") and c[1] == r.get("mtime")):
                todo.append(r)
    fresh = {}
    total = len(todo)
    workers = min(8, os.cpu_count() or 2) if mailparse.NATIVE_TEXT else 1
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [(r, ex.submit(_text_or_empty, r["path"])) for r in todo]
        for n, (r, f) in enumerate(futures, 1):
            if stop is not None and stop():
                for _r, g in futures:
                    g.cancel()
                return None
            fresh[r["path"]] = f.result()
            if progress is not None and (n % 200 == 0 or n == total):
                progress(n, total)
    index = TextIndex()
    for d, rs in by_dir.items():
        blob, items = caches[d]
        if len(rs) == len(items) and not any(r["path"] in fresh for r in rs):
            # папка не менялась: блок из файла как есть, в его порядке
            order = sorted(items.items(), key=lambda kv: kv[1][2])
            index.add(blob, [m[3] for _k, m in order], [os.path.join(d, k) for k, _m in order])
            continue
        names, metas, texts = [], [], []
        for r in rs:
            t = fresh.get(r["path"])
            if t is None:
                _fs, _mt, start, n = items[r["file"]]
                t = blob[start:start + n]
            names.append(r["file"])
            metas.append((r.get("fsize"), r.get("mtime")))
            texts.append(t)
        index.add(_write_texts(d, names, metas, texts), [len(t) for t in texts],
                  [r["path"] for r in rs])
    return index


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
