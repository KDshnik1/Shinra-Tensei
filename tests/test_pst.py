"""Запись PST: Rust и Python пишут одинаковые файлы, самопроверка ловит повреждения,
экспорт ящика и пропуск повторов писем."""
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from core import pst_export, pst_reader, pst_writer
from tests.helpers import dump_pst, make_mailbox, native_or_skip, pure_parse, shinra_core

ROOT = Path(__file__).resolve().parent.parent


class CB:
    def __init__(self):
        self.lines = []

    def log(self, s): self.lines.append(s)
    def progress(self, a, b): pass
    def stage(self, s): pass


def count(summary) -> int:
    """Писем в PST по сводке самопроверки (ключи «_…» — сведения о файле)."""
    return sum(v for k, v in summary.items() if not k.startswith("_"))


def export(mb: Path, out: Path, **kw):
    cb = CB()
    res = pst_export.export_mailbox(mb, out, "test", cb, threading.Event(), **kw)
    return res, cb


class PstTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="shinra-pst-"))
        cls.mb = make_mailbox(cls.tmp / "arch", n=45)
        tasks = pst_export.collect_tasks(cls.mb)
        with pure_parse():
            cls.msgs = [(info["display"], pst_export.eml_to_message(e, i % 2 == 0))
                        for info, _d, files in tasks for i, e in enumerate(files)]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def write(self, cls, path, **kw):
        pst = cls(path, "test", "", **kw)
        folders = {}
        for name, m in self.msgs:
            if name not in folders:
                folders[name] = pst.add_folder(pst_writer.NID_IPM_SUBTREE, name)
            pst.add_message(folders[name], m)
        pst.close()
        return path

    @native_or_skip
    def test_writers_byte_identical(self):
        uid = bytes(range(16))
        real = uuid.uuid4
        uuid.uuid4 = lambda: uuid.UUID(bytes=uid)
        try:
            a = self.write(pst_writer.PyPstFile, self.tmp / "py.pst")
        finally:
            uuid.uuid4 = real
        b = self.write(shinra_core.PstFile, self.tmp / "rs.pst", uid=uid)
        self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_verify(self):
        p = self.write(pst_writer.PstFile, self.tmp / "v.pst")
        summary = pst_reader.verify(p)
        self.assertEqual(count(summary), len(self.msgs))
        self.assertEqual(pst_reader._py_verify(p), summary)

    @native_or_skip
    def test_verify_fuzz(self):
        """Испорченный PST: обе самопроверки отвечают одинаково и только PstCheckError."""
        base = self.write(pst_writer.PyPstFile, self.tmp / "base.pst")
        good = base.read_bytes()
        r = pst_reader.PstReader(base)
        blocks, pages = list(r.bbt.items()), list(r.pages)
        r.f.close()
        rnd = random.Random(3)
        cur = self.tmp / "cur.pst"
        for i in range(150):
            d = bytearray(good)
            kind = rnd.choice(["block", "block", "page", "raw", "trunc"])
            if kind == "block":
                _bid, (ib, cb) = rnd.choice(blocks)
                if cb:
                    for _ in range(rnd.randint(1, 4)):
                        d[ib + rnd.randrange(cb)] = rnd.choice(
                            [0, 1, 2, 0xFF, 0x7F, 0x80, 0xEC, 0xB5, 0xBC, rnd.randrange(256)])
                    total = (cb + 16 + 63) // 64 * 64        # CRC верный — ловим смысл, а не CRC
                    struct.pack_into("<I", d, ib + total - 12, pst_writer.pst_crc(bytes(d[ib:ib + cb])))
            elif kind == "page":
                ib = rnd.choice(pages)
                for _ in range(rnd.randint(1, 3)):
                    d[ib + rnd.randrange(492)] = rnd.choice([0, 1, 0xFF, rnd.randrange(256)])
                struct.pack_into("<I", d, ib + 500, pst_writer.pst_crc(bytes(d[ib:ib + 496])))
            elif kind == "raw":
                for _ in range(rnd.randint(1, 3)):
                    d[rnd.randrange(len(d))] = rnd.randrange(256)
            else:
                d = d[:rnd.randrange(len(d))]
            cur.write_bytes(bytes(d))
            res = []
            for fn, err in ((pst_reader._py_verify, pst_reader.PstCheckError),
                            (shinra_core.verify, shinra_core.PstCheckError)):
                try:
                    res.append(("ok", fn(cur)))
                except err:
                    res.append(("check", None))
            self.assertEqual(res[0], res[1], f"искажение {i} ({kind})")

    def test_export_mailbox(self):
        (ok, errors, files, report), cb = export(self.mb, self.tmp / "exp" / "box.pst")
        self.assertEqual(errors, 0, cb.lines)
        self.assertEqual(ok, len(self.msgs))
        self.assertEqual(len(files), 1)
        self.assertTrue(Path(report).exists())
        self.assertEqual(count(pst_reader.verify(files[0])), len(self.msgs))

    @native_or_skip
    def test_export_native_equals_python(self):
        """Экспорт ящика на Rust и на Python (SHINRA_PURE_PYTHON=1) — те же письма в PST."""
        (_, _, files, _), _ = export(self.mb, self.tmp / "nat" / "box.pst")
        out = self.tmp / "pure" / "box.pst"
        code = ("import sys, threading; from pathlib import Path; from core import pst_export\n"
                "class CB:\n log = progress = stage = lambda self, *a: None\n"
                "r = pst_export.export_mailbox(Path(sys.argv[1]), Path(sys.argv[2]), 'test', CB(),"
                " threading.Event())\nprint(r[2][0])\n")
        env = dict(os.environ, SHINRA_PURE_PYTHON="1")
        res = subprocess.run([sys.executable, "-c", code, str(self.mb), str(out)], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=600)
        self.assertEqual(res.returncode, 0, res.stderr)
        a, b = dump_pst(files[0]), dump_pst(res.stdout.strip())
        self.assertEqual(len(a), len(self.msgs))
        for x, y in zip(a, b):
            self.assertEqual(x[:3], y[:3])
            # вложенные письма Rust кладёт исходными байтами — данные таких вложений не сравниваем
            for ax, ay in zip(x[3], y[3]):
                dx, dy = dict(ax), dict(ay)
                if dx.get(0x370E001F) == "message/rfc822" or dx.get(0x3707001F) == "a.zip":
                    for d_ in (dx, dy):
                        d_.pop(0x37010102, None)
                        d_.pop(0x0E200003, None)
                self.assertEqual(dx, dy, x[0])


class DuplicatesExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shinra-dup-"))
        self.mb = make_mailbox(self.tmp / "arch", n=30, with_data=False, all_mail=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_drop_duplicates(self):
        tasks = pst_export.collect_tasks(self.mb)
        total = sum(len(t[2]) for t in tasks)
        kept, dups = pst_export.drop_duplicates(tasks)
        self.assertEqual(dups, 20)                     # копии INBOX (10) и Sent (10)
        self.assertEqual(sum(len(t[2]) for t in kept), total - 20)
        allm = [t for t in kept if t[0]["display"] == "[Gmail]/All Mail"][0]
        self.assertEqual([Path(e).name for e in allm[2]], ["2_own.eml"])

    def test_export_skip_duplicates(self):
        (ok, errors, files, report), cb = export(self.mb, self.tmp / "a" / "box.pst",
                                                 skip_duplicates=True)
        self.assertEqual((ok, errors), (31, 0))
        summary = pst_reader.verify(files[0])
        self.assertEqual(count(summary), 31)
        self.assertIn("20", Path(report).read_text(encoding="utf-8"))
        (ok, _, _, _), _ = export(self.mb, self.tmp / "b" / "box.pst", skip_duplicates=False)
        self.assertEqual(ok, 51)


if __name__ == "__main__":
    unittest.main()
