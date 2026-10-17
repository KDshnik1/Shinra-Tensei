"""Окно ящика: «Далее» проверяет первую страницу, вторая задаёт соединения и повторы."""
import os
import unittest
from unittest import mock

from core.models import Account

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
except ImportError:
    QApplication = None


@unittest.skipIf(QApplication is None, "нет PySide6")
class AccountDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def dialog(self, account=None):
        from gui.account_dialog import AccountDialog
        d = AccountDialog(None, account, "")
        self.addCleanup(d.deleteLater)
        return d

    def test_next_validates_first_page(self):
        d = self.dialog()
        with mock.patch("gui.account_dialog.QMessageBox.warning") as warn:
            d._next()
        warn.assert_called_once()
        self.assertEqual(d.pages.currentIndex(), 0)
        d.ed_email.setText("user@example.com")
        d._autofill()
        d._next()
        self.assertEqual(d.pages.currentIndex(), 1)
        self.assertTrue(d.btn_save.isVisibleTo(d) and not d.btn_next.isVisibleTo(d))
        a = d._collect()
        self.assertEqual((a.connections, a.duplicates), (1, "auto"))

    def test_options_round_trip(self):
        d = self.dialog()
        d.ed_email.setText("user@example.com")
        d._autofill()
        self.assertFalse(d.sp_conns.isEnabled())
        d.rb_many_conn.setChecked(True)
        self.assertTrue(d.sp_conns.isEnabled())
        d.sp_conns.setValue(5)
        for rb, mode in ((d.rb_dup_always, "always"), (d.rb_dup_never, "never"), (d.rb_dup_auto, "auto")):
            rb.setChecked(True)
            a = d._collect()
            self.assertEqual((a.connections, a.duplicates), (5, mode))
            again = self.dialog(a)
            self.assertTrue(again.rb_many_conn.isChecked())
            self.assertEqual(again.sp_conns.value(), 5)
            self.assertEqual(again._collect().duplicates, mode)

    def test_save_from_second_page(self):
        d = self.dialog(Account(email="a@b.c", host="imap.b.c", connections=3, duplicates="never"))
        d._show_page(1)
        d._accept()
        self.assertEqual(d.result(), d.DialogCode.Accepted)
        self.assertEqual((d.account.connections, d.account.duplicates), (3, "never"))


if __name__ == "__main__":
    unittest.main()
