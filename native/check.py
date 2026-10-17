"""Сверка нативного ядра (Rust) с Python-версией на настоящем архиве.

    python native/check.py "<архив>\\<e-mail>" [--limit 500]

1. Каждое письмо разбирается обеими версиями, свойства для PST сравниваются.
   Вложенные письма (message/rfc822) Rust сохраняет байт в байт, а Python
   пересобирает заново, поэтому их данные считаются отдельно, а не ошибкой.
2. Одни и те же письма записываются в PST обоими писателями — файлы
   должны совпасть побайтно.
3. Файл проверяется обеими версиями самопроверки.
"""
import argparse
import os
import random
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shinra_core  # noqa: E402
from core import pst_export, pst_reader, pst_writer  # noqa: E402

RANDOM_TAGS = {0x300B0102, 0x0FF90102}  # случайные ключи поиска и идентификаторы


def _clean(p):
    return {k: v for k, v in p.items() if k not in RANDOM_TAGS}


def compare_message(a, b):
    """Список отличий и число вложенных писем с иным текстом (ожидаемо)."""
    out, forwarded = [], 0
    pa, pb = _clean(a["props"]), _clean(b["props"])
    for k in sorted(set(pa) | set(pb)):
        if pa.get(k) != pb.get(k):
            out.append(f"0x{k:08X}: {pa.get(k)!r:.80} != {pb.get(k)!r:.80}")
    if a.get("received") != b.get("received"):
        out.append(f"received: {a.get('received')} != {b.get('received')}")
    if len(a["recipients"]) != len(b["recipients"]):
        out.append(f"recipients: {len(a['recipients'])} != {len(b['recipients'])}")
    for x, y in zip(a["recipients"], b["recipients"]):
        if _clean(x) != _clean(y):
            out.append(f"recipient: {_clean(x)!r:.80} != {_clean(y)!r:.80}")
    if len(a["attachments"]) != len(b["attachments"]):
        out.append(f"attachments: {len(a['attachments'])} != {len(b['attachments'])}")
    for x, y in zip(a["attachments"], b["attachments"]):
        xp, yp = dict(x["props"]), dict(y["props"])
        if xp.get(0x370E001F) == "message/rfc822" and xp.get(0x37010102) != yp.get(0x37010102):
            forwarded += 1
            for p in (xp, yp, x["row"], y["row"]):
                p.pop(0x37010102, None)
                p.pop(0x0E200003, None)
        if xp != yp or x["row"] != y["row"]:
            out.append(f"attachment {xp.get(0x3001001F)!r}")
    return out, forwarded


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mailbox", type=Path, help="папка ящика в архиве (с folders.json)")
    ap.add_argument("--limit", type=int, default=0, help="не больше N писем на папку")
    args = ap.parse_args()

    shinra_core.set_translator(pst_writer.tr)
    tasks = pst_export.collect_tasks(args.mailbox)
    if not tasks:
        sys.exit("нет folders.json или писем в этой папке")
    emls = [(info["display"], e) for info, _fdir, files in tasks
            for e in (files[:args.limit] if args.limit else files)]
    print(f"Писем: {len(emls)}")

    # 1. разбор писем
    rnd = random.Random(1)
    os.urandom = lambda n: bytes(rnd.getrandbits(8) for _ in range(n))
    bad = forwarded = 0
    msgs = []
    t_py = t_rs = 0.0
    for folder, eml in emls:
        t = time.perf_counter()
        try:
            a = pst_export.eml_to_message(eml, False)
        except Exception as e:
            a = e
        t_py += time.perf_counter() - t
        t = time.perf_counter()
        try:
            b = shinra_core.eml_to_message(eml, False)
        except Exception as e:
            b = e
        t_rs += time.perf_counter() - t
        if isinstance(a, Exception) or isinstance(b, Exception):
            if type(a) is not type(b):
                bad += 1
                print(f"ОТЛИЧИЕ {eml}: python={a!r:.100} rust={b!r:.100}")
            continue
        diffs, fw = compare_message(a, b)
        forwarded += fw
        if diffs:
            bad += 1
            print(f"ОТЛИЧИЕ {eml}:\n  " + "\n  ".join(diffs[:8]))
        msgs.append((folder, a))
    print(f"1. Разбор: отличий {bad} из {len(emls)}; вложенных писем с исходными байтами: {forwarded}. "
          f"Python {t_py:.1f} с, Rust {t_rs:.1f} с")

    # 2. запись PST
    uid = uuid.uuid4().bytes
    real_uuid4 = uuid.uuid4
    uuid.uuid4 = lambda: uuid.UUID(bytes=uid)
    tmp = Path(tempfile.mkdtemp(prefix="shinra_check_"))

    def write(cls, path, **kw):
        t = time.perf_counter()
        pst = cls(path, "check", "", **kw)
        folders = {}
        for name, m in msgs:
            if name not in folders:
                folders[name] = pst.add_folder(pst_writer.NID_IPM_SUBTREE, name)
            pst.add_message(folders[name], m)
        pst.close()
        return time.perf_counter() - t

    tp = write(pst_writer.PyPstFile, tmp / "python.pst")
    tr_ = write(shinra_core.PstFile, tmp / "rust.pst", uid=uid)
    uuid.uuid4 = real_uuid4
    same = (tmp / "python.pst").read_bytes() == (tmp / "rust.pst").read_bytes()
    print(f"2. Запись PST: файлы {'совпадают побайтно' if same else 'РАЗЛИЧАЮТСЯ'}. "
          f"Python {tp:.1f} с, Rust {tr_:.1f} с (здесь Rust получает письма из Python и тратит время "
          f"на перевод данных; при экспорте он разбирает .eml сам, и запись быстрее)")

    # 3. самопроверка
    t = time.perf_counter()
    a = pst_reader._py_verify(tmp / "rust.pst")
    tvp = time.perf_counter() - t
    t = time.perf_counter()
    b = shinra_core.verify(tmp / "rust.pst")
    tvr = time.perf_counter() - t
    print(f"3. Самопроверка: {'результаты совпадают' if a == b else 'РЕЗУЛЬТАТЫ РАЗНЫЕ'}. "
          f"Python {tvp:.1f} с, Rust {tvr:.1f} с")
    for p in tmp.iterdir():
        p.unlink()
    tmp.rmdir()
    sys.exit(0 if same and a == b and not bad else 1)


if __name__ == "__main__":
    main()
