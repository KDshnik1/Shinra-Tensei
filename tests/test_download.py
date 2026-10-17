"""Загрузка с тестового IMAP-сервера: пачки, обрывы, капризы серверов, повторы писем."""
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from core.downloader import Downloader
from core.models import Account
from tests.fake_imap import FakeImapServer, UIDVALIDITY


def make_msg(i: int, folder: str = "INBOX", msgid: str | None = None, pad: int = 0) -> bytes:
    msgid = msgid if msgid is not None else f"<m{i}@test>"
    head = (f"From: Sender {i} <s{i}@example.com>\r\nTo: r@example.com\r\n"
            f"Subject: Message {i} in {folder}\r\nDate: Mon, 5 Jan 2015 10:00:00 +0300\r\n")
    if msgid:
        head += f"Message-ID: {msgid}\r\n"
    return (head + "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            + f"Body {i}\r\n" + "x" * pad + "\r\n").encode()


class CB:
    def __init__(self):
        self.lines, self.c, self.fin, self.last = [], None, None, None

    def status(self, s): pass
    def progress(self, a, b): self.last = (a, b)
    def log(self, s): self.lines.append(s)
    def counters(self, *a): self.c = a

    def ask(self, q):
        self.lines.append(f"ASK {q.uid}: {q.error}")
        q.answer = "skip"
        q.event.set()

    def finished(self, kind, s): self.fin = (kind, s)


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shinra-dl-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    CONNECTIONS = 1

    def run_dl(self, folders, skip_duplicates=False, connections=None, **opt):
        srv = FakeImapServer(folders, **opt)
        try:
            acc = Account(email="t@x", host="127.0.0.1", port=srv.port, security="none",
                          login="u", password="p", skip_duplicates=skip_duplicates,
                          connections=connections or self.CONNECTIONS)
            cb = CB()
            Downloader(acc, str(self.tmp), cb, threading.Event(), threading.Semaphore(1)).run()
            return cb, srv.state
        finally:
            srv.close()

    def saved(self, folder):
        d = self.tmp.joinpath("t@x", *folder.split("/"))
        return {int(p.name.split("_")[1].split(".")[0]): p.read_bytes()
                for p in d.glob(f"{UIDVALIDITY}_*.eml")}

    def check_all(self, folders, missing=()):
        for name, f in folders.items():
            want = {u: b for u, b in f["messages"].items() if u not in missing}
            self.assertEqual(self.saved(name), want, name)

    def folders(self, n=250, pad=0):
        return {"INBOX": {"messages": {u: make_msg(u, pad=pad) for u in range(1, n + 1)}},
                "Sent": {"messages": {u: make_msg(1000 + u, "Sent") for u in range(1, 11)}}}

    # ---------- обычная загрузка ----------
    def test_basic_batches(self):
        f = self.folders()
        cb, st = self.run_dl(f)
        self.assertEqual(cb.fin[0], "ok", cb.lines)
        self.check_all(f)
        self.assertEqual(cb.c, (260, 0, 0))
        self.assertLessEqual(st.fetch_cmds, 5)          # пачки, а не по письму
        self.assertEqual(cb.last, (260, 260))

    def test_rerun_downloads_nothing(self):
        f = self.folders(30)
        self.run_dl(f)
        cb, st = self.run_dl(f)
        self.assertEqual(cb.c, (0, 40, 0))
        self.assertEqual(st.fetch_cmds, 0)

    def test_big_messages_split_by_bytes(self):
        f = {"INBOX": {"messages": {u: make_msg(u, pad=3 * 1024 * 1024) for u in range(1, 9)}}}
        cb, st = self.run_dl(f)
        self.check_all(f)
        self.assertGreaterEqual(st.fetch_cmds, 2)       # 8 × 3 МБ не влезают в одну пачку

    # ---------- капризы серверов ----------
    def test_connection_drop(self):
        f = self.folders()
        cb, _ = self.run_dl(f, drop_after=37)
        self.assertEqual(cb.fin[0], "ok", cb.lines)
        self.check_all(f)

    def test_missing_messages(self):
        f = self.folders(40)
        cb, _ = self.run_dl(f, missing=(5, 17))
        self.check_all(f, missing=(5, 17))
        self.assertEqual(cb.c[2], 3)                    # INBOX 5, 17 и Sent 5 — ошибки, не потеряны молча
        self.assertEqual(cb.fin[0], "warn")

    def test_no_multi_fetch(self):
        f = self.folders(20)
        cb, _ = self.run_dl(f, no_multi=True)
        self.assertEqual(cb.fin[0], "ok", cb.lines)
        self.check_all(f)

    def test_no_sizes(self):
        f = self.folders(120)
        for dups in (False, True):
            with self.subTest(skip_duplicates=dups):
                shutil.rmtree(self.tmp, ignore_errors=True)
                cb, _ = self.run_dl(f, skip_duplicates=dups, no_size=True)
                self.assertEqual(cb.fin[0], "ok", cb.lines)
                self.check_all(f)

    def test_flags_after_body_and_unsolicited(self):
        f = self.folders(30)
        cb, _ = self.run_dl(f, flags_after=True, unsolicited=True)
        self.assertEqual(cb.fin[0], "ok", cb.lines)
        self.check_all(f)

    # ---------- повторы писем ----------
    def gmail(self):
        inbox = {u: make_msg(u) for u in range(1, 21)}
        work = {u: make_msg(100 + u, "Work") for u in range(1, 6)}
        allm = {}
        for i, b in enumerate(list(inbox.values()) + list(work.values()), 1):
            allm[i] = b
        allm[90] = make_msg(500, "Archive")              # есть только во «Вся почта»
        allm[91] = make_msg(501, "Archive", msgid="")    # без Message-ID — всегда качается
        allm[92] = make_msg(3, "x", msgid="<m3@test>")   # тот же Message-ID, но другое письмо
        # «Вся почта» идёт первой в списке сервера, но обрабатываться должна последней
        return {"[Gmail]/All Mail": {"flags": ["\\All"], "messages": allm},
                "INBOX": {"messages": inbox}, "Work": {"messages": work}}

    def test_duplicates_skipped(self):
        f = self.gmail()
        cb, _ = self.run_dl(f, skip_duplicates=True)
        self.assertEqual(cb.fin[0], "ok", cb.lines)
        self.assertEqual(self.saved("INBOX"), f["INBOX"]["messages"])
        self.assertEqual(self.saved("Work"), f["Work"]["messages"])
        self.assertEqual(sorted(self.saved("[Gmail]/All Mail")), [90, 91, 92])
        self.assertEqual(cb.c, (28, 0, 0))
        self.assertEqual(cb.last, (53, 53))

    def test_duplicates_against_existing_archive(self):
        f = self.gmail()
        only_inbox = {"INBOX": f["INBOX"]}
        self.run_dl(only_inbox, skip_duplicates=True)
        cb, _ = self.run_dl(f, skip_duplicates=True)
        self.assertEqual(sorted(self.saved("[Gmail]/All Mail")), [90, 91, 92])
        self.assertEqual(cb.c, (8, 20, 0))

    def test_duplicates_server_without_header_fetch(self):
        f = self.gmail()
        cb, st = self.run_dl(f, skip_duplicates=True, no_msgid=True)
        self.assertEqual(cb.fin[0], "ok", cb.lines)
        self.check_all(f)                                 # повторы не ищутся, но всё скачано
        self.assertLessEqual(st.fetch_cmds, 4)            # размеры есть — качается пачками
        self.assertTrue(any("Message-ID" in line for line in cb.lines))

    def test_duplicates_kept_when_disabled(self):
        f = self.gmail()
        cb, _ = self.run_dl(f, skip_duplicates=False)
        self.check_all(f)
        self.assertEqual(cb.c, (53, 0, 0))


class ParallelDownloadTest(DownloadTest):
    """Несколько соединений на один ящик: все сценарии выше — с тремя соединениями, и свои."""
    CONNECTIONS = 3

    def flags_ok(self, folders):
        for name, f in folders.items():
            p = self.tmp.joinpath("t@x", *name.split("/"), "flags.txt")
            lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
            self.assertEqual(sorted(lines), sorted(f"{UIDVALIDITY}_{u}\t\\Seen"
                                                   for u in f["messages"]), name)

    def test_parallel_basic(self):
        f = self.folders(450)
        cb, st = self.run_dl(f, connections=4, latency=0.01)
        self.assertEqual(cb.fin[0], "ok", cb.lines)
        self.check_all(f)
        self.flags_ok(f)
        self.assertEqual(cb.c, (460, 0, 0))
        self.assertEqual(cb.last, (460, 460))
        self.assertGreaterEqual(st.max_active, 2)
        self.assertLessEqual(st.max_active, 4)

    def test_no_more_connections_than_batches(self):
        f = {"INBOX": {"messages": {u: make_msg(u) for u in range(1, 31)}}}
        cb, st = self.run_dl(f, connections=8)
        self.check_all(f)
        self.assertEqual(st.max_active, 1)              # одна пачка — одно соединение

    def test_server_connection_limit(self):
        f = self.folders(450)
        cb, st = self.run_dl(f, connections=5, max_conns=2, latency=0.01)
        self.assertEqual(cb.fin[0], "ok", cb.lines)
        self.check_all(f)
        self.flags_ok(f)
        self.assertEqual(cb.c, (460, 0, 0))
        self.assertTrue(any("не открылось" in l or "did not open" in l for l in cb.lines), cb.lines)

    def test_parallel_connection_drop(self):
        f = self.folders(450)
        cb, _ = self.run_dl(f, connections=3, drop_after=150)
        self.assertEqual(cb.fin[0], "ok", cb.lines)
        self.check_all(f)
        self.assertEqual(cb.c, (460, 0, 0))

    def test_parallel_no_multi_and_missing(self):
        f = self.folders(60)
        cb, _ = self.run_dl(f, connections=3, no_multi=True, missing=(5, 17))
        self.check_all(f, missing=(5, 17))
        self.assertEqual(cb.c, (67, 0, 3))

    def big_gmail(self):
        inbox = {u: make_msg(u) for u in range(1, 251)}
        work = {u: make_msg(1000 + u, "Work") for u in range(1, 121)}
        allm = {i: b for i, b in enumerate(list(inbox.values()) + list(work.values()), 1)}
        allm[900] = make_msg(5000, "Archive")
        allm[901] = make_msg(5001, "Archive", msgid="")
        return {"[Gmail]/All Mail": {"flags": ["\\All"], "messages": allm},
                "INBOX": {"messages": inbox}, "Work": {"messages": work}}

    def test_parallel_duplicates(self):
        f = self.big_gmail()
        for conns in (1, 4):
            with self.subTest(connections=conns):
                shutil.rmtree(self.tmp, ignore_errors=True)
                cb, _ = self.run_dl(f, skip_duplicates=True, connections=conns)
                self.assertEqual(cb.fin[0], "ok", cb.lines)
                self.assertEqual(self.saved("INBOX"), f["INBOX"]["messages"])
                self.assertEqual(self.saved("Work"), f["Work"]["messages"])
                self.assertEqual(sorted(self.saved("[Gmail]/All Mail")), [900, 901])
                self.assertEqual(cb.c, (372, 0, 0))
                self.assertEqual(cb.last, (742, 742))

    def test_stop(self):
        f = self.folders(450)
        srv = FakeImapServer(f, latency=0.01)
        stop = threading.Event()
        try:
            acc = Account(email="t@x", host="127.0.0.1", port=srv.port, security="none",
                          login="u", password="p", connections=4)
            cb = CB()
            t = threading.Thread(target=Downloader(acc, str(self.tmp), cb, stop,
                                                   threading.Semaphore(1)).run)
            t.start()
            while not cb.c or cb.c[0] < 50:
                if not t.is_alive():
                    break
                time.sleep(0.01)
            stop.set()
            t.join(30)
            self.assertFalse(t.is_alive())
            self.assertEqual(cb.fin[0], "stopped")
        finally:
            srv.close()
        # повторный запуск докачивает остальное без повторов
        cb, _ = self.run_dl(f, connections=4)
        self.check_all(f)
        self.flags_ok(f)
        self.assertEqual(cb.c[0] + cb.c[1], 460)


if __name__ == "__main__":
    unittest.main()
