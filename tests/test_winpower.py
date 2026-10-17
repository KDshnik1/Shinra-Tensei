"""Запрет сна и EcoQoS: включаются, пока идёт хотя бы одна задача, и только по галочке."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import winpower
from core.models import Config


class FakeWin:
    def __init__(self, keeper):
        self.awake = []
        self.throttling = []
        keeper._set_awake = self.set_awake
        keeper._set_throttling = self.set_throttling
        self.k = keeper

    def set_awake(self, on):
        if on != self.k._awake:
            self.awake.append(on)
            self.k._awake = on

    def set_throttling(self, allowed):
        self.throttling.append(allowed)


@mock.patch.object(winpower, "IS_WIN", True)
class PowerKeeperTest(unittest.TestCase):
    def test_awake_while_any_task_runs(self):
        k = winpower.PowerKeeper()
        w = FakeWin(k)
        k.hold(("download", "a"))
        k.hold(("export", 1))
        self.assertTrue(k.awake)
        k.release(("download", "a"))
        self.assertTrue(k.awake)
        k.release(("export", 1))
        self.assertFalse(k.awake)
        self.assertEqual(w.awake, [True, False])
        self.assertEqual(w.throttling[0], False)    # EcoQoS выключен на время работы
        self.assertEqual(w.throttling[-1], True)    # и возвращён системе после

    def test_checkbox_off_keeps_sleep_but_not_throttling(self):
        k = winpower.PowerKeeper()
        w = FakeWin(k)
        k.set_keep_awake(False)
        k.hold("x")
        self.assertFalse(k.awake)
        self.assertEqual(w.throttling[-1], False)
        k.set_keep_awake(True)                      # включили галочку посреди работы
        self.assertTrue(k.awake)
        k.set_keep_awake(False)
        self.assertFalse(k.awake)
        k.release("x")

    def test_release_unknown_key_is_harmless(self):
        k = winpower.PowerKeeper()
        FakeWin(k)
        k.release("nothing")
        self.assertFalse(k.busy)

    def test_windows_errors_do_not_break_tasks(self):
        k = winpower.PowerKeeper()
        k._set_awake = mock.Mock(side_effect=OSError("boom"))
        k._set_throttling = mock.Mock(side_effect=OSError("boom"))
        k.hold("x")
        k.release("x")


class ConfigKeepAwakeTest(unittest.TestCase):
    def test_saved_and_loaded(self):
        with tempfile.TemporaryDirectory() as d:
            c = Config()
            c.path = Path(d) / "config.json"
            self.assertTrue(c.keep_awake)           # по умолчанию включено
            c.keep_awake = False
            c.save()
            self.assertFalse(json.loads(c.path.read_text(encoding="utf-8"))["keep_awake"])
            c2 = Config()
            c2.path = c.path
            c2.load()
            self.assertFalse(c2.keep_awake)


if __name__ == "__main__":
    unittest.main()
