"""Окно настройки почтового ящика."""
import copy
import os
from datetime import date
from pathlib import Path

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDateEdit, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton,
                               QSpinBox, QVBoxLayout)

from core.imap_client import ImapConnection
from core.models import SECURITY_OPTIONS, Account
from core.storage import safe_name
from . import theme
from .common import run_bg
from core.i18n import QT_DATE_FMT, tr


class AccountDialog(QDialog):
    def __init__(self, parent=None, account: Account | None = None, default_archive: str = ""):
        super().__init__(parent)
        self.default_archive = default_archive
        self.account = copy.deepcopy(account) if account else Account()
        self.setWindowTitle(tr('Изменить почтовый ящик') if account else tr('Добавить почтовый ящик'))
        self.setMinimumWidth(560)
        self._build()
        self._load(self.account)

    # ---------- интерфейс ----------
    def _build(self):
        lay = QVBoxLayout(self)

        g = QGroupBox(tr('Подключение к серверу'))
        f = QFormLayout(g)
        self.ed_email = QLineEdit(placeholderText="user@example.com")
        self.ed_email.editingFinished.connect(self._autofill)
        self.ed_host = QLineEdit(placeholderText="imap.example.com")
        self.cb_sec = QComboBox()
        for text, val, _ in SECURITY_OPTIONS:
            self.cb_sec.addItem(text, val)
        self.cb_sec.currentIndexChanged.connect(self._sec_changed)
        self.sp_port = QSpinBox(minimum=1, maximum=65535, value=993)
        hp = QHBoxLayout()
        hp.addWidget(self.cb_sec, 1)
        hp.addWidget(QLabel(tr('Порт:')))
        hp.addWidget(self.sp_port)
        self.chk_certs = QCheckBox(tr('Принимать все сертификаты (просроченные, самоподписанные, с чужим именем)'))
        self.ed_login = QLineEdit(placeholderText=tr('обычно совпадает с e-mail'))
        self.ed_pass = QLineEdit(echoMode=QLineEdit.EchoMode.Password)
        self.chk_show = QCheckBox(tr('показать'))
        self.chk_show.toggled.connect(lambda on: self.ed_pass.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        hpw = QHBoxLayout()
        hpw.addWidget(self.ed_pass, 1)
        hpw.addWidget(self.chk_show)
        self.chk_save = QCheckBox(tr('Сохранить пароль (шифруется средствами Windows)'))
        f.addRow("E-mail:", self.ed_email)
        f.addRow(tr('Сервер IMAP:'), self.ed_host)
        f.addRow(tr('Протокол:'), hp)
        f.addRow("", self.chk_certs)
        f.addRow(tr('Логин:'), self.ed_login)
        f.addRow(tr('Пароль:'), hpw)
        f.addRow("", self.chk_save)
        lay.addWidget(g)

        g = QGroupBox(tr('Период'))
        v = QVBoxLayout(g)
        self.rb_all_time = QRadioButton(tr('За всё время'))
        self.rb_range = QRadioButton(tr('За период:'))
        bg = QButtonGroup(self)
        bg.addButton(self.rb_all_time)
        bg.addButton(self.rb_range)
        self.de_from = QDateEdit(calendarPopup=True, displayFormat=QT_DATE_FMT)
        self.de_to = QDateEdit(calendarPopup=True, displayFormat=QT_DATE_FMT)
        h = QHBoxLayout()
        h.addWidget(self.rb_range)
        h.addWidget(QLabel(tr('с')))
        h.addWidget(self.de_from)
        h.addWidget(QLabel(tr('по')))
        h.addWidget(self.de_to)
        h.addStretch()
        v.addWidget(self.rb_all_time)
        v.addLayout(h)
        self.rb_range.toggled.connect(self._sync_enabled)
        lay.addWidget(g)

        g = QGroupBox(tr('Папки'))
        v = QVBoxLayout(g)
        self.rb_all_folders = QRadioButton(tr('Все папки'))
        self.rb_one_folder = QRadioButton(tr('Только папка:'))
        bg2 = QButtonGroup(self)
        bg2.addButton(self.rb_all_folders)
        bg2.addButton(self.rb_one_folder)
        self.cb_folder = QComboBox()
        self.cb_folder.setMinimumWidth(240)
        self.btn_folders = QPushButton(tr('Загрузить список папок'))
        self.btn_folders.clicked.connect(self._load_folders)
        h = QHBoxLayout()
        h.addWidget(self.rb_one_folder)
        h.addWidget(self.cb_folder, 1)
        h.addWidget(self.btn_folders)
        v.addWidget(self.rb_all_folders)
        v.addLayout(h)
        self.chk_dups = QCheckBox(tr('Не загружать повторы писем из других папок (например, «Вся почта» в Gmail)'))
        self.chk_dups.setToolTip(tr('Письмо с тем же Message-ID и размером, что уже есть в архиве '
                                    'ящика, второй раз не скачивается'))
        v.addWidget(self.chk_dups)
        self.rb_one_folder.toggled.connect(self._sync_enabled)
        lay.addWidget(g)

        g = QGroupBox(tr('Куда сохранять письма (.eml)'))
        v = QVBoxLayout(g)
        self.rb_arch_default = QRadioButton(tr('В общую папку архива'))
        self.rb_arch_own = QRadioButton(tr('В свою папку:'))
        bg3 = QButtonGroup(self)
        bg3.addButton(self.rb_arch_default)
        bg3.addButton(self.rb_arch_own)
        self.ed_arch = QLineEdit(placeholderText=tr('Выберите папку…'))
        self.btn_arch = QPushButton(tr('Обзор…'))
        self.btn_arch.clicked.connect(self._browse_archive)
        h = QHBoxLayout()
        h.addWidget(self.rb_arch_own)
        h.addWidget(self.ed_arch, 1)
        h.addWidget(self.btn_arch)
        v.addWidget(self.rb_arch_default)
        v.addLayout(h)
        self.lbl_arch = QLabel()
        self.lbl_arch.setWordWrap(True)
        v.addWidget(self.lbl_arch)
        self.rb_arch_own.toggled.connect(self._sync_enabled)
        self.ed_arch.textChanged.connect(self._update_arch_label)
        self.ed_email.textChanged.connect(self._update_arch_label)
        lay.addWidget(g)

        self.lbl_info = QLabel()
        self.lbl_info.setWordWrap(True)
        lay.addWidget(self.lbl_info)

        h = QHBoxLayout()
        self.btn_test = QPushButton(tr('Проверить подключение'))
        self.btn_test.clicked.connect(self._test)
        h.addWidget(self.btn_test)
        h.addStretch()
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText(tr('Сохранить'))
        bb.button(QDialogButtonBox.StandardButton.Ok).setObjectName("Primary")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText(tr('Отмена'))
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        h.addWidget(bb)
        lay.addLayout(h)

    def _load(self, a: Account):
        self.ed_email.setText(a.email)
        self.ed_host.setText(a.host)
        idx = self.cb_sec.findData(a.security)
        self.cb_sec.blockSignals(True)
        self.cb_sec.setCurrentIndex(max(idx, 0))
        self.cb_sec.blockSignals(False)
        self.sp_port.setValue(a.port)
        self.chk_certs.setChecked(a.accept_all_certs)
        self.ed_login.setText(a.login)
        self.ed_pass.setText(a.password)
        self.chk_save.setChecked(a.save_password)
        today = QDate.currentDate()
        self.de_from.setDate(QDate.fromString(a.date_from, "yyyy-MM-dd") if a.date_from
                             else today.addYears(-1))
        self.de_to.setDate(QDate.fromString(a.date_to, "yyyy-MM-dd") if a.date_to else today)
        (self.rb_range if a.date_mode == "range" else self.rb_all_time).setChecked(True)
        if a.folder_raw:
            self.cb_folder.addItem(a.folder_display or a.folder_raw, a.folder_raw)
        (self.rb_one_folder if a.folder_mode == "one" else self.rb_all_folders).setChecked(True)
        self.chk_dups.setChecked(a.skip_duplicates)
        self.ed_arch.setText(a.archive_dir)
        (self.rb_arch_own if a.archive_dir else self.rb_arch_default).setChecked(True)
        self._sync_enabled()

    def _sync_enabled(self):
        r = self.rb_range.isChecked()
        self.de_from.setEnabled(r)
        self.de_to.setEnabled(r)
        o = self.rb_one_folder.isChecked()
        self.cb_folder.setEnabled(o)
        own = self.rb_arch_own.isChecked()
        self.ed_arch.setEnabled(own)
        self.btn_arch.setEnabled(own)
        self._update_arch_label()

    def _archive_root(self) -> str:
        if self.rb_arch_own.isChecked():
            return self.ed_arch.text().strip()
        return self.default_archive

    def _update_arch_label(self, *_):
        root = self._archive_root()
        email = self.ed_email.text().strip().lower()
        if not root:
            self.lbl_arch.setText(tr('Укажите папку, куда сохранять письма этого ящика.'))
            return
        sub = safe_name(email, 100) if "@" in email else "<e-mail>"
        self.lbl_arch.setText(tr('Письма будут сохраняться в: {0}').format(Path(root) / sub))

    def _browse_archive(self):
        start = self.ed_arch.text().strip() or self.default_archive or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, tr('Куда сохранять письма'), start)
        if d:
            self.ed_arch.setText(os.path.normpath(d))
            self.rb_arch_own.setChecked(True)

    def _autofill(self):
        email = self.ed_email.text().strip()
        if "@" not in email:
            return
        if not self.ed_login.text().strip():
            self.ed_login.setText(email)
        if not self.ed_host.text().strip():
            self.ed_host.setText("imap." + email.split("@", 1)[1])

    def _sec_changed(self):
        val = self.cb_sec.currentData()
        port = self.sp_port.value()
        if val == "ssl" and port == 143:
            self.sp_port.setValue(993)
        elif val != "ssl" and port == 993:
            self.sp_port.setValue(143)

    # ---------- данные ----------
    def _collect(self) -> Account:
        a = copy.deepcopy(self.account)
        a.email = self.ed_email.text().strip()
        a.host = self.ed_host.text().strip()
        a.security = self.cb_sec.currentData()
        a.port = self.sp_port.value()
        a.accept_all_certs = self.chk_certs.isChecked()
        a.login = self.ed_login.text().strip()
        a.password = self.ed_pass.text()
        a.save_password = self.chk_save.isChecked()
        a.date_mode = "range" if self.rb_range.isChecked() else "all"
        a.date_from = self.de_from.date().toString("yyyy-MM-dd")
        a.date_to = self.de_to.date().toString("yyyy-MM-dd")
        a.folder_mode = "one" if self.rb_one_folder.isChecked() else "all"
        a.folder_raw = self.cb_folder.currentData() or ""
        a.skip_duplicates = self.chk_dups.isChecked()
        a.folder_display = self.cb_folder.currentText()
        a.archive_dir = self.ed_arch.text().strip() if self.rb_arch_own.isChecked() else ""
        return a

    def _validate(self, a: Account, need_password=False) -> str | None:
        if "@" not in a.email:
            return tr('Укажите корректный e-mail.')
        if not a.host:
            return tr('Укажите адрес IMAP-сервера.')
        if need_password and not a.password:
            return tr('Укажите пароль.')
        if a.date_mode == "range" and date.fromisoformat(a.date_from) > date.fromisoformat(a.date_to):
            return tr('Дата «с» больше даты «по».')
        if self.rb_arch_own.isChecked():
            if not a.archive_dir:
                return tr('Укажите папку для писем или выберите «В общую папку архива».')
            try:
                Path(a.archive_dir).mkdir(parents=True, exist_ok=True)
                probe = Path(a.archive_dir) / ".shinra_write_test"
                probe.write_bytes(b"")
                probe.unlink()
            except OSError as e:
                return tr('Нет доступа на запись в папку «{0}»: {1}').format(a.archive_dir, e.strerror or e)
        return None

    def _busy(self, on: bool, text=""):
        for w in (self.btn_test, self.btn_folders):
            w.setEnabled(not on)
        self.lbl_info.setStyleSheet(f"color:{theme.status_color('running')}")
        self.lbl_info.setText(text)

    def _result(self, ok: bool, text: str):
        self._busy(False)
        self.lbl_info.setStyleSheet(f"color:{theme.status_color('ok' if ok else 'error')}")
        self.lbl_info.setText(text)

    def _test(self):
        a = self._collect()
        err = self._validate(a, need_password=True)
        if err:
            return self._result(False, err)

        def job():
            c = ImapConnection(a)
            c.connect()
            n = len(c.list_folders())
            c.logout()
            return n
        self._busy(True, tr('Подключение…'))
        run_bg(job, lambda n: self._result(True, tr('Подключение успешно. Папок на сервере: {n}.').format(n=n)),
               lambda e: self._result(False, e))

    def _load_folders(self):
        a = self._collect()
        err = self._validate(a, need_password=True)
        if err:
            return self._result(False, err)

        def job():
            c = ImapConnection(a)
            c.connect()
            folders = c.list_folders()
            c.logout()
            return folders

        def done(folders):
            current = self.cb_folder.currentData()
            self.cb_folder.clear()
            for fo in folders:
                if fo.selectable:
                    self.cb_folder.addItem(fo.display, fo.raw)
            i = self.cb_folder.findData(current)
            if i >= 0:
                self.cb_folder.setCurrentIndex(i)
            self.rb_one_folder.setChecked(True)
            self._result(True, tr('Загружено папок: {0}.').format(self.cb_folder.count()))
        self._busy(True, tr('Получение списка папок…'))
        run_bg(job, done, lambda e: self._result(False, e))

    def _accept(self):
        a = self._collect()
        err = self._validate(a)
        if not err and a.folder_mode == "one" and not a.folder_raw:
            err = tr('Выберите папку (нажмите «Загрузить список папок»).')
        if err:
            QMessageBox.warning(self, tr('Проверьте данные'), err)
            return
        self.account = a
        self.accept()
