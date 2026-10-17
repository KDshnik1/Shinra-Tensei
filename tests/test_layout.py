"""Вёрстка окон: колонки по ширине окна, окно экспорта без налезающих строк."""
import os
import tempfile
import unittest
from unittest import mock

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
except ImportError:
    QApplication = None


@unittest.skipIf(QApplication is None, "нет PySide6")
class LayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"APPDATA": tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def settle(self):
        for _ in range(3):
            self.app.processEvents()

    def main_window(self):
        import app as main
        from core.models import Account
        with mock.patch("core.models.Config.load", lambda cfg: cfg.accounts.extend(
                [Account(email="jolyman2017@ya.ru", host="imap.ya.ru"),
                 Account(email="a.gavshin@maximatelecom.ru", host="cas.maximatelecom.ru")])):
            w = main.MainWindow()
        self.addCleanup(lambda: (w._logfile.close(), w.deleteLater()))
        w.show()
        self.settle()
        return w

    def widths(self, view):
        return [view.columnWidth(c) for c in range(view.horizontalHeader().count())]

    def test_columns_come_back_after_narrow_window(self):
        w = self.main_window()
        w.resize(1300, 700)
        self.settle()
        wide = self.widths(w.table)
        w.resize(760, 700)
        self.settle()
        narrow = self.widths(w.table)
        hh = w.table.horizontalHeader()
        for c, width in enumerate(narrow):
            self.assertGreaterEqual(width, w.columns.min_width(c) if c else 30, c)
            if c:       # заголовок не обрезается
                self.assertGreaterEqual(width, hh.sectionSizeFromContents(c).width(), c)
        w.resize(1300, 700)
        self.settle()
        self.assertEqual(self.widths(w.table), wide)
        # на широком окне таблица занимает ровно всю ширину, без прокрутки
        self.assertEqual(sum(wide), w.table.viewport().width())

    def test_user_width_is_kept(self):
        w = self.main_window()
        w.resize(1300, 700)
        self.settle()
        w.table.horizontalHeader().resizeSection(2, 300)       # «Сервер» мышью
        self.assertEqual(w.cfg.ui["columns"], {"server": 300})
        w.resize(1600, 700)
        self.settle()
        self.assertGreaterEqual(w.table.columnWidth(2), 300)
        w.columns.reset()
        self.assertEqual(w.cfg.ui["columns"], {})

    def test_settings_menu(self):
        w = self.main_window()
        dark = next(a for a in w.theme_group.actions() if a.data() == "dark")
        with mock.patch("gui.theme.apply"):
            dark.trigger()
        self.assertEqual(w.cfg.theme, "dark")
        w.sp_parallel.setValue(5)
        self.assertEqual(w.cfg.parallel, 5)

    def test_progress_is_coalesced(self):
        w = self.main_window()
        acc = w.cfg.accounts[0]
        with mock.patch.object(w, "_update_row") as upd:
            for n in range(500):
                w._on_progress(acc.id, n, 1000)
            upd.assert_not_called()
            w._flush_rows()
            upd.assert_called_once_with(acc.id)
        self.assertEqual(w.state[acc.id]["done"], 499)

    def test_export_dialog_fits_small_screen(self):
        from core.models import Config
        from gui.export_dialog import ExportDialog
        d = ExportDialog(None, Config())
        self.addCleanup(d.deleteLater)
        d.show()
        self.assertLess(d.minimumSizeHint().height(), 700)
        d.resize(d.minimumSizeHint().width(), 600)
        self.settle()
        lbl = d.lbl_info
        self.assertGreaterEqual(lbl.height(), lbl.heightForWidth(lbl.width()))
        self.assertTrue(d.pb.isVisible() and d.btn_start.isVisible())


class ConfigUiTest(unittest.TestCase):
    def test_ui_round_trip(self):
        from core.models import Config
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"APPDATA": tmp}):
            c = Config()
            c.ui = {"columns": {"email": 250}, "geometry": "AAAA"}
            c.save()
            c2 = Config()
            c2.load()
            self.assertEqual(c2.ui, c.ui)


@unittest.skipIf(QApplication is None, "нет PySide6")
class ColumnMathTest(unittest.TestCase):
    """Раскладка колонок без окна на экране."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def fitter(self):
        from PySide6.QtWidgets import QTableWidget
        from gui.fit import ColumnFitter
        t = QTableWidget(0, 3)
        t.setHorizontalHeaderLabels(["a", "b", "c"])
        self.addCleanup(t.deleteLater)
        return ColumnFitter(t, [("a", 100, 1, 50), ("b", 100, 3, 50), ("c", 80, 0, 40)])

    def test_grow_by_weight(self):
        self.assertEqual(self.fitter().widths_for(480), [150, 250, 80])

    def test_shrink_to_minimum(self):
        f = self.fitter()
        self.assertEqual(f.widths_for(230), [75, 75, 80])
        self.assertEqual(f.widths_for(100), [50, 50, 80])      # уже нельзя — прокрутка


if __name__ == "__main__":
    unittest.main()
