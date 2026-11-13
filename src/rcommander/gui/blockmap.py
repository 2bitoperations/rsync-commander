"""Grid of blocks for one file, colored by state; active blocks fill up live."""

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QToolTip, QWidget

from .. import engine
from ..fmt import fmt_bytes

COLORS = {
    engine.B_UNKNOWN: QColor("#3a3f47"),   # not yet compared
    engine.B_PENDING: QColor("#6b7280"),   # needs sending
    engine.B_HASHING: QColor("#a78bfa"),   # being checksummed
    engine.B_SAME:    QColor("#2f6f4f"),   # verified identical, skipped
    engine.B_ACTIVE:  QColor("#f59e0b"),   # in flight
    engine.B_DONE:    QColor("#22c55e"),   # sent + verified
    engine.B_FAILED:  QColor("#ef4444"),
}
LEGEND = [(engine.B_UNKNOWN, "unchecked"), (engine.B_HASHING, "checksumming"),
          (engine.B_SAME, "identical"), (engine.B_PENDING, "pending"),
          (engine.B_ACTIVE, "transferring"), (engine.B_DONE, "sent"),
          (engine.B_FAILED, "failed")]


class BlockMap(QWidget):
    def __init__(self):
        super().__init__()
        self.blocks = b""
        self.progress = {}
        self.bs = 1
        self.size = 0
        self.cell = 10
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumHeight(40)

    def set_data(self, blocks, progress, bs, size):
        self.blocks, self.progress, self.bs, self.size = blocks, progress, bs, size
        n = len(blocks)
        # pick a cell size that keeps big files on a reasonable number of rows
        w = max(100, self.width())
        for c in (14, 10, 7, 5, 4, 3):
            if (n * c * c) / max(1, w) < 220 or c == 3:
                self.cell = c
                break
        self.updateGeometry()
        self.update()

    def _cols(self):
        return max(1, self.width() // self.cell)

    def sizeHint(self):
        rows = (len(self.blocks) + self._cols() - 1) // self._cols()
        return QSize(400, max(40, rows * self.cell + 2))

    def minimumSizeHint(self):
        return QSize(100, 40)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        cols = max(1, w // self.cell)
        return max(40, ((len(self.blocks) + cols - 1) // cols) * self.cell + 2)

    def paintEvent(self, ev):
        p = QPainter(self)
        cols, c = self._cols(), self.cell
        gap = 1 if c >= 5 else 0
        for i, st in enumerate(self.blocks):
            x, y = (i % cols) * c, (i // cols) * c
            r = QRect(x, y, c - gap, c - gap)
            if st == engine.B_ACTIVE:
                p.fillRect(r, COLORS[engine.B_PENDING])
                blen = min(self.bs, self.size - i * self.bs) or 1
                frac = min(1.0, self.progress.get(i, 0) / blen)
                h = int(round((c - gap) * frac))
                p.fillRect(QRect(x, y + (c - gap) - h, c - gap, h), COLORS[engine.B_ACTIVE])
            else:
                p.fillRect(r, COLORS.get(st, COLORS[0]))
        if not self.blocks:
            p.setPen(QColor("#888"))
            p.drawText(self.rect(), Qt.AlignCenter, "select a file to see its blocks")

    def mouseMoveEvent(self, ev):
        pos = ev.position().toPoint()
        i = (pos.y() // self.cell) * self._cols() + pos.x() // self.cell
        if 0 <= i < len(self.blocks) and pos.x() < self._cols() * self.cell:
            off = i * self.bs
            end = min(self.size, off + self.bs)
            st = engine.B_NAMES[self.blocks[i]]
            extra = ""
            if self.blocks[i] == engine.B_ACTIVE:
                extra = f"\n{fmt_bytes(self.progress.get(i, 0))} of {fmt_bytes(end - off)} moved"
            QToolTip.showText(ev.globalPosition().toPoint(),
                              f"block {i + 1}/{len(self.blocks)}: {st}\n"
                              f"bytes {off:,} – {end - 1:,}{extra}", self)
        else:
            QToolTip.hideText()


def legend_widget():
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    for st, name in LEGEND:
        sw = QLabel()
        sw.setFixedSize(10, 10)
        sw.setStyleSheet(f"background: {COLORS[st].name()};")
        lay.addWidget(sw)
        lay.addWidget(QLabel(name))
        lay.addSpacing(8)
    lay.addStretch(1)
    return w
