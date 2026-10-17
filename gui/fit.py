"""Колонки таблиц по ширине окна и подписи, которые обрезаются многоточием."""
from PySide6.QtCore import QEvent, QObject, QSize, Qt
from PySide6.QtWidgets import QFrame, QHeaderView, QLabel, QScrollArea, QSizePolicy

PAD = 6             # к ширине заголовка: чтобы текст не прилипал к краям


class ColumnFitter(QObject):
    """Раскладывает колонки по ширине таблицы при каждом её изменении.

    cols — [(ключ, ширина, вес, мин. ширина)] по порядку колонок. Свободное место делят
    колонки с весом > 0 пропорционально весу; при нехватке места они же сужаются, но не
    уже минимальной ширины и не уже заголовка (дальше — горизонтальная прокрутка).
    Ширина, которую пользователь задал мышью, запоминается в saved[ключ] и становится
    основной для этой колонки. Колонки с шириной 0 — фиксированные, их не трогаем."""

    def __init__(self, view, cols, saved=None, on_change=None):
        super().__init__(view)
        self.view = view
        self.cols = cols
        self.saved = saved if saved is not None else {}
        self.on_change = on_change
        self._fitting = False
        hh = view.horizontalHeader()
        hh.setStretchLastSection(False)
        hh.setMinimumSectionSize(24)
        for c, (_k, w, _wt, _mn) in enumerate(cols):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive if w
                                    else QHeaderView.ResizeMode.Fixed)
        hh.sectionResized.connect(self._user_resized)
        view.viewport().installEventFilter(self)
        self.fit()

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Resize:
            self.fit()
        return False

    def min_width(self, c) -> int:
        hh = self.view.horizontalHeader()
        head = hh.sectionSizeFromContents(c).width() + PAD
        return max(self.cols[c][3], head)

    def base_widths(self) -> list:
        out = []
        for c, (key, w, _wt, _mn) in enumerate(self.cols):
            if not w:
                out.append(self.view.columnWidth(c))
                continue
            b = self.saved.get(key, w)
            out.append(max(int(b) if isinstance(b, (int, float)) else w, self.min_width(c)))
        return out

    def widths_for(self, avail: int) -> list:
        """Ширины колонок для доступной ширины avail (без побочных эффектов)."""
        ws = self.base_widths()
        flex = [c for c, col in enumerate(self.cols) if col[1] and col[2] > 0]
        diff = avail - sum(ws)
        if diff > 0 and flex:
            total_w = sum(self.cols[c][2] for c in flex)
            given = 0
            for c in flex:
                add = diff * self.cols[c][2] // total_w
                ws[c] += add
                given += add
            ws[flex[-1]] += diff - given          # остаток от деления — последней
        elif diff < 0:
            # сужаем гибкие колонки пропорционально запасу до их минимума
            room = {c: ws[c] - self.min_width(c) for c in flex if ws[c] > self.min_width(c)}
            total_room = sum(room.values())
            need = min(-diff, total_room)
            if need > 0:
                taken = 0
                for c, r in room.items():
                    cut = need * r // total_room
                    ws[c] -= cut
                    taken += cut
                for c, r in room.items():          # остаток — где ещё есть запас
                    if taken >= need:
                        break
                    extra = min(need - taken, ws[c] - self.min_width(c))
                    ws[c] -= extra
                    taken += extra
        return ws

    def fit(self):
        if self._fitting:
            return
        self._fitting = True
        try:
            for c, w in enumerate(self.widths_for(self.view.viewport().width())):
                if self.cols[c][1]:
                    self.view.setColumnWidth(c, w)
        finally:
            self._fitting = False

    def _user_resized(self, c, _old, new):
        if self._fitting or not self.cols[c][1]:
            return
        self.saved[self.cols[c][0]] = int(new)
        if self.on_change is not None:
            self.on_change(self.saved)

    def reset(self):
        """Вернуть ширины по умолчанию."""
        self.saved.clear()
        if self.on_change is not None:
            self.on_change(self.saved)
        self.fit()


class ElidedLabel(QLabel):
    """Однострочная подпись: не держит ширину окна, а обрезается «…»; целиком — в подсказке."""

    def __init__(self, text="", parent=None, **kw):
        super().__init__(parent, **kw)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text):
        self._full = text or ""
        self.setToolTip(self._full)
        self._elide()

    def text_full(self) -> str:
        return self._full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._elide()

    def _elide(self):
        w = self.contentsRect().width()
        shown = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, w) \
            if w > 0 else self._full
        super().setText(shown)



class FitScrollArea(QScrollArea):
    """Прокрутка, которая просит столько места, сколько нужно содержимому: на большом
    экране полосы прокрутки нет, на маленьком окно можно сделать ниже содержимого."""

    def __init__(self, content, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWidget(content)

    def sizeHint(self):
        w = self.widget()
        hint = w.sizeHint()
        bar = self.verticalScrollBar().sizeHint().width()
        return QSize(hint.width() + bar, w.heightForWidth(hint.width()) if w.hasHeightForWidth()
                     else hint.height())

    def minimumSizeHint(self):
        w = self.widget()
        return QSize(w.minimumSizeHint().width() + self.verticalScrollBar().sizeHint().width(), 80)
