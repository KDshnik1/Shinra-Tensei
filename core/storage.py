"""Локальный архив: каждое письмо — отдельный .eml или сжатый .eml.gz.

Структура:
  <архив>/<email>/folders.json                 — соответствие папок диска и сервера
  <архив>/<email>/<Папка>/<Подпапка>/<uidvalidity>_<uid>.eml[.gz]
  <архив>/<email>/<Папка>/flags.txt            — флаги писем (прочитано и т.п.)

Повторный запуск докачивает только новые письма.
"""
import json
import os
import threading
import time
from pathlib import Path

from . import mailparse, msgfile

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


# Папки, где лежат копии писем из других папок: в Gmail — «Вся почта» (флаг \All по RFC 6154).
# Старые архивы флагов не хранят — тогда узнаём папку по имени.
_ALL_MAIL_NAMES = {"[gmail]/all mail", "[google mail]/all mail", "[gmail]/вся почта",
                   "[google mail]/вся почта"}


def is_all_mail(info) -> bool:
    """info — запись folders.json (или папка с .flags/.display)."""
    flags = info.get("flags", []) if isinstance(info, dict) else getattr(info, "flags", [])
    display = info.get("display", "") if isinstance(info, dict) else getattr(info, "display", "")
    return "\\all" in [f.lower() for f in flags] or display.lower() in _ALL_MAIL_NAMES


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
        info = {"display": folder.display, "delimiter": folder.delimiter, "raw": folder.raw,
                "flags": list(folder.flags)}
        with self._lock:
            meta = self.load_meta()
            if meta.get(rel) != info:
                meta[rel] = info
                self._save_meta(meta)
        d = self.dir.joinpath(*rel.split("/"))
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def message_key(uidvalidity: str, uid: str) -> str:
        return f"{uidvalidity}_{uid}"

    def save_message(self, fdir: Path, uidvalidity: str, uid: str,
                     body: bytes, flags: str, idate, compress: bool = False):
        ts = None
        if idate:
            try:
                ts = float(idate) if isinstance(idate, (int, float)) else time.mktime(idate)
            except Exception:
                ts = None
        if ts is None:
            # сервер не дал INTERNALDATE: дата из заголовков, а не момент загрузки
            ts = mailparse.received_from_bytes(body)
        msgfile.write_message(fdir / self.message_key(uidvalidity, uid), body, compress, ts)
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
