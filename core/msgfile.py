"""Файлы писем в архиве: обычные .eml и сжатые .eml.gz (gzip).

Все модули программы открывают письма только через эти функции, поэтому
в одной папке могут спокойно лежать письма в обоих форматах.
"""
import gzip
import os
import struct
import tempfile
import time
from pathlib import Path

PLAIN, PACKED = ".eml", ".eml.gz"
GZ_LEVEL = 6                 # баланс скорости и степени сжатия


def is_packed(p) -> bool:
    return str(p).lower().endswith(PACKED)


def is_message(p) -> bool:
    n = str(p).lower()
    return n.endswith(PLAIN) or n.endswith(PACKED)


def msg_key(p) -> str:
    """«1420483195_28295» для 1420483195_28295.eml и для 1420483195_28295.eml.gz."""
    name = Path(p).name
    low = name.lower()
    if low.endswith(PACKED):
        return name[:-len(PACKED)]
    if low.endswith(PLAIN):
        return name[:-len(PLAIN)]
    return name


def list_messages(fdir: Path) -> list:
    """Все письма папки (оба формата); если есть оба варианта одного письма — сжатый."""
    if not fdir.exists():
        return []
    found = {}
    with os.scandir(fdir) as it:
        for e in it:
            if not e.is_file():
                continue
            low = e.name.lower()
            if low.endswith(PACKED) or low.endswith(PLAIN):
                k = msg_key(e.name)
                if k not in found or low.endswith(PACKED):
                    found[k] = Path(e.path)
    return list(found.values())


def scan_messages(fdir: Path) -> list:
    """Как list_messages, но с os.stat каждого файла: [(имя, путь строкой, stat)]. В Windows
    сведения о файле приходят вместе со списком папки, отдельного обращения к диску не нужно.
    Пути строками, а не Path: на десятках тысяч писем это заметно быстрее."""
    if not fdir.exists():
        return []
    found = {}
    with os.scandir(fdir) as it:
        for e in it:
            low = e.name.lower()
            if not (low.endswith(PACKED) or low.endswith(PLAIN)):
                continue
            try:
                if not e.is_file():
                    continue
                st = e.stat()
            except OSError:
                continue
            packed = low.endswith(PACKED)
            k = e.name[:-len(PACKED if packed else PLAIN)]
            if packed or k not in found:
                found[k] = (e.name, e.path, st)
    return list(found.values())


def existing_keys(fdir: Path) -> set:
    return {msg_key(p) for p in list_messages(fdir)}


def open_message(p):
    """Файловый объект с исходным (несжатым) содержимым письма."""
    return gzip.open(p, "rb") if is_packed(p) else open(p, "rb")


def read_message(p) -> bytes:
    with open_message(p) as fh:
        return fh.read()


def raw_size(p, st=None) -> int:
    """Размер письма без сжатия (для .gz — из трейлера ISIZE, без распаковки)."""
    st = st or os.stat(p)
    if not is_packed(p):
        return st.st_size
    try:
        with open(p, "rb") as fh:
            fh.seek(-4, os.SEEK_END)
            return struct.unpack("<I", fh.read(4))[0]   # писем > 4 ГБ не бывает
    except OSError:
        return st.st_size


def write_message(path_without_ext: Path, body: bytes, compress: bool, mtime=None) -> Path:
    """Атомарно записать письмо; вернуть итоговый путь."""
    final = Path(str(path_without_ext) + (PACKED if compress else PLAIN))
    tmp = final.with_name(final.name + ".part")
    data = gzip.compress(body, GZ_LEVEL, mtime=0) if compress else body
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, final)
    if mtime is not None:
        try:
            os.utime(final, (mtime, mtime))
        except OSError:
            pass
    return final


def compress_file(p: Path) -> tuple:
    """.eml -> .eml.gz с сохранением даты получения. Возвращает (новый путь, было, стало)."""
    st = p.stat()
    with open(p, "rb") as fh:
        body = fh.read()
    new = write_message(p.with_name(msg_key(p)), body, True, st.st_mtime)
    new_size = new.stat().st_size
    try:
        p.unlink()
    except OSError:
        pass
    return new, st.st_size, new_size


def plain_copy(p) -> Path:
    """Путь к обычному .eml (для «Открыть в почтовой программе»): сжатое распаковывается
    во временную папку."""
    p = Path(p)
    if not is_packed(p):
        return p
    tmpdir = Path(tempfile.gettempdir()) / "Shinra Tensei"
    tmpdir.mkdir(parents=True, exist_ok=True)
    out = tmpdir / (msg_key(p) + PLAIN)
    out.write_bytes(read_message(p))
    # чистим старые временные копии (старше суток)
    now = time.time()
    for old in tmpdir.glob("*" + PLAIN):
        try:
            if now - old.stat().st_mtime > 86400 and old != out:
                old.unlink()
        except OSError:
            pass
    return out
