"""Окно настройки почтового ящика."""
import copy
from datetime import date

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDateEdit, QDialog,
                               QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton,
                               QSpinBox, QVBoxLayout)

from core.imap_client import ImapConnection
from core.models import SECURITY_OPTIONS, Account
from . import theme
from .common import run_bg


class AccountDialog(QDialog):
    def __init__(self, parent=None, account: Account | None = None):
        super().__init__(parent)
        self.account = copy.deepcopy(account) if account else Account()
        self.setWindowTitle("Изменить почтовый ящик" if account else "Добавить почтовый ящик")
        self.setMinimumWidth(560)
        self._build()
        self._load(self.account)

    # ---------- интерфейс ----------
    def _build(self):
        lay = QVBoxLayout(self)

        g = QGroupBox("Подключение к серверу")
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
        hp.addWidget(QLabel("Порт:"))
        hp.addWidget(self.sp_port)
        self.chk_certs = QCheckBox("Принимать все сертификаты (просроченные, самоподписанные, "
                                   "с чужим именем)")
        self.ed_login = QLineEdit(placeholderText="обычно совпадает с e-mail")
        self.ed_pass = QLineEdit(echoMode=QLineEdit.EchoMode.Password)
        self.chk_show = QCheckBox("показать")
        self.chk_show.toggled.connect(lambda on: self.ed_pass.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        hpw = QHBoxLayout()
        hpw.addWidget(self.ed_pass, 1)
        hpw.addWidget(self.chk_show)
        self.chk_save = QCheckBox("Сохранить пароль (шифруется средствами Windows)")
        f.addRow("E-mail:", self.ed_email)
        f.addRow("Сервер IMAP:", self.ed_host)
        f.addRow("Протокол:", hp)
        f.addRow("", self.chk_certs)
        f.addRow("Логин:", self.ed_login)
        f.addRow("Пароль:", hpw)
        f.addRow("", self.chk_save)
        lay.addWidget(g)

        g = QGroupBox("Период")
        v = QVBoxLayout(g)
        self.rb_all_time = QRadioButton("За всё время")
        self.rb_range = QRadioButton("За период:")
        bg = QButtonGroup(self)
        bg.addButton(self.rb_all_time)
        bg.addButton(self.rb_range)
        self.de_from = QDateEdit(calendarPopup=True, displayFormat="dd.MM.yyyy")
        self.de_to = QDateEdit(calendarPopup=True, displayFormat="dd.MM.yyyy")
        h = QHBoxLayout()
        h.addWidget(self.rb_range)
        h.addWidget(QLabel("с"))
        h.addWidget(self.de_from)
        h.addWidget(QLabel("по"))
        h.addWidget(self.de_to)
        h.addStretch()
        v.addWidget(self.rb_all_time)
        v.addLayout(h)
        self.rb_range.toggled.connect(self._sync_enabled)
        lay.addWidget(g)

        g = QGroupBox("Папки")
        v = QVBoxLayout(g)
        self.rb_all_folders = QRadioButton("Все папки")
        self.rb_one_folder = QRadioButton("Только папка:")
        bg2 = QButtonGroup(self)
        bg2.addButton(self.rb_all_folders)
        bg2.addButton(self.rb_one_folder)
        self.cb_folder = QComboBox()
        self.cb_folder.setMinimumWidth(240)
        self.btn_folders = QPushButton("Загрузить список папок")
        self.btn_folders.clicked.connect(self._load_folders)
        h = QHBoxLayout()
        h.addWidget(self.rb_one_folder)
        h.addWidget(self.cb_folder, 1)
        h.addWidget(self.btn_folders)
        v.addWidget(self.rb_all_folders)
        v.addLayout(h)
        self.rb_one_folder.toggled.connect(self._sync_enabled)
        lay.addWidget(g)

        self.lbl_info = QLabel()
        self.lbl_info.setWordWrap(True)
        lay.addWidget(self.lbl_info)

        h = QHBoxLayout()
        self.btn_test = QPushButton("Проверить подключение")
        self.btn_test.clicked.connect(self._test)
        h.addWidget(self.btn_test)
        h.addStretch()
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Сохранить")
        bb.button(QDialogButtonBox.StandardButton.Ok).setObjectName("Primary")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
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
        self._sync_enabled()

    def _sync_enabled(self):
        r = self.rb_range.isChecked()
        self.de_from.setEnabled(r)
        self.de_to.setEnabled(r)
        o = self.rb_one_folder.isChecked()
        self.cb_folder.setEnabled(o)

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
        a.folder_display = self.cb_folder.currentText()
        return a

    def _validate(self, a: Account, need_password=False) -> str | None:
        if "@" not in a.email:
            return "Укажите корректный e-mail."
        if not a.host:
            return "Укажите адрес IMAP-сервера."
        if need_password and not a.password:
            return "Укажите пароль."
        if a.date_mode == "range" and date.fromisoformat(a.date_from) > date.fromisoformat(a.date_to):
            return "Дата «с» больше даты «по»."
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
        self._busy(True, "Подключение…")
        run_bg(job, lambda n: self._result(True, f"Подключение успешно. Папок на сервере: {n}."),
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
            self._result(True, f"Загружено папок: {self.cb_folder.count()}.")
        self._busy(True, "Получение списка папок…")
        run_bg(job, done, lambda e: self._result(False, e))

    def _accept(self):
        a = self._collect()
        err = self._validate(a)
        if not err and a.folder_mode == "one" and not a.folder_raw:
            err = "Выберите папку (нажмите «Загрузить список папок»)."
        if err:
            QMessageBox.warning(self, "Проверьте данные", err)
            return
        self.account = a
        self.accept()
