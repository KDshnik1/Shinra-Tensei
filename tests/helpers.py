"""Общее для тестов: тестовый архив ящика, Python-режим ядра, содержимое PST."""
import contextlib
import json
import os
import random
import shutil
from email.message import EmailMessage
from pathlib import Path

from core import mailparse

DATA = Path(__file__).parent / "data"
WORDS = "привет мир почта отчёт hello world invoice meeting draft report quarterly".split()
PURE = bool(os.environ.get("SHINRA_PURE_PYTHON"))

try:
    import shinra_core
except ImportError:
    shinra_core = None


def native_or_skip(test):
    """Сравнение Rust и Python: нужен собранный модуль; в Python-режиме не повторяем."""
    import unittest
    if shinra_core is None or PURE:
        return unittest.skip("нет нативного модуля или включён Python-режим")(test)
    return test


@contextlib.contextmanager
def pure_parse():
    """Разбор писем на Python, даже если нативный модуль загружен."""
    old = mailparse.NATIVE, mailparse.NATIVE_TEXT, mailparse.NATIVE_BODY
    mailparse.NATIVE = mailparse.NATIVE_TEXT = mailparse.NATIVE_BODY = False
    try:
        yield
    finally:
        mailparse.NATIVE, mailparse.NATIVE_TEXT, mailparse.NATIVE_BODY = old


def gen_message(rnd: random.Random, i: int, fi: int = 0, attach: bool = False) -> bytes:
    m = EmailMessage()
    m["From"] = f"Иван Петров <ivan{i}@example.com>"
    m["To"] = ", ".join(f"user{j}@example.org" for j in range(rnd.randint(1, 4)))
    m["Cc"] = "boss@example.org"
    m["Subject"] = " ".join(rnd.choice(WORDS) for _ in range(6))
    m["Date"] = f"Mon, {1 + i % 28:02d} Sep 2025 10:{i % 60:02d}:00 +0300"
    m["Message-ID"] = f"<{fi}.{i}@example.com>"
    text = "\n".join(" ".join(rnd.choice(WORDS) for _ in range(12))
                     for _ in range(rnd.randint(2, 30)))
    m.set_content(text)
    m.add_alternative("<html><body><p>" + text.replace("\n", "</p><p>") + "</p></body></html>",
                      subtype="html")
    if attach:
        m.add_attachment(rnd.randbytes(rnd.randint(1000, 200_000)), maintype="application",
                         subtype="octet-stream", filename=f"файл{i}.bin")
    return bytes(m)


def make_mailbox(root: Path, n: int = 30, with_data: bool = True, all_mail: bool = False) -> Path:
    """Архив ящика как после загрузки: folders.json, папки с .eml и flags.txt.
    with_data — ещё папка «Tricky» с трудными письмами из tests/data.
    all_mail — папка «[Gmail]/All Mail» (\\All) с копиями всех писем INBOX и Sent и одним своим."""
    rnd = random.Random(1)
    mb = root / "test@example.com"
    meta, files = {}, {}
    for fi, folder in enumerate(["INBOX", "INBOX/Работа", "Sent"]):
        files[folder] = {f"1_{i}.eml": gen_message(rnd, i, fi, attach=i % 5 == 0)
                         for i in range(n // 3)}
    if all_mail:
        copies = {}
        for folder in ("INBOX", "Sent"):
            for b in files[folder].values():
                copies[f"2_{len(copies)}.eml"] = b
        copies["2_own.eml"] = gen_message(rnd, 999, 9)
        files["[Gmail]/All Mail"] = copies
    for folder, msgs in files.items():
        d = mb.joinpath(*folder.split("/"))
        d.mkdir(parents=True, exist_ok=True)
        meta[folder] = {"display": folder, "delimiter": "/", "raw": folder,
                        "flags": ["\\All"] if folder.startswith("[Gmail]") else []}
        flags = []
        for k, (name, data) in enumerate(msgs.items()):
            p = d / name
            p.write_bytes(data)
            os.utime(p, (1_700_000_000 + k * 60,) * 2)
            seen = "\\Seen" if k % 2 else ""
            flags.append(f"{name[:-4]}\t{seen}")
        (d / "flags.txt").write_text("\n".join(flags) + "\n", encoding="utf-8")
    if with_data:
        d = mb / "Tricky"
        d.mkdir(parents=True, exist_ok=True)
        for k, src in enumerate(sorted(DATA.iterdir())):
            ext = ".eml.gz" if src.name.endswith(".gz") else ".eml"
            shutil.copyfile(src, d / f"3_{k}{ext}")
        meta["Tricky"] = {"display": "Tricky", "delimiter": "/", "raw": "Tricky", "flags": []}
    (mb / "folders.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return mb


RANDOM_TAGS = {0x300B0102, 0x0FF90102, 0x0FFF0102}   # случайные ключи и идентификаторы


def dump_pst(path) -> list:
    """Содержимое PST по письмам (папка, свойства, получатели, вложения) без случайных ключей."""
    from core.pst_reader import PstReader
    r = PstReader(path)
    out = []

    def clean(d):
        return sorted((k, v) for k, v in d.items() if k not in RANDOM_TAGS)

    def walk(nid, prefix):
        for child, _ in r.node_tc((nid & ~0x1F) | 0x0D):
            name = r.node_pc(child).get(0x3001001F, "?")
            for mid, _ in r.node_tc((child & ~0x1F) | 0x0E):
                d, s, _par = r.node(mid)
                subs = r.subnodes(s)
                rec = [clean(row) for _, row in r.tc(*subs[0x692])] if 0x692 in subs else []
                att = [clean(r.pc(*subs[a])) for a, _ in r.tc(*subs[0x671])] if 0x671 in subs else []
                out.append((prefix + name, clean(r.pc(d, s)), rec, att))
            walk(child, prefix + name + "/")
    try:
        walk(0x122, "")
    finally:
        r.f.close()
    return out
