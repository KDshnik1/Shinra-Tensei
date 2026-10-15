"""Окно экспорта архива в PST (встроенный движок, Outlook не требуется)."""
import os
import threading
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout)

from core.pst_export import check_password, export_mailbox
from core.storage import MailboxArchive, safe_name


class _Bridge(QObject):
    log = Signal(str)
    progress = Signal(int, int)
    current = Signal(str)
    done = Signal(object)


class ExportDialog(QDialog):
    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Экспорт в Outlook PST")
        self.resize(680, 680)
        self.stop = threading.Event()
        self.thread = None
        self.bridge = _Bridge()
        self.bridge.log.connect(self._log)
        self.bridge.progress.connect(self._progress)
        self.bridge.current.connect(lambda t: self.lbl_cur.setText(t))
        self.bridge.done.connect(self._finished)
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        g = QGroupBox("Почтовые ящики из архива")
        v = QVBoxLayout(g)
        self.lst = QListWidget()
        for a in self.cfg.accounts:
            arch = MailboxArchive(self.cfg.archive_dir, a.email)
            has = (arch.dir / MailboxArchive.META).exists()
            it = QListWidgetItem(a.email + ("" if has else "   (нет загруженных писем)"))
            it.setData(Qt.ItemDataRole.UserRole, a.email)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if has else Qt.CheckState.Unchecked)
            if not has:
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.lst.addItem(it)
        v.addWidget(self.lst)
        lay.addWidget(g)

        g = QGroupBox("Куда сохранить")
        v = QVBoxLayout(g)
        h = QHBoxLayout()
        h.addWidget(QLabel("Папка для PST:"))
        self.ed_out = QLineEdit(self.cfg.pst_dir)
        h.addWidget(self.ed_out, 1)
        b = QPushButton("Обзор…")
        b.clicked.connect(self._browse)
        h.addWidget(b)
        v.addLayout(h)
        info = QLabel("Для каждого ящика создаётся отдельный файл <e-mail>.pst. Файл собирается "
                      "самой программой — Outlook не нужен. Ящики больше ~1,9 ГБ "
                      "автоматически делятся на части.")
        info.setWordWrap(True)
        v.addWidget(info)
        lay.addWidget(g)

        g = QGroupBox("Защита паролем")
        v = QVBoxLayout(g)
        self.chk_pw = QCheckBox("Установить пароль на PST-файл")
        self.chk_pw.toggled.connect(self._sync_pw)
        v.addWidget(self.chk_pw)
        h = QHBoxLayout()
        self.ed_pw1 = QLineEdit(echoMode=QLineEdit.EchoMode.Password, placeholderText="Пароль")
        self.ed_pw2 = QLineEdit(echoMode=QLineEdit.EchoMode.Password,
                                placeholderText="Повторите пароль")
        self.ed_pw1.setMaxLength(15)
        self.ed_pw2.setMaxLength(15)
        h.addWidget(self.ed_pw1)
        h.addWidget(self.ed_pw2)
        v.addLayout(h)
        hint = QLabel("До 15 символов: латинские буквы, цифры и знаки (ограничение Outlook). "
                      "Outlook спросит пароль при открытии файла. Программа пароль не "
                      "сохраняет — запомните его.")
        hint.setWordWrap(True)
        v.addWidget(hint)
        lay.addWidget(g)
        self._sync_pw()

        self.lbl_cur = QLabel()
        lay.addWidget(self.lbl_cur)
        self.pb = QProgressBar()
        lay.addWidget(self.pb)
        self.log = QPlainTextEdit(readOnly=True)
        lay.addWidget(self.log, 1)

        h = QHBoxLayout()
        self.btn_folder = QPushButton("Открыть папку с PST")
        self.btn_folder.clicked.connect(self._open_folder)
        h.addWidget(self.btn_folder)
        h.addStretch()
        self.btn_start = QPushButton("Экспортировать")
        self.btn_start.setObjectName("Primary")
        self.btn_start.clicked.connect(self._start)
        self.btn_stop = QPushButton("Остановить")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop.set)
        self.btn_close = QPushButton("Закрыть")
        self.btn_close.clicked.connect(self.close)
        for w in (self.btn_start, self.btn_stop, self.btn_close):
            h.addWidget(w)
        lay.addLayout(h)

    def _sync_pw(self):
        on = self.chk_pw.isChecked()
        self.ed_pw1.setEnabled(on)
        self.ed_pw2.setEnabled(on)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Папка для PST", self.ed_out.text())
        if d:
            self.ed_out.setText(d)

    def _open_folder(self):
        p = Path(self.ed_out.text().strip())
        p.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(str(p))

    def _log(self, t):
        self.log.appendPlainText(f"[{datetime.now():%H:%M:%S}] {t}")

    def _progress(self, done, total):
        self.pb.setRange(0, max(total, 1))
        self.pb.setValue(done)
        self.pb.setFormat(f"{done} / {total}")

    def _start(self):
        emails = [self.lst.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.lst.count())
                  if self.lst.item(i).checkState() == Qt.CheckState.Checked]
        if not emails:
            QMessageBox.information(self, "Экспорт", "Отметьте хотя бы один ящик.")
            return
        password = ""
        if self.chk_pw.isChecked():
            password = self.ed_pw1.text()
            if not password:
                QMessageBox.warning(self, "Пароль", "Введите пароль для PST.")
                return
            if password != self.ed_pw2.text():
                QMessageBox.warning(self, "Пароль", "Пароли не совпадают.")
                return
            err = check_password(password)
            if err:
                QMessageBox.warning(self, "Пароль", err)
                return
        out = Path(self.ed_out.text().strip())
        self.cfg.pst_dir = str(out)
        self.stop.clear()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.thread = threading.Thread(target=self._worker, args=(emails, out, password),
                                       daemon=True)
        self.thread.start()

    def _worker(self, emails, out: Path, password):
        b = self.bridge

        class CB:
            log = b.log.emit
            progress = b.progress.emit
        results = []
        for email in emails:
            if self.stop.is_set():
                break
            arch = MailboxArchive(self.cfg.archive_dir, email)
            pst = out / f"{safe_name(email, 100)}.pst"
            if pst.exists():
                pst = out / f"{safe_name(email, 100)}_{datetime.now():%Y%m%d_%H%M%S}.pst"
            b.current.emit(f"Экспорт: {email} → {pst.name}")
            b.log.emit(f"=== {email} → {pst}")
            try:
                ok, err, files = export_mailbox(arch.dir, pst, email, CB, self.stop, password)
                b.log.emit(f"{email}: записано писем {ok}, ошибок {err}, файлов {len(files)}")
                results.append((email, ok, err))
            except Exception as e:
                b.log.emit(f"{email}: ОШИБКА — {e}")
                b.log.emit(traceback.format_exc())
                results.append((email, 0, -1))
        b.done.emit(results)

    def _finished(self, results):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        if self.stop.is_set():
            self.lbl_cur.setText("Экспорт остановлен")
            return
        bad = [r for r in results if r[2] != 0]
        self.lbl_cur.setText("Экспорт завершён" + (" с ошибками — см. журнал" if bad else ""))

    def closeEvent(self, e):
        if self.thread and self.thread.is_alive():
            if QMessageBox.question(self, "Экспорт", "Экспорт ещё идёт. Остановить?") \
                    != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.stop.set()
        e.accept()
