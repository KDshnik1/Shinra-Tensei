"""Окно экспорта архива в PST (встроенный движок, Outlook не требуется)."""
import os
import threading
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QDoubleSpinBox, QFileDialog, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QVBoxLayout)

from core.pst_export import (MAX_SPLIT_GB, MIN_SPLIT_GB, check_password, collect_tasks,
                             export_mailbox)
from core.storage import MailboxArchive, safe_name
from core.winpower import keeper
from .common import fmt_num, set_progress
from core.i18n import dec, tr


class _Bridge(QObject):
    log = Signal(str)
    progress = Signal(int, int)          # текущий ящик: экспортировано, всего
    overall = Signal(int, int)           # все выбранные ящики
    current = Signal(str)
    done = Signal(object)
    mailbox_log = Signal(str, str)       # (e-mail, строка) — в журнал главного окна


class ExportDialog(QDialog):
    """Отдельное (немодальное) окно: главное окно программы остаётся доступным."""
    closed = Signal(object)

    def __init__(self, parent, cfg, preselect=None, log_sink=None):
        """preselect — список e-mail, которые отметить; None — все ящики с письмами.
        log_sink(email, text) — куда дублировать журнал (вкладки главного окна)."""
        super().__init__(None)              # без владельца: своё окно на панели задач
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowMinimizeButtonHint |
                            Qt.WindowType.WindowMaximizeButtonHint |
                            Qt.WindowType.WindowCloseButtonHint)
        self.cfg = cfg
        self._close_after = False
        self._last_reports = []
        self.preselect = {e.strip().lower() for e in preselect} if preselect else None
        self.setWindowTitle(tr('Экспорт в Outlook PST'))
        self.resize(700, 720)
        self.stop = threading.Event()
        self.thread = None
        self._running_emails = []
        self.bridge = _Bridge()
        self.bridge.log.connect(self._log)
        self.bridge.progress.connect(
            lambda d, t: set_progress(self.pb, d, t, tr('Экспортировано писем: ')))
        self.bridge.overall.connect(
            lambda d, t: set_progress(self.pb_total, d, t, tr('Всего: ')))
        self.bridge.current.connect(lambda t: self.lbl_cur.setText(t))
        self.bridge.done.connect(self._finished)
        if log_sink:
            self.bridge.mailbox_log.connect(log_sink)
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        g = QGroupBox(tr('Почтовые ящики из архива'))
        v = QVBoxLayout(g)
        self.lst = QListWidget()
        for a in self.cfg.accounts:
            has = (self.cfg.mailbox_dir(a) / MailboxArchive.META).exists()
            it = QListWidgetItem(a.email + ("" if has else tr('   (нет загруженных писем)')))
            it.setData(Qt.ItemDataRole.UserRole, a.email)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            wanted = has and (self.preselect is None or a.email.strip().lower() in self.preselect)
            it.setCheckState(Qt.CheckState.Checked if wanted else Qt.CheckState.Unchecked)
            if not has:
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.lst.addItem(it)
        v.addWidget(self.lst)
        h = QHBoxLayout()
        b_all = QPushButton(tr('Отметить все'))
        b_all.clicked.connect(lambda: self._check_all(True))
        b_none = QPushButton(tr('Снять все'))
        b_none.clicked.connect(lambda: self._check_all(False))
        h.addWidget(b_all)
        h.addWidget(b_none)
        h.addStretch()
        v.addLayout(h)
        lay.addWidget(g)

        g = QGroupBox(tr('Куда сохранить'))
        v = QVBoxLayout(g)
        h = QHBoxLayout()
        h.addWidget(QLabel(tr('Папка для PST:')))
        self.ed_out = QLineEdit(self.cfg.pst_dir)
        h.addWidget(self.ed_out, 1)
        b = QPushButton(tr('Обзор…'))
        b.clicked.connect(self._browse)
        h.addWidget(b)
        v.addLayout(h)
        h = QHBoxLayout()
        h.addWidget(QLabel(tr('Размер одной части PST:')))
        self.sp_split = QDoubleSpinBox()
        self.sp_split.setRange(MIN_SPLIT_GB, MAX_SPLIT_GB)
        self.sp_split.setDecimals(1)
        self.sp_split.setSingleStep(1.0)
        self.sp_split.setSuffix(tr(' ГБ'))
        self.sp_split.setValue(min(max(float(self.cfg.split_gb), MIN_SPLIT_GB), MAX_SPLIT_GB))
        self.sp_split.setToolTip(dec(tr('От {MIN_SPLIT_GB:g} до {MAX_SPLIT_GB:g} ГБ (предел Outlook для одного файла — 50 ГБ)').format(MIN_SPLIT_GB=MIN_SPLIT_GB, MAX_SPLIT_GB=MAX_SPLIT_GB)))
        self.sp_split.valueChanged.connect(self._split_changed)
        h.addWidget(self.sp_split)
        h.addStretch()
        v.addLayout(h)
        self.lbl_info = QLabel()
        self.lbl_info.setWordWrap(True)
        v.addWidget(self.lbl_info)
        self._split_changed(self.sp_split.value())
        self.chk_dups = QCheckBox(tr('Не записывать повторы писем (то же письмо в нескольких папках, например «Вся почта» в Gmail)'))
        self.chk_dups.setChecked(self.cfg.export_skip_duplicates)
        v.addWidget(self.chk_dups)
        lay.addWidget(g)

        g = QGroupBox(tr('Защита паролем'))
        v = QVBoxLayout(g)
        self.chk_pw = QCheckBox(tr('Установить пароль на PST-файл'))
        self.chk_pw.toggled.connect(self._sync_pw)
        v.addWidget(self.chk_pw)
        h = QHBoxLayout()
        self.ed_pw1 = QLineEdit(echoMode=QLineEdit.EchoMode.Password, placeholderText=tr('Пароль'))
        self.ed_pw2 = QLineEdit(echoMode=QLineEdit.EchoMode.Password,
                                placeholderText=tr('Повторите пароль'))
        self.ed_pw1.setMaxLength(15)
        self.ed_pw2.setMaxLength(15)
        h.addWidget(self.ed_pw1)
        h.addWidget(self.ed_pw2)
        v.addLayout(h)
        hint = QLabel(tr('До 15 символов: латинские буквы, цифры и знаки (ограничение Outlook). Outlook спросит пароль при открытии файла. Программа пароль не сохраняет — запомните его.'))
        hint.setWordWrap(True)
        v.addWidget(hint)
        lay.addWidget(g)
        self._sync_pw()

        g = QGroupBox(tr('Ход экспорта'))
        v = QVBoxLayout(g)
        self.lbl_cur = QLabel(tr('Отметьте ящики и нажмите «Экспортировать»'))
        self.lbl_cur.setWordWrap(True)
        v.addWidget(self.lbl_cur)
        v.addWidget(QLabel(tr('Текущий ящик:')))
        self.pb = QProgressBar()
        self.pb.setTextVisible(True)
        set_progress(self.pb, 0, 0, tr('Экспортировано писем: '))
        v.addWidget(self.pb)
        v.addWidget(QLabel(tr('Все выбранные ящики:')))
        self.pb_total = QProgressBar()
        self.pb_total.setTextVisible(True)
        set_progress(self.pb_total, 0, 0, tr('Всего: '))
        v.addWidget(self.pb_total)
        lay.addWidget(g)

        self.log = QPlainTextEdit(readOnly=True)
        lay.addWidget(self.log, 1)

        h = QHBoxLayout()
        self.btn_folder = QPushButton(tr('Открыть папку с PST'))
        self.btn_folder.clicked.connect(self._open_folder)
        h.addWidget(self.btn_folder)
        self.btn_report = QPushButton(tr('Открыть отчёт'))
        self.btn_report.setToolTip(tr('Какие папки и за какие даты попали в каждую часть PST'))
        self.btn_report.setEnabled(False)
        self.btn_report.clicked.connect(self._open_report)
        h.addWidget(self.btn_report)
        h.addStretch()
        self.btn_start = QPushButton(tr('Экспортировать'))
        self.btn_start.setObjectName("Primary")
        self.btn_start.clicked.connect(self._start)
        self.btn_stop = QPushButton(tr('Остановить'))
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop.set)
        self.btn_close = QPushButton(tr('Закрыть'))
        self.btn_close.clicked.connect(self.close)
        for w in (self.btn_start, self.btn_stop, self.btn_close):
            h.addWidget(w)
        lay.addLayout(h)

    def _split_changed(self, v):
        gb = dec(f"{v:g}")
        self.lbl_info.setText(
            tr('Для каждого ящика создаётся отдельный файл <e-mail>.pst. Файл собирается самой программой — Outlook не нужен. Если ящик больше {gb} ГБ, он делится на части (_часть2.pst, _часть3.pst…). Рядом сохраняется отчёт: какие папки и за какие даты лежат в каждой части.').format(gb=gb))

    def busy(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def exporting(self) -> set:
        return set(self._running_emails) if self.busy() else set()

    def _open_report(self):
        if not self._last_reports or not hasattr(os, "startfile"):
            return
        if len(self._last_reports) == 1:
            os.startfile(str(self._last_reports[0]))
        else:
            os.startfile(str(self._last_reports[0].parent))

    def _check_all(self, on: bool):
        for i in range(self.lst.count()):
            it = self.lst.item(i)
            if it.flags() & Qt.ItemFlag.ItemIsEnabled:
                it.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def _sync_pw(self):
        on = self.chk_pw.isChecked()
        self.ed_pw1.setEnabled(on)
        self.ed_pw2.setEnabled(on)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, tr('Папка для PST'), self.ed_out.text())
        if d:
            self.ed_out.setText(d)

    def _open_folder(self):
        p = Path(self.ed_out.text().strip())
        p.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(str(p))

    def _log(self, t):
        self.log.appendPlainText(f"[{datetime.now():%H:%M:%S}] {t}")

    def _start(self):
        emails = [self.lst.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.lst.count())
                  if self.lst.item(i).checkState() == Qt.CheckState.Checked]
        if not emails:
            QMessageBox.information(self, tr('Экспорт'), tr('Отметьте хотя бы один ящик.'))
            return
        password = ""
        if self.chk_pw.isChecked():
            password = self.ed_pw1.text()
            if not password:
                QMessageBox.warning(self, tr('Пароль'), tr('Введите пароль для PST.'))
                return
            if password != self.ed_pw2.text():
                QMessageBox.warning(self, tr('Пароль'), tr('Пароли не совпадают.'))
                return
            err = check_password(password)
            if err:
                QMessageBox.warning(self, tr('Пароль'), err)
                return
        out = Path(self.ed_out.text().strip())
        self.cfg.pst_dir = str(out)
        self.cfg.split_gb = self.sp_split.value()
        self.cfg.export_skip_duplicates = self.chk_dups.isChecked()
        try:
            self.cfg.save()
        except Exception:
            pass
        self._running_emails = list(emails)
        self._last_reports = []
        self.btn_report.setEnabled(False)
        self.sp_split.setEnabled(False)
        self.chk_dups.setEnabled(False)
        self.stop.clear()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        keeper.hold(("export", id(self)))
        set_progress(self.pb, 0, 0, tr('Экспортировано писем: '))
        set_progress(self.pb_total, 0, 0, tr('Всего: '))
        self.thread = threading.Thread(target=self._worker,
                                       args=(emails, out, password, self.sp_split.value(),
                                             self.chk_dups.isChecked()),
                                       daemon=True)
        self.thread.start()

    def _worker(self, emails, out: Path, password, split_gb, skip_dups):
        b = self.bridge
        b.current.emit(tr('Подсчёт писем…'))
        counts = {}
        for email in emails:
            try:
                counts[email] = sum(len(t[2]) for t in
                                    collect_tasks(self.cfg.mailbox_dir_by_email(email)))
            except Exception:
                counts[email] = 0
        grand = sum(counts.values())
        b.overall.emit(0, grand)
        b.log.emit(tr('К экспорту: {0} писем в {1} ящ.').format(fmt_num(grand), len(emails)))

        state = {"base": 0, "email": ""}

        class CB:
            @staticmethod
            def log(text):
                b.log.emit(text)
                if state["email"]:
                    b.mailbox_log.emit(state["email"], "[PST] " + text)

            @staticmethod
            def progress(done, total):
                b.progress.emit(done, total)
                b.overall.emit(state["base"] + done, grand)

            @staticmethod
            def stage(text):
                b.current.emit(f"{state['email']}: {text}")

        results = []
        for email in emails:
            if self.stop.is_set():
                break
            state["email"] = email
            mdir = self.cfg.mailbox_dir_by_email(email)
            pst = out / f"{safe_name(email, 100)}.pst"
            if pst.exists():
                pst = out / f"{safe_name(email, 100)}_{datetime.now():%Y%m%d_%H%M%S}.pst"
            b.current.emit(tr('Экспорт: {email} → {0}').format(pst.name, email=email))
            CB.log(tr('=== Экспорт в {pst}').format(pst=pst))
            try:
                ok, err, files, report = export_mailbox(mdir, pst, email, CB, self.stop,
                                                        password, split_gb, skip_dups)
                CB.log(tr('Готово: записано писем {0}, ошибок {err}, файлов PST: {1}').format(fmt_num(ok), len(files), err=err))
                results.append((email, ok, err, report))
            except Exception as e:
                CB.log(tr('ОШИБКА — {e}').format(e=e))
                b.log.emit(traceback.format_exc())
                results.append((email, 0, -1, None))
            state["base"] += counts.get(email, 0)
            b.overall.emit(min(state["base"], grand), grand)
        b.done.emit(results)

    def _finished(self, results):
        keeper.release(("export", id(self)))
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.sp_split.setEnabled(True)
        self.chk_dups.setEnabled(True)
        self._last_reports = [r[3] for r in results if r[3]]
        self.btn_report.setEnabled(bool(self._last_reports))
        if self._close_after:
            self.close()
            return
        if self.stop.is_set():
            self.lbl_cur.setText(tr('Экспорт остановлен'))
            return
        total_ok = sum(max(r[1], 0) for r in results)
        bad = [r for r in results if r[2] != 0]
        self.lbl_cur.setText(tr('Экспорт завершён: {0} писем').format(fmt_num(total_ok))
                             + (tr(' — есть ошибки, см. журнал') if bad else ""))

    def stop_and_close(self):
        """Остановить экспорт; окно закроется, когда текущая часть PST будет дописана."""
        self._close_after = True
        self.stop.set()
        self.btn_stop.setEnabled(False)
        self.lbl_cur.setText(tr('Остановка: дописываю структуру текущего файла PST…'))
        if not self.busy():
            self.close()

    def closeEvent(self, e):
        if self.busy():
            if not self._close_after:
                if QMessageBox.question(self, tr('Экспорт'), tr('Экспорт ещё идёт. Остановить?\nУже записанные письма останутся в PST-файле.')) \
                        != QMessageBox.StandardButton.Yes:
                    e.ignore()
                    return
                self.stop_and_close()
            e.ignore()                       # закроемся сами, когда поток завершится
            return
        e.accept()
        self.closed.emit(self)
