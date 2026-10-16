"""Значки, нарисованные программой (не зависят от шрифтов и эмодзи Windows)."""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

_cache = {}


def _paperclip_path() -> QPainterPath:
    """Скрепка в координатах 16×16: внешняя петля, внутренняя петля, хвостик."""
    p = QPainterPath()
    p.moveTo(11.0, 5.0)
    p.lineTo(11.0, 11.0)
    p.arcTo(QRectF(5.0, 8.0, 6.0, 6.0), 0, -180)       # нижняя дуга, r = 3
    p.lineTo(5.0, 4.0)
    p.arcTo(QRectF(5.0, 2.0, 4.0, 4.0), 180, -180)     # верхняя дуга, r = 2
    p.lineTo(9.0, 10.5)
    p.arcTo(QRectF(7.0, 9.5, 2.0, 2.0), 0, -180)       # маленькая дуга, r = 1
    p.lineTo(7.0, 6.0)
    return p


def paperclip(color: str, size: int = 16) -> QIcon:
    key = ("clip", color, size)
    if key not in _cache:
        scale = 3                                       # чёткость на экранах 125–300 %
        pm = QPixmap(size * scale, size * scale)
        pm.fill(Qt.GlobalColor.transparent)
        qp = QPainter(pm)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        qp.scale(size * scale / 16.0, size * scale / 16.0)
        qp.translate(8, 8)
        qp.rotate(35)                                   # наклон, как у привычной скрепки
        qp.translate(-8, -8)
        pen = QPen(QColor(color), 1.45)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        qp.setPen(pen)
        qp.setBrush(Qt.BrushStyle.NoBrush)
        qp.drawPath(_paperclip_path())
        qp.end()
        pm.setDevicePixelRatio(scale)
        _cache[key] = QIcon(pm)
    return _cache[key]
