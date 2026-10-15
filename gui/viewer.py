"""Просмотр скачанных писем: дерево ящиков/папок, список с датами, предпросмотр."""
import html
import os
import shutil
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import (QAbstractTableModel, QDate, QModelIndex, QSortFilterProxyModel, Qt,
                            QUrl)
from PySide6.QtGui import QImage, QTextDocument
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateEdit,
                               QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSplitter,
                               QTableView, QTextBrowser, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from core.mail_index import list_mailboxes, load_folder, mailbox_folders
from core.mailparse import head_text, parse_full, sent_datetime
from . import theme
from .common import run_bg

COLS = ["", "От", "Кому", "Тема", "Отправлено", "Получено", "Размер", "Папка"]
C_ATT, C_FROM, C_TO, C_SUBJ, C_SENT, C_RECV, C_SIZE, C_FOLDER = range(8)
SORT_ROLE = Qt.ItemDataRole.UserRole + 1


def human_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def fmt_dt(dt: datetime | None) -> str:
    return dt.strftime("%d.%m.%Y %H:%M") if dt else ""


class MessageModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.rows = []

    def set_rows(self, rows):
        self.beginResetModel()
        for r in rows:
            sdt = sent_datetime(r)
            r["_sent_local"] = sdt.astimezone() if sdt else None
            r["_sent_ts"] = sdt.timestamp() if sdt else None
            r["_recv_local"] = datetime.fromtimestamp(r["received"]) if r.get("received") else None
            r["_search"] = " ".join((r.get("from", ""), r.get("to", ""), r.get("cc", ""),
                                     r.get("subject", ""))).lower()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLS)

    def headerData(self, section, orient, role=Qt.ItemDataRole.DisplayRole):
        if orient == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLS[section]
        if orient == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.ToolTipRole:
            return {C_ATT: "Есть вложения", C_SENT: "Дата из письма (когда отправлено)",
                    C_RECV: "Когда письмо получил почтовый сервер"}.get(section)
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        r = self.rows[index.row()]
        c = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return {
                C_ATT: "📎" if r.get("attach") else "",
                C_FROM: r.get("from_name") or r.get("from_addr") or r.get("from", ""),
                C_TO: r.get("to", ""),
                C_SUBJ: r.get("subject") or "(без темы)",
                C_SENT: fmt_dt(r["_sent_local"]),
                C_RECV: fmt_dt(r["_recv_local"]),
                C_SIZE: human_size(r.get("size", 0)),
                C_FOLDER: r.get("folder", ""),
            }[c]
        if role == SORT_ROLE:
            return {
                C_ATT: int(bool(r.get("attach"))),
                C_SENT: r["_sent_ts"] or 0.0,
                C_RECV: float(r.get("received") or 0),
                C_SIZE: r.get("size", 0),
            }.get(c, (self.data(index) or "").lower())
        if role == Qt.ItemDataRole.ToolTipRole and c in (C_FROM, C_TO, C_SUBJ):
            return {C_FROM: r.get("from"), C_TO: r.get("to"), C_SUBJ: r.get("subject")}[c]
        if role == Qt.ItemDataRole.TextAlignmentRole and c in (C_ATT, C_SIZE):
            return int(Qt.AlignmentFlag.AlignCenter)
        return None


class MessageFilter(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.setSortRole(SORT_ROLE)
        self.text = ""
        self.dfrom = self.dto = None
        self.by = "received"

    def update(self, text, dfrom, dto, by):
        self.text, self.dfrom, self.dto, self.by = text.lower().strip(), dfrom, dto, by
        self.invalidateFilter()

    def filterAcceptsRow(self, src_row, parent):
        r = self.sourceModel().rows[src_row]
        if self.text and self.text not in r["_search"]:
            return False
        if self.dfrom or self.dto:
            dt = r["_recv_local"] if self.by == "received" else r["_sent_local"]
            if dt is None:
                return False
            d = dt.date()
            if (self.dfrom and d < self.dfrom) or (self.dto and d > self.dto):
                return False
        return True


class MailBrowser(QTextBrowser):
    """Показывает HTML письма; встроенные картинки (cid:) берутся из вложений,
    внешние картинки из интернета не загружаются (безопасность и приватность)."""

    def __init__(self):
        super().__init__()
        self.setOpenExternalLinks(True)
        self.inline = {}

    def loadResource(self, rtype, url: QUrl):
        if url.scheme() == "cid":
            data = self.inline.get(url.toString()[4:])
            if data is not None:
                img = QImage()
                img.loadFromData(data)
                return img
        return None


class ViewerWindow(QDialog):
    def __init__(self, parent, cfg, select_email=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Просмотр архива писем — Shinra Tensei")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint |
                            Qt.WindowType.WindowMinimizeButtonHint)
        self.resize(1300, 820)
        self._load_token = 0
        self._body = None
        self._current = None
        self._build()
        self._fill_tree(select_email)

    # ---------- интерфейс ----------
    def _build(self):
        lay = QVBoxLayout(self)
        split = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Ящики и папки", "Писем"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.currentItemChanged.connect(self._tree_changed)
        ll.addWidget(self.tree)
        b = QPushButton("Обновить")
        b.clicked.connect(lambda: self._fill_tree(None))
        ll.addWidget(b)
        split.addWidget(left)

        right = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        fl = QHBoxLayout()
        self.ed_search = QLineEdit(placeholderText="Поиск: отправитель, получатель, тема…")
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.textChanged.connect(self._apply_filter)
        fl.addWidget(self.ed_search, 2)
        self.chk_date = QCheckBox("Дата")
        self.chk_date.toggled.connect(self._apply_filter)
        self.cb_by = QComboBox()
        self.cb_by.addItem("получения", "received")
        self.cb_by.addItem("отправки", "sent")
        self.cb_by.currentIndexChanged.connect(self._apply_filter)
        self.de_from = QDateEdit(calendarPopup=True, displayFormat="dd.MM.yyyy")
        self.de_to = QDateEdit(calendarPopup=True, displayFormat="dd.MM.yyyy")
        self.de_from.setDate(QDate.currentDate().addMonths(-1))
        self.de_to.setDate(QDate.currentDate())
        for w in (self.de_from, self.de_to):
            w.dateChanged.connect(self._apply_filter)
        fl.addWidget(self.chk_date)
        fl.addWidget(self.cb_by)
        fl.addWidget(QLabel("с"))
        fl.addWidget(self.de_from)
        fl.addWidget(QLabel("по"))
        fl.addWidget(self.de_to)
        self.lbl_count = QLabel()
        fl.addWidget(self.lbl_count)
        tl.addLayout(fl)

        self.model = MessageModel()
        self.proxy = MessageFilter()
        self.proxy.setSourceModel(self.model)
        self.view = QTableView()
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(C_RECV, Qt.SortOrder.DescendingOrder)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setAlternatingRowColors(True)
        self.view.verticalHeader().setVisible(False)
        self.view.verticalHeader().setDefaultSectionSize(24)
        self.view.setWordWrap(False)
        hh = self.view.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(C_SUBJ, QHeaderView.ResizeMode.Stretch)
        for c, w in {C_ATT: 28, C_FROM: 190, C_TO: 170, C_SENT: 120, C_RECV: 120,
                     C_SIZE: 75, C_FOLDER: 120}.items():
            self.view.setColumnWidth(c, w)
        self.view.selectionModel().currentRowChanged.connect(self._row_changed)
        self.view.doubleClicked.connect(self._open_external)
        tl.addWidget(self.view)
        right.addWidget(top)

        prev = QWidget()
        pl = QVBoxLayout(prev)
        pl.setContentsMargins(0, 0, 0, 0)
        self.lbl_head = QLabel()
        self.lbl_head.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_head.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_head.setWordWrap(True)
        pl.addWidget(self.lbl_head)

        al = QHBoxLayout()
        self.att_list = QListWidget()
        self.att_list.setFlow(QListWidget.Flow.LeftToRight)
        self.att_list.setMaximumHeight(34)
        self.att_list.setToolTip("Двойной щелчок — сохранить вложение")
        self.att_list.itemDoubleClicked.connect(self._save_attachment)
        al.addWidget(self.att_list, 1)
        self.btn_save_all = QPushButton("Сохранить вложения…")
        self.btn_save_all.clicked.connect(self._save_all)
        al.addWidget(self.btn_save_all)
        self.btn_headers = QPushButton("Заголовки")
        self.btn_headers.clicked.connect(self._show_headers)
        al.addWidget(self.btn_headers)
        self.btn_eml = QPushButton("Сохранить .eml…")
        self.btn_eml.clicked.connect(self._save_eml)
        al.addWidget(self.btn_eml)
        self.btn_open = QPushButton("Открыть в почтовой программе")
        self.btn_open.clicked.connect(self._open_external)
        al.addWidget(self.btn_open)
        pl.addLayout(al)

        self.browser = MailBrowser()
        pl.addWidget(self.browser, 1)
        right.addWidget(prev)
        right.setSizes([420, 400])
        split.addWidget(right)
        split.setSizes([260, 1040])
        lay.addWidget(split)
        self._show_message(None)

    # ---------- дерево ----------
    def _fill_tree(self, select_email):
        cur = self.tree.currentItem()
        prev_key = cur.data(0, Qt.ItemDataRole.UserRole) if cur else None
        self.tree.clear()
        target = None
        boxes = list_mailboxes(self.cfg.archive_dir)
        for name, mdir in boxes:
            folders = mailbox_folders(mdir)
            top = QTreeWidgetItem([name, str(sum(f["count"] for f in folders))])
            top.setData(0, Qt.ItemDataRole.UserRole, ("box", str(mdir)))
            top.setData(0, Qt.ItemDataRole.UserRole + 1, folders)
            f0 = top.font(0)
            f0.setBold(True)
            top.setFont(0, f0)
            self.tree.addTopLevelItem(top)
            nodes = {}
            for f in folders:
                segs = f["display"].split(f["delimiter"]) if f["delimiter"] else [f["display"]]
                parent, path = top, ()
                for i, seg in enumerate(segs):
                    path += (seg,)
                    if path not in nodes:
                        it = QTreeWidgetItem([seg, ""])
                        parent.addChild(it)
                        nodes[path] = it
                    parent = nodes[path]
                parent.setText(1, str(f["count"]))
                parent.setData(0, Qt.ItemDataRole.UserRole, ("folder", str(f["dir"])))
                parent.setData(0, Qt.ItemDataRole.UserRole + 1, [f])
            top.setExpanded(True)
            if select_email and name == select_email.strip().lower():
                target = top
            key = top.data(0, Qt.ItemDataRole.UserRole)
            if prev_key == key:
                target = target or top
        if not boxes:
            self.lbl_count.setText("Архив пуст — сначала выполните загрузку почты")
            return
        self.tree.setCurrentItem(target or self.tree.topLevelItem(0))

    def _tree_changed(self, item, _prev=None):
        if item is None:
            return
        folders = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if not folders:            # промежуточный узел без своей папки — собрать подпапки
            folders = []
            stack = [item]
            while stack:
                it = stack.pop()
                fs = it.data(0, Qt.ItemDataRole.UserRole + 1)
                if fs:
                    folders += fs
                stack += [it.child(i) for i in range(it.childCount())]
        self._load_token += 1
        token = self._load_token
        self.model.set_rows([])
        self._show_message(None)
        self.lbl_count.setText("Загрузка списка писем…")
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)

        def job():
            rows = []
            for f in folders:
                if token != self._load_token:
                    return None
                rows += load_folder(Path(f["dir"]), f["display"],
                                    stop=lambda: token != self._load_token)
            return rows

        def done(rows):
            QApplication.restoreOverrideCursor()
            if rows is None or token != self._load_token:
                return
            self.model.set_rows(rows)
            self._update_count()

        def fail(err):
            QApplication.restoreOverrideCursor()
            self.lbl_count.setText(f"Ошибка: {err}")
        run_bg(job, done, fail)

    # ---------- фильтр ----------
    def _apply_filter(self, *_):
        on = self.chk_date.isChecked()
        for w in (self.cb_by, self.de_from, self.de_to):
            w.setEnabled(on)
        dfrom = self.de_from.date().toPython() if on else None
        dto = self.de_to.date().toPython() if on else None
        self.proxy.update(self.ed_search.text(), dfrom, dto, self.cb_by.currentData())
        self._update_count()

    def _update_count(self):
        total, shown = self.model.rowCount(), self.proxy.rowCount()
        self.lbl_count.setText(f"Писем: {shown}" if shown == total else f"Писем: {shown} из {total}")

    # ---------- предпросмотр ----------
    def _row_changed(self, cur, _prev=None):
        if not cur.isValid():
            return self._show_message(None)
        src = self.proxy.mapToSource(cur)
        self._show_message(self.model.rows[src.row()])

    def _show_message(self, row):
        self._current = row
        self._body = None
        self.att_list.clear()
        self.browser.inline = {}
        has = row is not None
        for w in (self.btn_eml, self.btn_open, self.btn_headers):
            w.setEnabled(has)
        self.btn_save_all.setEnabled(False)
        if not has:
            self.lbl_head.setText("")
            self.browser.setStyleSheet("")
            self.browser.setPlainText("")
            return
        muted = theme.color("text_muted")

        def line(label, value):
            return (f"<tr><td style='color:{muted};padding-right:10px'>{label}</td>"
                    f"<td>{html.escape(value)}</td></tr>") if value else ""
        subj = html.escape(row.get("subject") or "(без темы)")
        self.lbl_head.setText(
            f"<div style='font-size:13pt;font-weight:600'>{subj}</div><table>"
            + line("От:", row.get("from", ""))
            + line("Кому:", row.get("to", ""))
            + line("Копия:", row.get("cc", ""))
            + line("Отправлено:", fmt_dt(row.get("_sent_local")))
            + line("Получено:", fmt_dt(row.get("_recv_local")))
            + line("Папка:", f"{row.get('folder', '')}   •   {human_size(row.get('size', 0))}")
            + "</table>")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            body = parse_full(Path(row["path"]))
        except Exception as e:
            QApplication.restoreOverrideCursor()
            self.browser.setPlainText(f"Не удалось разобрать письмо: {e}")
            return
        QApplication.restoreOverrideCursor()
        self._body = body
        for a in body.attachments:
            if a.cid:
                self.browser.inline[a.cid] = a.data
            if a.inline:
                continue
            it = QListWidgetItem(f"📎 {a.name} ({human_size(len(a.data))})")
            it.setData(Qt.ItemDataRole.UserRole, a)
            self.att_list.addItem(it)
        self.att_list.setVisible(self.att_list.count() > 0)
        self.btn_save_all.setEnabled(self.att_list.count() > 0)
        doc = self.browser.document()
        doc.clear()
        if body.html:
            # HTML-письма рассчитаны на белый фон — показываем их «на бумаге» в любой теме
            self.browser.setStyleSheet("QTextBrowser { background: #ffffff; color: #1d1d1d; }")
            doc.setDefaultStyleSheet("body { color: #1d1d1d; }")
            self.browser.setHtml(body.html)
        else:
            self.browser.setStyleSheet("")
            doc.setDefaultStyleSheet("")
            self.browser.setPlainText(body.text or "")
        self.browser.verticalScrollBar().setValue(0)

    # ---------- действия ----------
    def _save_attachment(self, item):
        a = item.data(Qt.ItemDataRole.UserRole)
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить вложение",
                                              str(Path.home() / "Downloads" / a.name))
        if path:
            Path(path).write_bytes(a.data)

    def _save_all(self):
        if not self._body:
            return
        d = QFileDialog.getExistingDirectory(self, "Папка для вложений",
                                             str(Path.home() / "Downloads"))
        if not d:
            return
        n = 0
        for i in range(self.att_list.count()):
            a = self.att_list.item(i).data(Qt.ItemDataRole.UserRole)
            target = Path(d) / a.name
            k = 1
            while target.exists():
                target = Path(d) / f"{Path(a.name).stem} ({k}){Path(a.name).suffix}"
                k += 1
            target.write_bytes(a.data)
            n += 1
        QMessageBox.information(self, "Вложения", f"Сохранено файлов: {n}")

    def _save_eml(self):
        if not self._current:
            return
        name = "".join(c if c not in '<>:"/\\|?*' else "_"
                       for c in (self._current.get("subject") or "письмо"))[:80] + ".eml"
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить письмо",
                                              str(Path.home() / "Downloads" / name),
                                              "Письмо (*.eml)")
        if path:
            shutil.copyfile(self._current["path"], path)

    def _open_external(self, *_):
        if self._current and hasattr(os, "startfile"):
            try:
                os.startfile(self._current["path"])
            except OSError as e:
                QMessageBox.warning(self, "Открыть письмо",
                                    f"Нет программы для открытия .eml: {e}")

    def _show_headers(self):
        if not self._current:
            return
        d = QDialog(self)
        d.setWindowTitle("Заголовки письма")
        d.resize(800, 500)
        lay = QVBoxLayout(d)
        t = QTextBrowser()
        t.setPlainText(head_text(Path(self._current["path"])))
        lay.addWidget(t)
        d.exec()
