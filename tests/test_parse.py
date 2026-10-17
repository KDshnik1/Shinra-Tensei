"""Разбор писем: Rust и Python дают одно и то же, кривые письма не роняют программу."""
import gzip
import random
import shutil
import tempfile
import unittest
from pathlib import Path

from core import mailparse, pst_export
from core.mail_index import norm_text
from tests.helpers import DATA, RANDOM_TAGS, make_mailbox, native_or_skip, pure_parse, shinra_core

# Письма, где версии расходятся намеренно:
#  missing_close.eml — вложение base64 обрезано посередине строки; Rust сохраняет
#  всё, что удалось раскодировать, Python — меньше. Разбор остального совпадает.
KNOWN_ATTACH_DIFF = {"missing_close.eml"}


def _clean(p):
    return {k: v for k, v in p.items() if k not in RANDOM_TAGS}


def compare_message(a, b, attach_data=True) -> list:
    out = []
    pa, pb = _clean(a["props"]), _clean(b["props"])
    out += [f"0x{k:08X}: {pa.get(k)!r:.80} != {pb.get(k)!r:.80}"
            for k in sorted(set(pa) | set(pb)) if pa.get(k) != pb.get(k)]
    if a.get("received") != b.get("received"):
        out.append(f"received {a.get('received')} != {b.get('received')}")
    if [_clean(x) for x in a["recipients"]] != [_clean(x) for x in b["recipients"]]:
        out.append("recipients")
    if len(a["attachments"]) != len(b["attachments"]):
        out.append(f"attachments {len(a['attachments'])} != {len(b['attachments'])}")
    for x, y in zip(a["attachments"], b["attachments"]):
        xp, yp, xr, yr = dict(x["props"]), dict(y["props"]), dict(x["row"]), dict(y["row"])
        # пересланное письмо Rust кладёт байт в байт, Python собирает заново
        if not attach_data or xp.get(0x370E001F) == "message/rfc822":
            for p in (xp, yp, xr, yr):
                p.pop(0x37010102, None)
                p.pop(0x0E200003, None)
        if xp != yp or xr != yr:
            out.append(f"attachment {xp.get(0x3001001F)!r}")
    return out


class ParseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="shinra-parse-"))
        mb = make_mailbox(cls.tmp, n=45)
        cls.files = sorted(p for p in mb.rglob("*") if p.name.endswith((".eml", ".eml.gz")))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_all_files_parse(self):
        for f in self.files:
            with self.subTest(f.name):
                info = mailparse.summarize(f)
                self.assertIn("subject", info)
                pst_export.eml_to_message(f, False)
                mailparse.search_text(f)

    @native_or_skip
    def test_message_parity(self):
        for f in self.files:
            with self.subTest(f.name):
                with pure_parse():
                    a = pst_export.eml_to_message(f, False)
                b = shinra_core.eml_to_message(f, False)
                diffs = compare_message(a, b, attach_data=not self._is_known(f))
                self.assertEqual(diffs, [], str(f))

    def _is_known(self, f):
        # файлы из tests/data копируются в «Tricky» под номерами — сверяем по содержимому
        data = f.read_bytes()
        for k in KNOWN_ATTACH_DIFF:
            if (DATA / k).read_bytes() == data:
                return True
        return False

    @native_or_skip
    def test_summary_parity(self):
        for f in self.files:
            with self.subTest(f.name):
                self.assertEqual(mailparse._py_summarize(f), shinra_core.summarize(f))

    @native_or_skip
    def test_search_text_parity(self):
        for f in self.files:
            with self.subTest(f.name):
                with pure_parse():
                    a = mailparse.search_text(f)
                self.assertEqual(norm_text(a), norm_text(shinra_core.search_text(f)))

    @native_or_skip
    def test_body_parity(self):
        """Письмо в просмотрщике: Rust (parse_body) и Python (parse_full) — одно и то же."""
        for f in self.files:
            with self.subTest(f.name):
                with pure_parse():
                    a = mailparse.parse_body(f)
                b = mailparse.parse_body(f)
                self.assertEqual((a.text, a.html), (b.text, b.html))
                self.assertEqual([(x.name, x.ctype, x.cid, x.inline) for x in a.attachments],
                                 [(x.name, x.ctype, x.cid, x.inline) for x in b.attachments])
                for x, y in zip(a.attachments, b.attachments):
                    # пересланное письмо Rust отдаёт байт в байт, Python собирает заново
                    if x.ctype != "message/rfc822" and not self._is_known(f):
                        self.assertEqual(x.data, y.data, x.name)

    def test_gz_same_as_plain(self):
        src = DATA / "gz_lf_only.eml.gz"
        plain = self.tmp / "plain.eml"
        plain.write_bytes(gzip.decompress(src.read_bytes()))
        a, b = mailparse.summarize(src), mailparse.summarize(plain)
        for k in ("subject", "from", "to", "msgid", "sent", "size", "attach", "att_names"):
            self.assertEqual(a.get(k), b.get(k), k)

    def test_deep_nesting(self):
        """Тысячи вложенных частей: без падения и зависания (и в Python, и в Rust)."""
        for depth in (60, 3000):
            head = "".join(f'Content-Type: multipart/mixed; boundary="b{i}"\r\n\r\n--b{i}\r\n'
                           for i in range(depth))
            data = ("From: a@b\r\nSubject: deep\r\nMIME-Version: 1.0\r\n" + head
                    + "Content-Type: text/plain\r\n\r\nhello\r\n").encode()
            p = self.tmp / f"deep{depth}.eml"
            p.write_bytes(data)
            with self.subTest(depth=depth):
                with pure_parse():
                    pst_export.eml_to_message(p, False)
                    mailparse.search_text(p)
                    mailparse.summarize(p)
                if shinra_core is not None:
                    shinra_core.eml_to_message(p, False)
                    shinra_core.search_text(p)
                    shinra_core.summarize(p)


TOKENS = [b"\r\n", b"\n", b"\r\n\r\n", b"--", b"boundary=", b"=?utf-8?B?", b"=?koi8-r?Q?", b"?=",
          b"\xff\xfe", b"\xd0", b"\x00", b"Content-Type: multipart/mixed; boundary=\"b\"\r\n",
          b"\r\n--b\r\n", b"\r\n--b--\r\n", b"Content-Type: message/rfc822\r\n\r\n",
          b"Content-Transfer-Encoding: base64\r\n", b"charset=", b"filename*0*=", b"%", b"'", b"\"",
          b"(", b")", b"<", b">", b",", b";", b":", b"@", b"\\", b"=", b" ", b"\t", b"\r\n ",
          b"Date: ", b"From: ", b"Subject: ", b"Message-ID: \xd1\x84<x>",
          b"Content-Disposition: attachment; filename=\"\xc4\xe0.txt\"\r\n"]


class FuzzParseTest(unittest.TestCase):
    """Испорченные письма: разбор не должен бросать исключения ни в одной из версий."""
    N = 300

    def test_mutations(self):
        rnd = random.Random(7)
        seeds = []
        for p in sorted(DATA.iterdir()):
            d = p.read_bytes()
            seeds.append(gzip.decompress(d) if p.name.endswith(".gz") else d)
        tmp = Path(tempfile.mkdtemp(prefix="shinra-fuzz-"))
        try:
            cur = tmp / "cur.eml"
            for i in range(self.N):
                d = bytearray(rnd.choice(seeds))
                for _ in range(rnd.randint(1, 8)):
                    op, pos = rnd.random(), rnd.randint(0, len(d))
                    if op < 0.3 and d:
                        d[min(pos, len(d) - 1)] = rnd.randint(0, 255)
                    elif op < 0.65:
                        d[pos:pos] = rnd.choice(TOKENS)
                    elif op < 0.8:
                        del d[pos:pos + rnd.randint(1, 64)]
                    elif op < 0.9:
                        d = d[:pos]
                    else:
                        src = rnd.choice(seeds)
                        s = rnd.randint(0, max(0, len(src) - 1))
                        d[pos:pos] = src[s:s + rnd.randint(1, 400)]
                cur.write_bytes(bytes(d))
                try:
                    with pure_parse():
                        pst_export.eml_to_message(cur, False)
                        mailparse.summarize(cur)
                        mailparse.search_text(cur)
                    if shinra_core is not None:
                        shinra_core.eml_to_message(cur, False)
                        shinra_core.summarize(cur)
                        shinra_core.search_text(cur)
                except Exception as e:
                    keep = tmp.parent / f"shinra-fuzz-fail-{i}.eml"
                    keep.write_bytes(bytes(d))
                    self.fail(f"мутация {i}: {type(e).__name__}: {e} (письмо: {keep})")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
