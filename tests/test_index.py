"""Индекс просмотрщика: строки писем, кэш, поиск по тексту; фильтры окна просмотра."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from core import mail_index, mailparse
from core.mail_index import load_folders, load_texts, mailbox_folders, norm_text
from tests.helpers import make_mailbox

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
except ImportError:
    QApplication = None


def folders_of(mb):
    return [(f["dir"], f["display"]) for f in mailbox_folders(mb)]


class IndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="shinra-idx-"))
        cls.mb = make_mailbox(cls.tmp, n=45)
        cls.files = sorted(p for p in cls.mb.rglob("*") if p.name.endswith((".eml", ".eml.gz")))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_rows_match_summaries(self):
        rows = load_folders(folders_of(self.mb))
        self.assertEqual(len(rows), len(self.files))
        by_path = {os.path.normcase(r["path"]): r for r in rows}
        for f in self.files:
            r = by_path[os.path.normcase(str(f))]
            s = mailparse.summarize(f)
            for k in ("subject", "from", "to", "msgid", "size", "attach"):
                self.assertEqual(r[k], s[k], f"{f.name}: {k}")

    def test_cache_reused_and_refreshed(self):
        first = load_folders(folders_of(self.mb))
        again = load_folders(folders_of(self.mb))
        key = lambda rs: sorted((r["path"], r["subject"], r["size"]) for r in rs)   # noqa: E731
        self.assertEqual(key(first), key(again))
        # письмо изменили на диске — строка обновляется
        f = next(p for p in self.files if p.suffix == ".eml" and "INBOX" in p.parts)
        old = f.read_bytes()
        try:
            f.write_bytes(old.replace(b"Subject: ", b"Subject: CHANGED ", 1))
            os.utime(f, (1_800_000_000,) * 2)
            rows = load_folders(folders_of(self.mb))
            r = next(r for r in rows if os.path.normcase(r["path"]) == os.path.normcase(str(f)))
            self.assertTrue(r["subject"].startswith("CHANGED "))
        finally:
            f.write_bytes(old)

    def test_stop(self):
        self.assertEqual(load_folders(folders_of(self.mb), stop=lambda: True), [])

    def test_text_search_matches_brute_force(self):
        rows = load_folders(folders_of(self.mb))
        for attempt in ("build", "cached"):
            idx = load_texts(rows)
            texts = {r["path"]: norm_text(mailparse.search_text(r["path"])) for r in rows}
            for q in ("привет мир", "Quarterly", "hello\nworld", "нет такого текста", "отчёт",
                      "ЭТО ТЕСТ"):
                with self.subTest(attempt=attempt, q=q):
                    want = {p for p, t in texts.items() if norm_text(q) and norm_text(q) in t}
                    self.assertEqual(idx.search(q), want)
        self.assertTrue(any(d.joinpath(mail_index.TEXT_INDEX).exists() for d, _ in folders_of(self.mb)))


@unittest.skipIf(QApplication is None, "нет PySide6")
class ViewerFilterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.tmp = Path(tempfile.mkdtemp(prefix="shinra-view-"))
        cls.mb = make_mailbox(cls.tmp, n=45)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        from gui.viewer import MessageFilter, MessageModel, prepare_rows
        self.rows = prepare_rows(load_folders(folders_of(self.mb)))
        self.model = MessageModel()
        self.model.set_rows(self.rows)
        self.proxy = MessageFilter()
        self.proxy.setSourceModel(self.model)

    def shown(self, text="", **kw):
        self.proxy.update(text, None, None, "received", **kw)
        return [self.model.rows[self.proxy.mapToSource(self.proxy.index(i, 0)).row()]
                for i in range(self.proxy.rowCount())]

    def test_scopes(self):
        self.assertEqual(len(self.shown()), len(self.rows))
        by_from = self.shown("ivan3@", scope="from")
        self.assertTrue(by_from and all("ivan3@" in r["from"].lower() for r in by_from))
        self.assertEqual(self.shown("ivan3@", scope="subject"), [])
        self.assertTrue(self.shown("boss@", scope="to"))
        self.assertEqual(self.shown("boss@", scope="from"), [])
        att = self.shown("файл0", scope="attach")
        self.assertTrue(att and all(any("файл0" in n for n in r["att_names"]) for r in att))
        self.assertEqual({id(r) for r in self.shown("файл0")} >= {id(r) for r in att}, True)

    def test_attachments_and_size(self):
        with_att = self.shown(with_att=True)
        self.assertTrue(with_att)
        self.assertTrue(all(r["attach"] for r in with_att))
        self.assertEqual(len(with_att), sum(1 for r in self.rows if r["attach"]))
        big = self.shown(min_size=20_000)
        self.assertEqual(len(big), sum(1 for r in self.rows if r["size"] >= 20_000))
        both = self.shown("ivan", scope="from", with_att=True, min_size=20_000)
        self.assertEqual(len(both), sum(1 for r in self.rows if r["attach"] and r["size"] >= 20_000
                                        and "ivan" in r["from"].lower()))

    def test_body_hits_only_for_everywhere(self):
        target = self.rows[0]["path"]
        self.assertEqual([r["path"] for r in self.shown("zzz-nowhere", hits={target})], [target])
        self.assertEqual(self.shown("zzz-nowhere", hits={target}, scope="subject"), [])

    def test_window_controls(self):
        from gui.viewer import ViewerWindow

        class Cfg:
            archive_dir = str(self.tmp)
            accounts = []
        w = ViewerWindow(None, Cfg())
        try:
            w.model.set_rows(self.rows)
            w.chk_att.setChecked(True)
            self.assertEqual(w.proxy.rowCount(), sum(1 for r in self.rows if r["attach"]))
            w.sp_size.setValue(0.1)               # МБ; срабатывает с задержкой — применяем сразу
            w._apply_filter()
            n = sum(1 for r in self.rows if r["attach"] and r["size"] >= 0.1 * 1024 * 1024)
            self.assertTrue(0 < n < sum(1 for r in self.rows if r["attach"]))
            self.assertEqual(w.proxy.rowCount(), n)
            w.cb_scope.setCurrentIndex(w.cb_scope.findData("subject"))
            self.assertFalse(w.chk_body.isEnabled())
            w._reset_filters()
            self.assertEqual(w.proxy.rowCount(), len(self.rows))
            self.assertTrue(w.chk_body.isEnabled())
        finally:
            w._load_token += 1                    # фоновая загрузка дерева больше не нужна
            w.close()
            w.deleteLater()


if __name__ == "__main__":
    unittest.main()
