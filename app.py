"""Shinra Tensei — параллельная архивация почты по IMAP с экспортом в PST."""
import collections
import copy
import html
import os
import sys
import threading
from datetime import datetime

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QFileDialog, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPlainTextEdit, QProgressBar, QSpinBox, QSplitter,
                               QStyle, QTableWidget, QTableWidgetItem, QToolBar, QVBoxLayout,
                               QWidget, QSizePolicy)

from core.downloader import Downloader
from core.models import APP_NAME, Config, app_data_dir
from core.storage import MailboxArchive
from gui.account_dialog import AccountDialog
from gui import theme
from gui.export_dialog import ExportDialog
from gui.viewer import ViewerWindow

COLS = ["E-mail", "Сервер", "Период", "Папки", "Статус", "Прогресс",
        "Загружено", "Уже в архиве", "Ошибки"]
C_STATUS, C_PROGRESS, C_DL, C_ALREADY, C_ERR = 4, 5, 6, 7, 8


class Bridge(QObject):
    """Сигналы из рабочих потоков в интерфейс (Qt сам доставит их в главный поток)."""
    status = Signal(str, str)
    progress = Signal(str, int, int)
    log = Signal(str, str)
    counters = Signal(str, int, int, int)
    ask = Signal(object)
    finished = Signal(str, str, str)


class Callbacks:
    def __init__(self, bridge: Bridge, acc_id: str):
        self.b, self.id = bridge, acc_id

    def status(self, t): self.b.status.emit(self.id, t)
    def progress(self, d, t): self.b.progress.emit(self.id, d, t)
    def log(self, t): self.b.log.emit(self.id, t)
    def counters(self, d, a, e): self.b.counters.emit(self.id, d, a, e)
    def ask(self, q): self.b.ask.emit(q)
    def finished(self, kind, msg): self.b.finished.emit(self.id, kind, msg)


def new_state():
    return {"status": "Ожидание", "kind": "idle", "done": 0, "total": 0,
            "downloaded": 0, "already": 0, "errors": 0}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} — архивация почты IMAP и экспорт в PST")
        self.resize(1250, 760)
        self.cfg = Config()
        self.cfg.load()
        self.state = {}
        self.running = {}            # acc_id -> stop_event
        self.sem = None
        self._ask_queue = collections.deque()
        self._asking = False

        logs = app_data_dir() / "logs"
        logs.mkdir(exist_ok=True)
        self._logfile = open(logs / f"{datetime.now():%Y-%m-%d}.log", "a", encoding="utf-8")

        self.bridge = Bridge()
        self.bridge.status.connect(self._on_status)
        self.bridge.progress.connect(self._on_progress)
        self.bridge.log.connect(self._on_log)
        self.bridge.counters.connect(self._on_counters)
        self.bridge.ask.connect(self._on_ask)
        self.bridge.finished.connect(self._on_finished)

        self._applying_theme = False
        self._viewer = None
        self._build_ui()
        self._refresh_table()
        self._sync_actions()
        try:   # следим за сменой темы Windows, если выбрана «Системная»
            QApplication.instance().styleHints().colorSchemeChanged.connect(
                self._system_scheme_changed)
        except AttributeError:
            pass

    # ================= интерфейс =================
    def _build_ui(self):
        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        banner = QWidget(objectName="Banner")
        bl = QVBoxLayout(banner)
        bl.setContentsMargins(18, 12, 18, 12)
        bl.addWidget(QLabel(APP_NAME, objectName="BannerTitle"))
        bl.addWidget(QLabel("Архивация почты: одновременная загрузка нескольких IMAP-ящиков и экспорт в Outlook PST",
                            objectName="BannerSub"))
        lay.addWidget(banner)

        tb = QToolBar()
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.addToolBar(tb)
        st = self.style()

        def act(text, icon, slot, tip=""):
            a = QAction(st.standardIcon(icon), text, self)
            a.setToolTip(tip or text)
            a.triggered.connect(slot)
            tb.addAction(a)
            return a
        SP = QStyle.StandardPixmap
        self.a_add = act("Добавить ящик", SP.SP_FileDialogNewFolder, self._add)
        self.a_edit = act("Изменить", SP.SP_FileDialogDetailedView, self._edit)
        self.a_dup = act("Копировать", SP.SP_FileDialogListView, self._duplicate,
                         "Создать ящик на основе выбранного (тот же сервер и настройки)")
        self.a_del = act("Удалить", SP.SP_TrashIcon, self._delete)
        tb.addSeparator()
        self.a_run_sel = act("Запустить выбранные", SP.SP_MediaPlay, self._run_selected)
        self.a_run_all = act("Запустить все", SP.SP_MediaSeekForward, self._run_all)
        self.a_stop = act("Остановить", SP.SP_MediaStop, self._stop_selected,
                          "Остановить выбранные (если ничего не выбрано — все)")
        tb.addSeparator()
        self.a_view = act("Просмотр писем", SP.SP_FileDialogContentsView, self._view,
                          "Просмотреть скачанные письма (выбранного ящика или всех)")
        self.a_export = act("Экспорт в PST", SP.SP_DialogSaveButton, self._export)
        self.a_open = act("Открыть архив", SP.SP_DirOpenIcon, self._open_archive)
        self.a_arch = act("Папка архива…", SP.SP_DriveHDIcon, self._choose_archive)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        tb.addWidget(QLabel("Тема: "))
        self.cb_theme = QComboBox()
        for text, val in theme.THEME_MODES:
            self.cb_theme.addItem(text, val)
        self.cb_theme.setCurrentIndex(max(self.cb_theme.findData(self.cfg.theme), 0))
        self.cb_theme.currentIndexChanged.connect(self._theme_changed)
        tb.addWidget(self.cb_theme)
        tb.addSeparator()
        tb.addWidget(QLabel(" Одновременно ящиков: "))
        self.sp_parallel = QSpinBox(minimum=1, maximum=20, value=self.cfg.parallel)
        self.sp_parallel.valueChanged.connect(self._parallel_changed)
        tb.addWidget(self.sp_parallel)

        split = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(C_STATUS, QHeaderView.ResizeMode.Stretch)
        for c, w in enumerate([220, 190, 150, 130, 0, 170, 85, 95, 65]):
            if w:
                self.table.setColumnWidth(c, w)
        self.table.doubleClicked.connect(self._edit)
        self.table.itemSelectionChanged.connect(self._sync_actions)
        split.addWidget(self.table)

        self.log = QPlainTextEdit(readOnly=True)
        self.log.setMaximumBlockCount(10000)
        split.addWidget(self.log)
        split.setSizes([450, 250])
        w = QWidget()
        wl = QVBoxLayout(w)
        wl.setContentsMargins(8, 8, 8, 8)
        wl.addWidget(split)
        lay.addWidget(w, 1)
        self.setCentralWidget(central)
        self._update_statusbar()

    def _update_statusbar(self):
        self.statusBar().showMessage(
            f"Архив: {self.cfg.archive_dir}   |   Работает: {len(self.running)}")

    # ================= таблица =================
    def _row_of(self, acc_id):
        for r, a in enumerate(self.cfg.accounts):
            if a.id == acc_id:
                return r
        return -1

    def _selected_accounts(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.cfg.accounts[r] for r in rows if r < len(self.cfg.accounts)]

    def _refresh_table(self):
        self.table.setRowCount(len(self.cfg.accounts))
        for r, a in enumerate(self.cfg.accounts):
            self.state.setdefault(a.id, new_state())
            for c, text in enumerate([a.email, f"{a.host}:{a.port}", a.period_text(),
                                      a.folder_text()]):
                self.table.setItem(r, c, QTableWidgetItem(text))
            pb = QProgressBar()
            pb.setTextVisible(True)
            self.table.setCellWidget(r, C_PROGRESS, pb)
            self._update_row(a.id)

    def _update_row(self, acc_id):
        r = self._row_of(acc_id)
        if r < 0:
            return
        s = self.state[acc_id]
        it = QTableWidgetItem(s["status"])
        it.setForeground(QColor(theme.status_color(s["kind"])))
        it.setToolTip(s["status"])
        self.table.setItem(r, C_STATUS, it)
        for c, key in ((C_DL, "downloaded"), (C_ALREADY, "already"), (C_ERR, "errors")):
            ci = QTableWidgetItem(str(s[key]))
            ci.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if key == "errors" and s[key]:
                ci.setForeground(QColor(theme.status_color("error")))
            self.table.setItem(r, c, ci)
        pb = self.table.cellWidget(r, C_PROGRESS)
        if pb:
            pb.setRange(0, max(s["total"], 1))
            pb.setValue(s["done"] if s["total"] else (1 if s["kind"] == "ok" else 0))
            pb.setFormat(f"{s['done']} / {s['total']}" if s["total"] else "")

    def _sync_actions(self):
        sel = self._selected_accounts()
        any_running_sel = any(a.id in self.running for a in sel)
        self.a_edit.setEnabled(len(sel) == 1 and not any_running_sel)
        self.a_dup.setEnabled(len(sel) == 1)
        self.a_del.setEnabled(bool(sel) and not any_running_sel)
        self.a_run_sel.setEnabled(bool(sel))
        self.a_stop.setEnabled(bool(self.running))
        self.a_arch.setEnabled(not self.running)
        self._update_statusbar()

    # ================= управление ящиками =================
    def _add(self):
        d = AccountDialog(self)
        if d.exec():
            self.cfg.accounts.append(d.account)
            self.cfg.save()
            self._refresh_table()

    def _edit(self, *_):
        sel = self._selected_accounts()
        if len(sel) != 1 or sel[0].id in self.running:
            return
        d = AccountDialog(self, sel[0])
        if d.exec():
            self.cfg.accounts[self._row_of(sel[0].id)] = d.account
            self.cfg.save()
            self._refresh_table()

    def _duplicate(self):
        sel = self._selected_accounts()
        if len(sel) != 1:
            return
        base = copy.deepcopy(sel[0])
        from core.models import Account
        base.id = Account().id
        base.email = base.login = ""
        base.password = ""
        d = AccountDialog(self, base)
        d.setWindowTitle("Добавить почтовый ящик")
        if d.exec():
            self.cfg.accounts.append(d.account)
            self.cfg.save()
            self._refresh_table()

    def _delete(self):
        sel = [a for a in self._selected_accounts() if a.id not in self.running]
        if not sel:
            return
        names = "\n".join(a.email for a in sel)
        if QMessageBox.question(self, "Удаление", f"Удалить из списка?\n\n{names}\n\n"
                                "Уже скачанные письма в архиве останутся на диске.") \
                != QMessageBox.StandardButton.Yes:
            return
        ids = {a.id for a in sel}
        self.cfg.accounts = [a for a in self.cfg.accounts if a.id not in ids]
        self.cfg.save()
        self._refresh_table()

    def _parallel_changed(self, v):
        self.cfg.parallel = v
        self.cfg.save()

    def _theme_changed(self):
        self.cfg.theme = self.cb_theme.currentData()
        self.cfg.save()
        self.apply_theme()

    def apply_theme(self):
        if self._applying_theme:
            return
        self._applying_theme = True
        try:
            theme.apply(QApplication.instance(), self.cfg.theme)
            self._refresh_table()       # перекрасить статусы под новую тему
        finally:
            self._applying_theme = False

    def _system_scheme_changed(self, *_):
        if self.cfg.theme == "system":
            self.apply_theme()

    def _choose_archive(self):
        d = QFileDialog.getExistingDirectory(self, "Папка архива", self.cfg.archive_dir)
        if d:
            self.cfg.archive_dir = d
            self.cfg.save()
            self._update_statusbar()

    def _open_archive(self):
        sel = self._selected_accounts()
        path = MailboxArchive(self.cfg.archive_dir, sel[0].email).dir if len(sel) == 1 \
            else self.cfg.archive_dir
        os.makedirs(path, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(str(path))

    def _view(self):
        sel = self._selected_accounts()
        if self._viewer is None:
            self._viewer = ViewerWindow(self, self.cfg, sel[0].email if len(sel) == 1 else None)
            self._viewer.finished.connect(lambda *_: setattr(self, "_viewer", None))
            self._viewer.show()
        else:
            self._viewer._fill_tree(sel[0].email if len(sel) == 1 else None)
            self._viewer.showNormal()
            self._viewer.raise_()
            self._viewer.activateWindow()

    def _export(self):
        ExportDialog(self, self.cfg).exec()
        self.cfg.save()

    # ================= запуск / остановка =================
    def _run_selected(self):
        self._start(self._selected_accounts())

    def _run_all(self):
        self._start(list(self.cfg.accounts))

    def _start(self, accounts):
        if not accounts:
            QMessageBox.information(self, "Запуск", "Нет ящиков для запуска.")
            return
        if not self.running:
            self.sem = threading.BoundedSemaphore(self.sp_parallel.value())
        for a in accounts:
            if a.id in self.running:
                continue
            if not a.password:
                pw, ok = QInputDialog.getText(self, "Пароль", f"Пароль для {a.email}:",
                                              QLineEdit.EchoMode.Password)
                if not ok or not pw:
                    continue
                a.password = pw
                if a.save_password:
                    self.cfg.save()
            stop = threading.Event()
            self.running[a.id] = stop
            st = self.state[a.id] = new_state()
            st.update(status="В очереди", kind="running")
            self._update_row(a.id)
            d = Downloader(copy.deepcopy(a), self.cfg.archive_dir,
                           Callbacks(self.bridge, a.id), stop, self.sem)
            threading.Thread(target=d.run, name=f"dl-{a.email}", daemon=True).start()
            self._write_log(a.email, "Запуск загрузки")
        self._sync_actions()

    def _stop_selected(self):
        sel = [a for a in self._selected_accounts() if a.id in self.running]
        targets = [a.id for a in sel] if sel else list(self.running)
        for acc_id in targets:
            self.running[acc_id].set()
            self.state[acc_id].update(status="Остановка…")
            self._update_row(acc_id)

    # ================= сигналы от потоков =================
    def _email(self, acc_id):
        r = self._row_of(acc_id)
        return self.cfg.accounts[r].email if r >= 0 else acc_id

    def _write_log(self, email, text):
        line = f"[{datetime.now():%H:%M:%S}] {email}: {text}"
        self.log.appendPlainText(line)
        try:
            self._logfile.write(line + "\n")
            self._logfile.flush()
        except Exception:
            pass

    def _on_status(self, acc_id, text):
        if acc_id in self.state:
            self.state[acc_id].update(status=text, kind="running")
            self._update_row(acc_id)

    def _on_progress(self, acc_id, done, total):
        if acc_id in self.state:
            self.state[acc_id].update(done=done, total=total)
            self._update_row(acc_id)

    def _on_counters(self, acc_id, dl, already, err):
        if acc_id in self.state:
            self.state[acc_id].update(downloaded=dl, already=already, errors=err)
            self._update_row(acc_id)

    def _on_log(self, acc_id, text):
        self._write_log(self._email(acc_id), text)

    def _on_finished(self, acc_id, kind, msg):
        self.running.pop(acc_id, None)
        if acc_id in self.state:
            self.state[acc_id].update(status=msg, kind=kind)
            self._update_row(acc_id)
        self._write_log(self._email(acc_id), msg)
        self._sync_actions()
        if not self.running:
            QApplication.alert(self)
            self.statusBar().showMessage("Все задачи завершены", 10000)

    # ---------- вопрос «пропустить письмо?» (по одному окну за раз) ----------
    def _on_ask(self, q):
        self._ask_queue.append(q)
        if not self._asking:
            self._process_asks()

    def _process_asks(self):
        self._asking = True
        try:
            while self._ask_queue:
                q = self._ask_queue.popleft()
                if q.cancelled:
                    continue
                q.answer = self._show_question(q)
                q.event.set()
        finally:
            self._asking = False

    def _show_question(self, q) -> str:
        err_color = theme.status_color("error")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Ошибка при загрузке письма")
        box.setText(f"<b>{html.escape(q.email)}</b><br>Папка: {html.escape(q.folder)}<br>"
                    f"UID письма: {html.escape(q.uid)}<br><br>"
                    f"<span style='color:{err_color}'>{html.escape(q.error)}</span><br><br>"
                    "Пропустить это письмо и продолжить загрузку?")
        box.setDetailedText(q.details)
        R = QMessageBox.ButtonRole
        b_skip = box.addButton("Пропустить", R.AcceptRole)
        b_all = box.addButton("Пропускать все ошибки этого ящика", R.AcceptRole)
        b_retry = box.addButton("Повторить", R.ActionRole)
        b_stop = box.addButton("Остановить ящик", R.RejectRole)
        box.setDefaultButton(b_skip)
        box.exec()
        if q.cancelled:
            return "stop"
        return {b_skip: "skip", b_all: "skip_all", b_retry: "retry",
                b_stop: "stop"}.get(box.clickedButton(), "skip")

    # ================= закрытие =================
    def closeEvent(self, e):
        if self.running:
            if QMessageBox.question(self, "Выход",
                                    "Идёт загрузка почты. Остановить и выйти?\n"
                                    "Уже скачанные письма сохранятся, при следующем запуске "
                                    "загрузка продолжится.") != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            for stop in self.running.values():
                stop.set()
        self.cfg.save()
        self._logfile.close()
        e.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    cfg = Config()
    cfg.load()
    theme.apply(app, cfg.theme)        # тема до создания окон
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
