"""Grid of blocks for one file, colored by state; active blocks fill up live."""

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter
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
EMPTY = 7   # padding after the last cell: transparent
COLOR_TABLE = [COLORS[i].rgba() for i in range(7)] + [0]
LEGEND = [(engine.B_UNKNOWN, "unchecked"), (engine.B_HASHING, "checksumming"),
          (engine.B_SAME, "identical"), (engine.B_PENDING, "pending"),
          (engine.B_ACTIVE, "transferring"), (engine.B_DONE, "sent"),
          (engine.B_FAILED, "failed")]


# A cell covering several blocks shows the "least finished" state among them,
# so it turns done/identical only once all of its blocks are.
GROUP_PRIORITY = [engine.B_FAILED, engine.B_ACTIVE, engine.B_HASHING, engine.B_PENDING,
                  engine.B_UNKNOWN, engine.B_DONE, engine.B_SAME]


class BlockMap(QWidget):
    """Fits the whole file into the space it's given: cells are as big as
    MAX_CELL (small files) and shrink as the block count grows. Past MIN_CELL
    each cell stands for several consecutive blocks."""
    MAX_CELL = 14
    MIN_CELL = 2

    def __init__(self):
        super().__init__()
        self.blocks = b""
        self.progress = {}
        self.bs = 1
        self.size = 0
        self.cell = 10
        self.per = 1          # blocks per cell
        self.cols = 1
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(40)

    def set_data(self, blocks, progress, bs, size):
        relayout = len(blocks) != len(self.blocks)
        self.blocks, self.progress, self.bs, self.size = blocks, progress, bs, size
        if relayout:
            self._layout()
        self.update()

    def resizeEvent(self, ev):
        self._layout()
        super().resizeEvent(ev)

    def _layout(self):
        """Biggest cell c such that a (w // c) x (h // c) grid holds every cell."""
        w, h, n = max(1, self.width()), max(1, self.height()), len(self.blocks)
        self.per = 1
        for c in range(self.MAX_CELL, self.MIN_CELL - 1, -1):
            if (w // c) * (h // c) >= n:
                break
        else:
            c = self.MIN_CELL
            fit = max(1, (w // c) * (h // c))
            self.per = -(-n // fit)
        self.cell, self.cols = c, max(1, w // c)

    def sizeHint(self):
        return QSize(400, 120)

    def minimumSizeHint(self):
        return QSize(100, 40)

    def _cell_state(self, k):
        if self.per == 1:
            return self.blocks[k]
        chunk = self.blocks[k * self.per:(k + 1) * self.per]
        for st in GROUP_PRIORITY:
            if st in chunk:
                return st
        return engine.B_UNKNOWN

    def _cells(self):
        """One state byte per cell."""
        if self.per == 1:
            return self.blocks
        n = -(-len(self.blocks) // self.per)
        return bytes(self._cell_state(k) for k in range(n))

    def paintEvent(self, ev):
        if not self.blocks:
            p = QPainter(self)
            p.setPen(QColor("#888"))
            p.drawText(self.rect(), Qt.AlignCenter, "select a file to see its blocks")
            return
        cols, c, per = self.cols, self.cell, self.per
        cells = self._cells()
        rows = -(-len(cells) // cols)
        # One pixel per cell in an indexed image, scaled up in a single draw:
        # tens of thousands of cells cost about as much as a handful.
        buf = cells + bytes([EMPTY]) * (rows * cols - len(cells))
        img = QImage(buf, cols, rows, cols, QImage.Format_Indexed8)
        img.setColorTable(COLOR_TABLE)
        p = QPainter(self)
        p.drawImage(QRect(0, 0, cols * c, rows * c), img)
        gap = 1 if c >= 5 else 0
        if gap:   # grid lines in the background color separate the cells
            p.setPen(self.palette().window().color())
            for x in range(c - 1, cols * c, c):
                p.drawLine(x, 0, x, rows * c - 1)
            for y in range(c - 1, rows * c, c):
                p.drawLine(0, y, cols * c - 1, y)
        if per == 1:   # in-flight blocks fill up from the bottom
            i = cells.find(engine.B_ACTIVE)
            while i >= 0:
                x, y = (i % cols) * c, (i // cols) * c
                p.fillRect(QRect(x, y, c - gap, c - gap), COLORS[engine.B_PENDING])
                blen = min(self.bs, self.size - i * self.bs) or 1
                frac = min(1.0, self.progress.get(i, 0) / blen)
                hh = int(round((c - gap) * frac))
                p.fillRect(QRect(x, y + (c - gap) - hh, c - gap, hh), COLORS[engine.B_ACTIVE])
                i = cells.find(engine.B_ACTIVE, i + 1)

    def mouseMoveEvent(self, ev):
        pos = ev.position().toPoint()
        k = (pos.y() // self.cell) * self.cols + pos.x() // self.cell
        first, last = k * self.per, min(len(self.blocks), (k + 1) * self.per) - 1
        if 0 <= first < len(self.blocks) and pos.x() < self.cols * self.cell:
            off = first * self.bs
            end = min(self.size, (last + 1) * self.bs)
            if self.per == 1:
                head = f"block {first + 1}/{len(self.blocks)}: {engine.B_NAMES[self.blocks[first]]}"
            else:
                chunk = self.blocks[first:last + 1]
                mix = ", ".join(f"{chunk.count(st)} {engine.B_NAMES[st]}"
                                for st in GROUP_PRIORITY if st in chunk)
                head = f"blocks {first + 1}–{last + 1}/{len(self.blocks)}: {mix}"
            extra = ""
            if self.per == 1 and self.blocks[first] == engine.B_ACTIVE:
                extra = f"\n{fmt_bytes(self.progress.get(first, 0))} of {fmt_bytes(end - off)} moved"
            QToolTip.showText(ev.globalPosition().toPoint(),
                              f"{head}\nbytes {off:,} – {end - 1:,}{extra}", self)
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
