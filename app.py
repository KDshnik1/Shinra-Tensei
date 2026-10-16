"""Shinra Tensei — параллельная архивация почты по IMAP с экспортом в PST."""
import collections
import copy
import html
import os
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QColor, QCursor
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QFileDialog, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QProgressBar, QSpinBox, QSplitter,
                               QStyle, QTableWidget, QTableWidgetItem, QTabWidget, QToolBar,
                               QVBoxLayout,
                               QWidget, QSizePolicy)

from core.downloader import Downloader
from core.models import APP_NAME, Config, app_data_dir
from core.storage import MailboxArchive
from gui.account_dialog import AccountDialog
from gui import theme
from gui.common import progress_text, run_bg, set_progress
from gui.export_dialog import ExportDialog
from gui.viewer import ViewerWindow

COLS = ["", "E-mail", "Сервер", "Период", "Папки", "Статус", "Прогресс",
        "Загружено", "Уже в архиве", "Ошибки"]
C_CHECK, C_EMAIL = 0, 1
C_STATUS, C_PROGRESS, C_DL, C_ALREADY, C_ERR = 5, 6, 7, 8, 9


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


def _same_path(a, b) -> bool:
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except OSError:
        return os.path.normcase(str(a)) == os.path.normcase(str(b))


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
        self.checked = set()         # id отмеченных галочкой ящиков
        self._exports = []           # открытые окна экспорта (немодальные)
        self._log_views = {}         # e-mail -> QPlainTextEdit вкладки журнала
        self._log_kind = {}          # e-mail -> итог (для цвета вкладки)
        self._filling = False
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
        self.a_edit = act("Изменить", SP.SP_FileDialogDetailedView, self._edit,
                          "Изменить выделенный ящик")
        self.a_dup = act("Копировать", SP.SP_FileDialogListView, self._duplicate,
                         "Создать ящик на основе выбранного (тот же сервер и настройки)")
        self.a_del = act("Удалить", SP.SP_TrashIcon, self._delete,
                         "Удалить отмеченные ящики (или выделенный, если галочек нет)")
        tb.addSeparator()
        self.a_run_sel = act("Запустить выбранные", SP.SP_MediaPlay, self._run_selected,
                             "Запустить загрузку отмеченных ящиков (или выделенного)")
        self.a_run_all = act("Запустить все", SP.SP_MediaSeekForward, self._run_all)
        self.a_stop = act("Остановить", SP.SP_MediaStop, self._stop_selected,
                          "Остановить отмеченные/выделенные (если таких нет — все)")
        tb.addSeparator()
        self.a_view = act("Просмотр писем", SP.SP_FileDialogContentsView, self._view,
                          "Просмотреть скачанные письма (выбранного ящика или всех)")
        self.a_export = act("Экспорт в PST", SP.SP_DialogSaveButton, self._export,
                            "Экспорт в PST (отмеченные ящики будут выбраны заранее)")
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
        hh.setSectionResizeMode(C_CHECK, QHeaderView.ResizeMode.Fixed)
        for c, w in enumerate([34, 220, 190, 150, 130, 0, 210, 85, 95, 65]):
            if w:
                self.table.setColumnWidth(c, w)
        hdr = QTableWidgetItem("✔")
        hdr.setToolTip("Отметить все ящики / снять все отметки")
        self.table.setHorizontalHeaderItem(C_CHECK, hdr)
        hh.sectionClicked.connect(self._header_clicked)
        self.table.doubleClicked.connect(self._double_clicked)
        self.table.itemSelectionChanged.connect(self._sync_actions)
        self.table.itemChanged.connect(self._item_changed)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        split.addWidget(self.table)

        self.log_tabs = QTabWidget()
        self.log_tabs.setDocumentMode(True)
        self.log_tabs.setUsesScrollButtons(True)
        self.log = self._new_log_view()             # вкладка «Все»: общий журнал
        self.log_tabs.addTab(self.log, "Все")
        self.log_tabs.setTabToolTip(0, "Журнал всех ящиков вместе")
        split.addWidget(self.log_tabs)
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
            f"Архив: {self.cfg.archive_dir}   |   Работает: {len(self.running)}"
            f"   |   Отмечено ящиков: {len(self.checked)}")

    # ================= таблица =================
    def _row_of(self, acc_id):
        for r, a in enumerate(self.cfg.accounts):
            if a.id == acc_id:
                return r
        return -1

    def _selected_accounts(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.cfg.accounts[r] for r in rows if r < len(self.cfg.accounts)]

    def _checked_accounts(self):
        return [a for a in self.cfg.accounts if a.id in self.checked]

    def _targets(self):
        """Ящики для действия: отмеченные галочкой, а если галочек нет — выделенные."""
        return self._checked_accounts() or self._selected_accounts()

    def _item_changed(self, item):
        if self._filling or item.column() != C_CHECK:
            return
        r = item.row()
        if r >= len(self.cfg.accounts):
            return
        acc_id = self.cfg.accounts[r].id
        if item.checkState() == Qt.CheckState.Checked:
            self.checked.add(acc_id)
        else:
            self.checked.discard(acc_id)
        self._sync_actions()

    def _header_clicked(self, section):
        if section != C_CHECK or not self.cfg.accounts:
            return
        all_on = len(self.checked) == len(self.cfg.accounts)
        self.checked = set() if all_on else {a.id for a in self.cfg.accounts}
        self._refresh_table()

    def _double_clicked(self, index):
        if index.column() != C_CHECK:
            self._edit()

    def _refresh_table(self):
        self._filling = True
        try:
            ids = {a.id for a in self.cfg.accounts}
            self.checked &= ids
            self.table.setRowCount(len(self.cfg.accounts))
            for r, a in enumerate(self.cfg.accounts):
                self.state.setdefault(a.id, new_state())
                chk = QTableWidgetItem()
                chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled |
                             Qt.ItemFlag.ItemIsSelectable)
                chk.setCheckState(Qt.CheckState.Checked if a.id in self.checked
                                  else Qt.CheckState.Unchecked)
                self.table.setItem(r, C_CHECK, chk)
                for c, text in enumerate([a.email, f"{a.host}:{a.port}", a.period_text(),
                                          a.folder_text()], start=C_EMAIL):
                    self.table.setItem(r, c, QTableWidgetItem(text))
                pb = QProgressBar()
                pb.setTextVisible(True)
                self.table.setCellWidget(r, C_PROGRESS, pb)
                self._update_row(a.id)
        finally:
            self._filling = False
        self._sync_actions()

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
            if s["total"]:
                set_progress(pb, s["done"], s["total"])
                pb.setToolTip(f"Скачано новых писем: {progress_text(s['done'], s['total'])}")
            else:
                pb.setRange(0, 1)
                finished_ok = s["kind"] in ("ok", "warn")
                pb.setValue(1 if finished_ok else 0)
                pb.setFormat("Новых писем нет (100%)" if finished_ok else "")
                pb.setToolTip("")

    def _sync_actions(self):
        if not hasattr(self, "a_edit"):
            return
        sel = self._selected_accounts()
        targets = self._targets()
        self.a_edit.setEnabled(len(sel) == 1 and sel[0].id not in self.running)
        self.a_dup.setEnabled(len(sel) == 1)
        self.a_del.setEnabled(any(a.id not in self.running for a in targets))
        self.a_run_sel.setEnabled(any(a.id not in self.running for a in targets))
        n = len(self.checked)
        self.a_run_sel.setText(f"Запустить отмеченные ({n})" if n else "Запустить выбранные")
        if len(sel) == 1:
            self._show_log_of(sel[0].email)
        self.a_stop.setEnabled(bool(self.running))
        self.a_arch.setEnabled(not self.running)
        self._update_statusbar()

    # ================= управление ящиками =================
    def _add(self):
        d = AccountDialog(self, default_archive=self.cfg.archive_dir)
        if d.exec():
            self.cfg.accounts.append(d.account)
            self.cfg.save()
            self._refresh_table()

    def _edit(self, *_):
        sel = self._selected_accounts()
        if len(sel) == 1:
            self._edit_account(sel[0])

    def _edit_account(self, acc):
        if acc.id in self.running:
            QMessageBox.information(self, "Изменение", "Идёт загрузка этого ящика — "
                                    "сначала остановите её.")
            return
        d = AccountDialog(self, acc, default_archive=self.cfg.archive_dir)
        if not d.exec():
            return
        new = d.account
        old_dir, new_dir = self.cfg.mailbox_dir(acc), self.cfg.mailbox_dir(new)
        if _same_path(old_dir, new_dir) or not (old_dir / MailboxArchive.META).exists():
            self._apply_account(acc.id, new)
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Папка для писем изменилась")
        box.setText(f"Уже скачанные письма этого ящика лежат в:\n{old_dir}\n\n"
                    f"Новая папка:\n{new_dir}\n\nПеренести письма в новую папку?")
        box.setInformativeText("Если не переносить, в новой папке загрузка начнётся заново, "
                               "а старые письма останутся на прежнем месте.")
        R = QMessageBox.ButtonRole
        b_move = box.addButton("Перенести письма", R.AcceptRole)
        b_keep = box.addButton("Не переносить", R.DestructiveRole)
        box.addButton("Отмена", R.RejectRole)
        box.setDefaultButton(b_move)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b_keep:
            self._apply_account(acc.id, new)
        elif clicked is b_move:
            self._move_mailbox(acc, new, old_dir, new_dir)

    def _apply_account(self, acc_id, new):
        self.cfg.accounts[self._row_of(acc_id)] = new
        self.cfg.save()
        self._refresh_table()

    def _move_mailbox(self, acc, new, old_dir, new_dir):
        if new_dir.exists() and any(new_dir.iterdir()):
            QMessageBox.warning(self, "Перенос писем",
                                f"В новой папке уже есть письма этого ящика:\n{new_dir}\n\n"
                                "Перенос отменён, настройки не изменены. Выберите пустую папку "
                                "или перенесите письма вручную.")
            return
        self.running[acc.id] = threading.Event()        # блокирует запуск и изменение
        self.state[acc.id].update(status="Перенос писем в новую папку…", kind="running",
                                  done=0, total=0)
        self._update_row(acc.id)
        self._sync_actions()
        self._write_log(acc.email, f"Перенос писем: {old_dir} → {new_dir}")

        def job():
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            if new_dir.exists():
                new_dir.rmdir()                          # пустая — убираем, чтобы move не вложил
            shutil.move(str(old_dir), str(new_dir))     # тот же диск — мгновенно
            return True

        def done(_):
            self.running.pop(acc.id, None)
            self._apply_account(acc.id, new)
            self.state[new.id].update(status="Письма перенесены в новую папку", kind="ok")
            self._update_row(new.id)
            self._write_log(new.email, "Перенос завершён")
            self._sync_actions()

        def fail(err):
            self.running.pop(acc.id, None)
            self.state[acc.id].update(status=f"Перенос не удался: {err}", kind="error")
            self._update_row(acc.id)
            self._write_log(acc.email, f"Перенос не удался, настройки не изменены: {err}")
            self._sync_actions()
            QMessageBox.warning(self, "Перенос писем",
                                f"Не удалось перенести письма:\n{err}\n\nНастройки ящика не "
                                f"изменены. Проверьте обе папки:\n{old_dir}\n{new_dir}")
        run_bg(job, done, fail)

    def _duplicate(self):
        sel = self._selected_accounts()
        if len(sel) != 1:
            return
        base = copy.deepcopy(sel[0])
        from core.models import Account
        base.id = Account().id
        base.email = base.login = ""
        base.password = ""
        d = AccountDialog(self, base, default_archive=self.cfg.archive_dir)
        d.setWindowTitle("Добавить почтовый ящик")
        if d.exec():
            self.cfg.accounts.append(d.account)
            self.cfg.save()
            self._refresh_table()

    def _delete(self):
        self._delete_accounts(self._targets())

    def _delete_accounts(self, accounts):
        sel = [a for a in accounts if a.id not in self.running]
        if not sel:
            return
        names = "\n".join(a.email for a in sel)
        if QMessageBox.question(self, "Удаление", f"Удалить из списка?\n\n{names}\n\n"
                                "Уже скачанные письма в архиве останутся на диске.") \
                != QMessageBox.StandardButton.Yes:
            return
        ids = {a.id for a in sel}
        self.cfg.accounts = [a for a in self.cfg.accounts if a.id not in ids]
        self.checked -= ids
        for i in ids:
            self.state.pop(i, None)
        for a in sel:
            self._remove_log_tab(a.email)
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
            for email in list(self._log_views):
                self._color_log_tab(email)
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
        path = self.cfg.mailbox_dir(sel[0]) if len(sel) == 1 else self.cfg.archive_dir
        os.makedirs(path, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(str(path))

    def _view(self):
        sel = self._selected_accounts()
        self._view_account(sel[0] if len(sel) == 1 else None)

    def _view_account(self, acc):
        email = acc.email if acc else None
        if self._viewer is None:
            self._viewer = ViewerWindow(self, self.cfg, email)
            self._viewer.finished.connect(lambda *_: setattr(self, "_viewer", None))
            self._viewer.show()
        else:
            self._viewer._fill_tree(email)
            self._viewer.showNormal()
            self._viewer.raise_()
            self._viewer.activateWindow()

    def _export(self):
        checked = self._checked_accounts()
        self._export_accounts(checked or None)

    def _export_accounts(self, accounts):
        """Окно экспорта — отдельное: главное окно и загрузка работают параллельно.
        Можно открыть несколько окон экспорта для разных ящиков."""
        d = ExportDialog(self, self.cfg, [a.email for a in accounts] if accounts else None,
                         log_sink=self.log_for)
        self._exports.append(d)
        d.closed.connect(self._export_closed)
        d.show()
        d.raise_()
        d.activateWindow()

    def _export_closed(self, d):
        if d in self._exports:
            self._exports.remove(d)
        try:
            self.cfg.save()
        except Exception:
            pass
        d.deleteLater()

    # ================= контекстное меню =================
    def _context_menu(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0 or row >= len(self.cfg.accounts):
            return
        acc = self.cfg.accounts[row]
        if acc.id in self.checked and len(self.checked) > 1:
            targets = self._checked_accounts()          # действие для всех отмеченных
        else:
            targets = [acc]
            if acc not in self._selected_accounts():
                self.table.selectRow(row)
        many = len(targets) > 1
        suffix = f" ({len(targets)})" if many else ""
        idle = [a for a in targets if a.id not in self.running]
        busy = [a for a in targets if a.id in self.running]
        st = self.style()
        SP = QStyle.StandardPixmap

        m = QMenu(self)
        a_run = m.addAction(st.standardIcon(SP.SP_MediaPlay),
                            ("Запуск загрузки писем отмеченных ящиков" if many
                             else "Запуск загрузки писем") + suffix)
        a_run.setEnabled(bool(idle))
        a_stop = None
        if busy:
            a_stop = m.addAction(st.standardIcon(SP.SP_MediaStop),
                                 "Остановить загрузку" + (f" ({len(busy)})" if many else ""))
        a_exp = m.addAction(st.standardIcon(SP.SP_DialogSaveButton),
                            ("Экспорт отмеченных ящиков в PST" if many
                             else "Экспорт почтового ящика в PST") + suffix)
        a_view = m.addAction(st.standardIcon(SP.SP_FileDialogContentsView), "Просмотр писем")
        m.addSeparator()
        a_edit = m.addAction(st.standardIcon(SP.SP_FileDialogDetailedView),
                             "Изменение почтового ящика")
        a_edit.setEnabled(not many and acc.id not in self.running)
        a_del = m.addAction(st.standardIcon(SP.SP_TrashIcon),
                            ("Удаление отмеченных ящиков" if many
                             else "Удаление почтового ящика") + suffix)
        a_del.setEnabled(bool(idle))

        a_run.triggered.connect(lambda: self._start(idle))
        if a_stop is not None:
            a_stop.triggered.connect(lambda: [self._stop_one(x.id) for x in busy])
        a_exp.triggered.connect(lambda: self._export_accounts(targets))
        a_view.triggered.connect(lambda: self._view_account(acc))
        a_edit.triggered.connect(lambda: self._edit_account(acc))
        a_del.triggered.connect(lambda: self._delete_accounts(idle))
        m.exec(QCursor.pos())

    # ================= запуск / остановка =================
    def _run_selected(self):
        self._start(self._targets())

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
            d = Downloader(copy.deepcopy(a), self.cfg.archive_root_for(a),
                           Callbacks(self.bridge, a.id), stop, self.sem)
            threading.Thread(target=d.run, name=f"dl-{a.email}", daemon=True).start()
            self._write_log(a.email, "Запуск загрузки")
        self._sync_actions()

    def _stop_selected(self):
        sel = [a for a in self._targets() if a.id in self.running]
        targets = [a.id for a in sel] if sel else list(self.running)
        for acc_id in targets:
            self._stop_one(acc_id)

    def _stop_one(self, acc_id):
        ev = self.running.get(acc_id)
        if ev is None:
            return
        ev.set()
        self.state[acc_id].update(status="Остановка…")
        self._update_row(acc_id)

    # ================= сигналы от потоков =================
    def _email(self, acc_id):
        r = self._row_of(acc_id)
        return self.cfg.accounts[r].email if r >= 0 else acc_id

    @staticmethod
    def _new_log_view():
        v = QPlainTextEdit(readOnly=True)
        v.setMaximumBlockCount(20000)
        return v

    def _log_view(self, email: str):
        """Вкладка журнала ящика (создаётся при первой записи)."""
        key = email.strip().lower()
        v = self._log_views.get(key)
        if v is None:
            v = self._new_log_view()
            self._log_views[key] = v
            i = self.log_tabs.addTab(v, email)
            self.log_tabs.setTabToolTip(i, f"Журнал ящика {email}")
        return v

    def _color_log_tab(self, email: str):
        key = email.strip().lower()
        v = self._log_views.get(key)
        if v is None:
            return
        i = self.log_tabs.indexOf(v)
        kind = self._log_kind.get(key)
        color = theme.status_color(kind) if kind in ("error", "warn") else theme.color("text")
        self.log_tabs.tabBar().setTabTextColor(i, QColor(color))

    def _remove_log_tab(self, email: str):
        v = self._log_views.pop(email.strip().lower(), None)
        self._log_kind.pop(email.strip().lower(), None)
        if v is not None:
            self.log_tabs.removeTab(self.log_tabs.indexOf(v))
            v.deleteLater()

    def _show_log_of(self, email: str):
        v = self._log_views.get(email.strip().lower())
        if v is not None:
            self.log_tabs.setCurrentWidget(v)

    def log_for(self, email, text):
        """Приёмник журнала из окна экспорта."""
        self._write_log(email, text)

    def _write_log(self, email, text):
        stamp = f"[{datetime.now():%H:%M:%S}]"
        line = f"{stamp} {email}: {text}"
        self.log.appendPlainText(line)
        if email and "@" in str(email):
            self._log_view(email).appendPlainText(f"{stamp} {text}")
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
        email = self._email(acc_id)
        self._write_log(email, msg)
        self._log_kind[email.strip().lower()] = kind
        self._color_log_tab(email)
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
        busy_exports = [d for d in self._exports if d.busy()]
        if self.running or busy_exports:
            what = []
            if self.running:
                what.append("загрузка почты")
            if busy_exports:
                what.append("экспорт в PST")
            if QMessageBox.question(self, "Выход",
                                    f"Сейчас идёт {' и '.join(what)}. Остановить и выйти?\n"
                                    "Уже скачанные письма сохранятся, при следующем запуске "
                                    "загрузка продолжится. Уже записанные в PST письма "
                                    "останутся в файле.") != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            for stop in self.running.values():
                stop.set()
            for d in busy_exports:
                d.stop_and_close()
            # PST-файл нужно корректно дописать, иначе он будет повреждён
            self.statusBar().showMessage("Завершаю экспорт: дописываю структуру PST-файлов…")
            while any(d.busy() for d in busy_exports):
                QApplication.processEvents()
                time.sleep(0.05)
        for d in list(self._exports):
            d.close()
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
