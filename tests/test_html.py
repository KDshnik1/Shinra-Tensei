"""Очистка HTML писем для просмотрщика: Rust и Python одинаково, кривой HTML не роняет."""
import random
import re
import unittest

from core import mailparse
from core.htmlclean import clean_html as py_clean
from tests.helpers import DATA, native_or_skip, shinra_core

NEWSLETTER = (DATA / "newsletter.html").read_text(encoding="utf-8")

CASES = [
    NEWSLETTER,
    '<p onclick="x()">Привет &amp; пока &copy;&nbsp;x</p><script>alert(1)</script>',
    '<table width=600><tr><td style="max-height:0;overflow:hidden">x</td><td>y</td></tr></table>',
    '<div hidden>a</div><p>b<br/>c<br>d</p><input type=text value="x"><form action="http://x">f</form>',
    'a < b > c & d',
    '<p title="a &quot;q&quot; \'s\'">x</p>',
    '<!DOCTYPE html><!--[if mso]><p>m</p><![endif]--><p>z</p>',
    '<P CLASS=Foo>Up</P><BR>',
    '<span style="display:none">unclosed',
    '<iframe src=x>in</iframe>after',
    '<a href="  java\tscript:x">j</a><img src="data:image/png;base64,AAA" alt=i>'
    '<a href="data:text/html,x">d</a>',
    '<td style="max-height:0.5em; overflow:hidden">kept</td>',
]


def implementations():
    out = [("python", py_clean)]
    if shinra_core is not None and hasattr(shinra_core, "clean_html"):
        out.append(("rust", shinra_core.clean_html))
    return out


class CleanHtmlTest(unittest.TestCase):
    def test_newsletter(self):
        for name, clean in implementations():
            with self.subTest(name):
                c = clean(NEWSLETTER)
                # скрытый «прехедер» и блок для телефонов не показываются
                self.assertNotIn("Только до воскресенья", c)
                self.assertNotIn("Мобильная версия", c)
                # исполняемое, формы, встроенные окна и обработчики событий убраны
                for bad in ("<script", "tracking", "onload", "onclick", "javascript:", "<iframe",
                            "<form", "<input", "<button", "<svg", "<title", "<link", "<base",
                            "<meta"):
                    self.assertNotIn(bad, c)
                # картинки из интернета: подпись вместо пустого места, счётчик открытий — ничего
                self.assertNotIn("logo.png", c)
                self.assertNotIn("open.gif", c)
                self.assertIn('<a href="https://shop.example.com/?utm=1&amp;x=2">Магазин</a>', c)
                # встроенная картинка письма, стили, ссылки и текст остаются
                self.assertIn('<img src="cid:banner@shop"', c)
                self.assertIn("<style", c)
                self.assertIn('href="mailto:help@shop.example.com"', c)
                self.assertIn("Здравствуйте, <b>Иван</b>!<br />", c)
                self.assertIn("«снижены»", c)

    def test_unclosed_hidden_keeps_rest(self):
        for name, clean in implementations():
            with self.subTest(name):
                self.assertIn("<p>b</p>", clean('<div style="display:none">a<p>b</p>'))

    def test_text_stays_text(self):
        for name, clean in implementations():
            with self.subTest(name):
                self.assertEqual(clean("a < b &amp; c &lt;script&gt;"),
                                 "a &lt; b &amp; c &lt;script&gt;")

    @native_or_skip
    def test_rust_equals_python(self):
        for h in CASES:
            with self.subTest(h[:40]):
                self.assertEqual(shinra_core.clean_html(h), py_clean(h))

    def test_garbage(self):
        """Обрывки и мусор: без исключений, и ни одного <script> или обработчика событий."""
        rnd = random.Random(7)
        pieces = ["<", ">", "<p", "</", "<!--", "-->", "'", '"', "=", "<script>", "</script>",
                  "<style>", "<div style='display:none'>", "</div>", "&", "&#", "&amp", "<img ",
                  "onclick=x", " ", "текст", "<a href=javascript:x>", "<![CDATA[", "]]>", "/>"]
        samples = ["".join(rnd.choice(pieces) for _ in range(rnd.randint(1, 40)))
                   for _ in range(300)]
        samples += [NEWSLETTER[a:b] for a, b in
                    (sorted(rnd.sample(range(len(NEWSLETTER)), 2)) for _ in range(100))]
        tag = re.compile(r"<[a-z][^>]*>", re.I)
        for name, clean in implementations():
            for h in samples:
                with self.subTest(name=name, h=h[:60]):
                    # содержимое <style> — CSS, а не теги (незакрытый — до конца текста)
                    c = re.sub(r"<style[^>]*>.*?(</style>|$)", "", clean(h), flags=re.S | re.I)
                    for t in tag.findall(c):
                        self.assertNotRegex(t.lower(), r"^<script|\son[a-z]+=|javascript:")

    def test_dispatch(self):
        self.assertEqual(mailparse.clean_html("<p onclick=x>a</p>"), "<p>a</p>")


try:
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
except ImportError:
    QApplication = None


@unittest.skipIf(QApplication is None, "нет PySide6")
class ViewerOpenTest(unittest.TestCase):
    """Письмо открывается в фоне, показывается очищенным, а устаревший разбор не мешает."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import tempfile
        from email.message import EmailMessage
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = "shop@example.com", "ivan@example.com", "Скидки"
        m.set_content("Скидки недели (текст)")
        m.add_alternative(NEWSLETTER, subtype="html")
        self.html_mail = self.dir / "1.eml"
        self.html_mail.write_bytes(bytes(m))
        p = EmailMessage()
        p["From"], p["Subject"] = "a@b", "Просто текст"
        p.set_content("Обычное письмо")
        p.add_attachment(b"x" * 1000, maintype="application", subtype="octet-stream",
                         filename="a.bin")
        self.text_mail = self.dir / "2.eml"
        self.text_mail.write_bytes(bytes(p))

    def viewer(self):
        from gui.viewer import ViewerWindow

        class Cfg:
            archive_dir = str(self.dir)
            accounts = []
        w = ViewerWindow(None, Cfg())

        def close():
            w._load_token += 1
            w.close()
            w.deleteLater()
        self.addCleanup(close)
        return w

    def settle(self):
        """Дождаться всех фоновых разборов и их обработки в окне."""
        import time
        from gui.common import _alive
        end = time.monotonic() + 10
        while _alive and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.01)
        for _ in range(5):
            self.app.processEvents()

    def wait_body(self, w):
        self.settle()
        self.assertIsNotNone(w._body)

    def test_html_mail_is_cleaned(self):
        w = self.viewer()
        w._show_message({"path": str(self.html_mail), "subject": "Скидки"})
        self.wait_body(w)
        html = w.browser.toHtml()
        self.assertIn("Скидки недели", w.browser.toPlainText())
        self.assertNotIn("Только до воскресенья", w.browser.toPlainText())
        self.assertNotIn("logo.png", html)

    def test_stale_result_is_dropped(self):
        w = self.viewer()
        w._show_message({"path": str(self.html_mail), "subject": "Скидки"})
        w._show_message({"path": str(self.text_mail), "subject": "Просто текст"})
        self.wait_body(w)            # оба разбора закончились, показан только второй
        self.assertEqual(w.browser.toPlainText().strip(), "Обычное письмо")
        self.assertEqual(w.att_list.count(), 1)

    def test_broken_file(self):
        w = self.viewer()
        w._show_message({"path": str(self.dir / "missing.eml"), "subject": "?"})
        self.settle()
        self.assertIsNone(w._body)
        from core.i18n import tr
        prefix = tr('Не удалось разобрать письмо: {e}').format(e="")
        self.assertTrue(w.browser.toPlainText().startswith(prefix))


if __name__ == "__main__":
    unittest.main()
