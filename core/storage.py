"""Локальный архив: каждое письмо — отдельный .eml.

Структура:
  <архив>/<email>/folders.json                 — соответствие папок диска и сервера
  <архив>/<email>/<Папка>/<Подпапка>/<uidvalidity>_<uid>.eml
  <архив>/<email>/<Папка>/flags.txt            — флаги писем (прочитано и т.п.)

Повторный запуск докачивает только новые письма.
"""
import json
import os
import threading
import time
from pathlib import Path

_INVALID = '<>:"/\\|?*'
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def safe_name(s: str, maxlen: int = 60) -> str:
    out = "".join("_" if (c in _INVALID or ord(c) < 32) else c for c in s).strip(" .")
    if not out:
        out = "_"
    if out.upper() in _RESERVED:
        out = "_" + out
    return out[:maxlen]


class MailboxArchive:
    META = "folders.json"

    def __init__(self, root, email: str):
        self.dir = Path(root) / safe_name(email.strip().lower(), 100)
        self._lock = threading.Lock()

    def load_meta(self) -> dict:
        p = self.dir / self.META
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_meta(self, meta: dict):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / (self.META + ".tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.dir / self.META)

    def folder_dir(self, folder) -> Path:
        segs = folder.display.split(folder.delimiter) if folder.delimiter else [folder.display]
        rel = "/".join(safe_name(s) for s in segs if s) or "_"
        info = {"display": folder.display, "delimiter": folder.delimiter, "raw": folder.raw}
        with self._lock:
            meta = self.load_meta()
            if meta.get(rel) != info:
                meta[rel] = info
                self._save_meta(meta)
        d = self.dir.joinpath(*rel.split("/"))
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def message_path(fdir: Path, uidvalidity: str, uid: str) -> Path:
        return fdir / f"{uidvalidity}_{uid}.eml"

    def save_message(self, fdir: Path, uidvalidity: str, uid: str,
                     body: bytes, flags: str, idate):
        path = self.message_path(fdir, uidvalidity, uid)
        tmp = path.with_name(path.name + ".part")
        with open(tmp, "wb") as fh:
            fh.write(body)
        os.replace(tmp, path)
        if idate:
            try:
                ts = time.mktime(idate)
                os.utime(path, (ts, ts))
            except Exception:
                pass
        with open(fdir / "flags.txt", "a", encoding="utf-8") as fh:
            fh.write(f"{uidvalidity}_{uid}\t{flags}\n")


def load_flags(fdir: Path) -> dict:
    flags = {}
    p = fdir / "flags.txt"
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, val = line.partition("\t")
            flags[key] = val
    return flags
