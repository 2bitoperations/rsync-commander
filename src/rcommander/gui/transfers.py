"""Transfer queue and the detailed live view of a job."""

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QObject, QPointF, QRectF, Qt,
                            QTimer, Signal)
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QCheckBox, QGridLayout, QHBoxLayout,
                               QHeaderView, QLabel, QProgressBar, QPushButton,
                               QScrollArea, QSplitter, QStyle, QStyledItemDelegate,
                               QTableView, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget, QAbstractItemView)

from .. import engine
from ..fmt import fmt_bytes, fmt_dur
from .blockmap import COLORS, BlockMap, legend_widget


class JobManager(QObject):
    """Runs queued jobs one at a time (they would only fight over the link)."""
    job_added = Signal(object)
    job_finished = Signal(object)

    def __init__(self):
        super().__init__()
        self.jobs = []
        self.running = None
        self.job_finished.connect(self._on_finished, Qt.QueuedConnection)

    def add(self, job):
        job.on_finished = lambda j: self.job_finished.emit(j)
        self.jobs.append(job)
        self.job_added.emit(job)
        self._kick()

    def _kick(self):
        if self.running is not None:
            return
        for j in self.jobs:
            if j.t_start is None and not j.cancelled:
                self.running = j
                j.start()
                return

    def _on_finished(self, job):
        if job is self.running:
            self.running = None
        self._kick()

    def cancel(self, job):
        if job.t_start is None:
            job.cancelled = True
            job.state = engine.J_CANCELLED
        else:
            job.cancel()

    def clear_finished(self):
        self.jobs = [j for j in self.jobs if j.state not in
                     (engine.J_DONE, engine.J_FAILED, engine.J_CANCELLED)]


SEG_SAME = COLORS[engine.B_SAME]       # identical: verified, not sent
SEG_DONE = COLORS[engine.B_DONE]       # sent + verified
SEG_ACTIVE = COLORS[engine.B_ACTIVE]   # in flight
TRACK = QColor("#3a3f47")


class ProgressDelegate(QStyledItemDelegate):
    """Draws a stacked bar ourselves. The native style's CE_ProgressBar ignores
    the cell rect on macOS and paints a stray line at the top of the view.

    Item data (Qt.UserRole): {"segs": [(fraction, QColor), ...], "text": str}.
    The segments use the block-map colours, so a file's bar summarises its block map.
    """

    def paint(self, painter, option, index):
        v = index.data(Qt.UserRole)
        if not v:
            return super().paint(painter, option, index)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        r = QRectF(option.rect).adjusted(4, 4, -4, -4)
        path = QPainterPath()
        path.addRoundedRect(r, 4, 4)
        painter.setClipPath(path)
        painter.fillRect(r, TRACK)
        x = r.left()
        for frac, color in v["segs"]:
            w = max(0.0, min(1.0, frac)) * r.width()
            if w > 0:
                painter.fillRect(QRectF(x, r.top(), w, r.height()), color)
                x += w
        painter.setClipping(False)
        text = v.get("text")
        if text:
            f = painter.font()
            f.setPointSizeF(max(8.0, f.pointSizeF() * 0.9))
            painter.setFont(f)
            painter.setPen(QColor(0, 0, 0, 160))
            painter.drawText(r.translated(1, 1), Qt.AlignCenter, text)
            painter.setPen(QColor("white"))
            painter.drawText(r, Qt.AlignCenter, text)
        painter.restore()


class SparkDelegate(QStyledItemDelegate):
    """Per-stream throughput sparkline. Item data (Qt.UserRole):
    {"hist": [bytes/s, ...] oldest first, "rate": bytes/s, "vmax": shared scale}.
    The newest sample is at the right edge, and all streams share one scale so
    they can be compared directly."""

    WINDOW = 60       # samples shown (x WORKER_SAMPLE = 30 s)
    MIN_POINTS = 20   # don't stretch fewer samples than this across the width
    LINE = QColor("#a78bfa")
    FILL_TOP = QColor(167, 139, 250, 150)
    FILL_BOTTOM = QColor(167, 139, 250, 25)

    def paint(self, painter, option, index):
        v = index.data(Qt.UserRole)
        if not v:
            return super().paint(painter, option, index)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        cell = QRectF(option.rect).adjusted(4, 3, -4, -3)
        text = f"{fmt_bytes(v['rate'])}/s"
        f = painter.font()
        f.setPointSizeF(max(8.0, f.pointSizeF() * 0.9))
        painter.setFont(f)
        tw = painter.fontMetrics().horizontalAdvance("999.9 MiB/s") + 8
        r = cell.adjusted(0, 0, -tw, 0)                 # graph area; label to its right
        painter.fillRect(r, QColor(255, 255, 255, 12))
        hist, vmax = v["hist"][-self.WINDOW:], v["vmax"] or 1.0
        if len(hist) >= 2:
            # fill the width once there are MIN_POINTS samples, then scroll
            step = r.width() / (max(len(hist), self.MIN_POINTS) - 1)
            x0 = r.right() - step * (len(hist) - 1)
            pts = [QPointF(x0 + i * step, r.bottom() - min(1.0, h / vmax) * r.height())
                   for i, h in enumerate(hist)]
            area = QPainterPath(QPointF(pts[0].x(), r.bottom()))
            for p in pts:
                area.lineTo(p)
            area.lineTo(QPointF(pts[-1].x(), r.bottom()))
            area.closeSubpath()
            g = QLinearGradient(0, r.top(), 0, r.bottom())
            g.setColorAt(0, self.FILL_TOP)
            g.setColorAt(1, self.FILL_BOTTOM)
            painter.fillPath(area, g)
            line = QPainterPath(pts[0])
            for p in pts[1:]:
                line.lineTo(p)
            painter.setPen(QPen(self.LINE, 1.5))
            painter.drawPath(line)
        painter.setPen(QColor("white"))
        painter.drawText(cell, Qt.AlignRight | Qt.AlignVCenter, text)
        painter.restore()


class FilesModel(QAbstractTableModel):
    COLS = ("File", "Size", "State", "Progress", "Identical", "Sent", "Blocks")
    STATE_COLORS = {engine.F_DONE: "#22c55e", engine.F_SKIPPED: "#2f9f6f",
                    engine.F_FAILED: "#ef4444", engine.F_WORKING: "#f59e0b",
                    engine.F_PREPARING: "#a78bfa", engine.F_FINALIZING: "#a78bfa"}

    def __init__(self):
        super().__init__()
        self.job = None
        self.n = 0

    def set_job(self, job):
        self.beginResetModel()
        self.job = job
        self.n = len(job.files) if job else 0
        self.endResetModel()

    def tick(self):
        if not self.job:
            return
        n = len(self.job.files)
        if n != self.n:
            self.beginResetModel()
            self.n = n
            self.endResetModel()
        elif n:
            self.dataChanged.emit(self.index(0, 2), self.index(n - 1, len(self.COLS) - 1))

    def file_at(self, row):
        return self.job.files[row] if self.job and 0 <= row < self.n else None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self.n

    def columnCount(self, parent=QModelIndex()):
        return len(self.COLS)

    def headerData(self, s, o, role=Qt.DisplayRole):
        if o == Qt.Horizontal and role == Qt.DisplayRole:
            return self.COLS[s]
        if o == Qt.Horizontal and role == Qt.ToolTipRole and s == 3:
            return ("How much of the file is in place at the destination:\n"
                    "dark green = identical (checksummed, not sent)\n"
                    "green = sent and verified\norange = in flight")
        return None

    def data(self, index, role=Qt.DisplayRole):
        f = self.file_at(index.row())
        if f is None:
            return None
        c = index.column()
        if role == Qt.DisplayRole:
            if c == 0:
                return f.rel
            if c == 1:
                return fmt_bytes(f.size)
            if c == 2:
                return engine.F_NAMES[f.state]
            if c == 4:
                return fmt_bytes(f.bytes_same)
            if c == 5:
                return fmt_bytes(f.bytes_done)
            if c == 6:
                b = f.blocks
                return f"{b.count(engine.B_SAME) + b.count(engine.B_DONE)}/{f.nblocks}"
        elif role == Qt.UserRole and c == 3:
            size = f.size or 1
            moving = sum(f.block_progress.values())
            pct = 100.0 if f.state in (engine.F_DONE, engine.F_SKIPPED) else min(
                100.0, (f.bytes_same + f.bytes_done + moving) / size * 100)
            return {"segs": [(f.bytes_same / size, SEG_SAME), (f.bytes_done / size, SEG_DONE),
                             (moving / size, SEG_ACTIVE)],
                    "text": f"{pct:.0f}%"}
        elif role == Qt.ToolTipRole and c == 3:
            moving = sum(f.block_progress.values())
            return (f"identical (skipped): {fmt_bytes(f.bytes_same)}\n"
                    f"sent: {fmt_bytes(f.bytes_done)}\n"
                    f"in flight: {fmt_bytes(moving)}\n"
                    f"of {fmt_bytes(f.size)}")
        elif role == Qt.ForegroundRole and c == 2 and f.state in self.STATE_COLORS:
            return QColor(self.STATE_COLORS[f.state])
        elif role == Qt.ToolTipRole and f.error:
            return f.error
        elif role == Qt.TextAlignmentRole and c in (1, 4, 5, 6):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None


class TransfersPane(QWidget):
    job_completed = Signal(object)

    def __init__(self, manager):
        super().__init__()
        self.mgr = manager
        self.items = {}
        self.cur_job = None
        self.cur_file = None

        # left: queue
        self.queue = QTreeWidget()
        self.queue.setHeaderLabels(["Job", "State", "Progress", "Speed", "ETA"])
        self.queue.setRootIsDecorated(False)
        self.queue.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.queue.currentItemChanged.connect(self._select_job)
        self.btn_pause = QPushButton("Pause")
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self._cancel)
        btn_clear = QPushButton("Clear finished")
        btn_clear.clicked.connect(self._clear)
        bl = QHBoxLayout()
        for b in (self.btn_pause, self.btn_cancel, btn_clear):
            bl.addWidget(b)
        bl.addStretch(1)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.queue, 1)
        ll.addLayout(bl)

        # right: details
        self.title = QLabel("No transfer selected")
        self.title.setStyleSheet("font-weight: bold;")
        self.title.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setFormat("%p%")
        self.stats = {}
        grid = QGridLayout()
        keys = [("files", "Files"), ("data", "To send"), ("same", "Identical"),
                ("hash", "Checksummed"), ("speed", "Speed"), ("wire", "On the wire"),
                ("elapsed", "Elapsed"), ("eta", "Remaining"),
                ("codec", "Compression"), ("notes", "Notes")]
        for i, (k, label) in enumerate(keys):
            r, c = divmod(i, 2)
            lab = QLabel(label + ":")
            lab.setStyleSheet("color: #888;")
            val = QLabel("–")
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            grid.addWidget(lab, r, c * 2)
            grid.addWidget(val, r, c * 2 + 1)
            self.stats[k] = val
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        self.workers = QTreeWidget()
        self.workers.setHeaderLabels(["Stream", "Activity", "Current block", "Throughput"])
        self.workers.headerItem().setToolTip(
            2, "How much of the block this stream is moving has crossed the network")
        self.workers.headerItem().setToolTip(
            3, "This connection's throughput over the last 30 s (all streams share one scale)")
        self.workers.setItemDelegateForColumn(3, SparkDelegate())
        self.workers.setRootIsDecorated(False)
        self.workers.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.workers.header().setStretchLastSection(False)
        self.workers.header().setSectionResizeMode(2, QHeaderView.Fixed)
        self.workers.setColumnWidth(2, 260)
        self.workers.header().setSectionResizeMode(3, QHeaderView.Fixed)
        self.workers.setColumnWidth(3, 300)
        self.workers.setItemDelegateForColumn(2, ProgressDelegate())

        self.files_model = FilesModel()
        self.files = QTableView()
        self.files.setModel(self.files_model)
        self.files.setItemDelegateForColumn(3, ProgressDelegate())
        self.files.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.files.setSelectionMode(QAbstractItemView.SingleSelection)
        self.files.verticalHeader().setVisible(False)
        self.files.verticalHeader().setDefaultSectionSize(20)
        self.files.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.files.horizontalHeader().setSectionResizeMode(3, QHeaderView.Fixed)
        self.files.setColumnWidth(3, 150)
        self.files.clicked.connect(self._pick_file)

        self.follow = QCheckBox("follow active file")
        self.follow.setChecked(True)
        self.map_label = QLabel()
        self.blockmap = BlockMap()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.blockmap)
        scroll.setMinimumHeight(60)
        self.errors = QLabel()
        self.errors.setStyleSheet("color: #ef4444;")
        self.errors.setWordWrap(True)
        self.errors.setTextInteractionFlags(Qt.TextSelectableByMouse)

        maphead = QHBoxLayout()
        maphead.addWidget(self.map_label, 1)
        maphead.addWidget(self.follow)
        mapbox = QWidget()
        ml = QVBoxLayout(mapbox)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.addLayout(maphead)
        ml.addWidget(scroll, 1)
        ml.addWidget(legend_widget())

        lower = QSplitter(Qt.Vertical)
        lower.addWidget(self.workers)
        lower.addWidget(self.files)
        lower.addWidget(mapbox)
        lower.setSizes([130, 160, 120])
        self.workers.setMinimumHeight(110)   # header + 4 streams without scrolling

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.title)
        rl.addWidget(self.bar)
        rl.addLayout(grid)
        rl.addWidget(self.errors)
        rl.addWidget(lower, 1)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([320, 700])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(split)

        self.mgr.job_added.connect(self._add_job)
        self.mgr.job_finished.connect(lambda j: (self._tick(), self.job_completed.emit(j)))
        self.timer = QTimer(self, interval=300, timeout=self._tick)
        self.timer.start()

    # -- queue ---------------------------------------------------------------
    def _add_job(self, job):
        it = QTreeWidgetItem([job.title, "queued", "", "", ""])
        it.setData(0, Qt.UserRole, job.id)
        it.setToolTip(0, job.title)
        self.queue.addTopLevelItem(it)
        self.items[job.id] = (it, job)
        if self.cur_job is None or self.cur_job.state in (engine.J_DONE, engine.J_CANCELLED):
            self.queue.setCurrentItem(it)

    def _select_job(self, cur, _prev):
        if cur is None:
            self.cur_job = None
            self.files_model.set_job(None)
            return
        self.cur_job = self.items[cur.data(0, Qt.UserRole)][1]
        self.cur_file = None
        self.follow.setChecked(True)
        self.files_model.set_job(self.cur_job)
        self._tick()

    def _toggle_pause(self):
        j = self.cur_job
        if j and j.state in (engine.J_RUNNING, engine.J_PAUSED, engine.J_PLANNING):
            j.set_paused(not j.paused)
            self._tick()

    def _cancel(self):
        if self.cur_job:
            self.mgr.cancel(self.cur_job)

    def _clear(self):
        self.mgr.clear_finished()
        keep = {j.id for j in self.mgr.jobs}
        for jid in list(self.items):
            if jid not in keep:
                it, _ = self.items.pop(jid)
                self.queue.takeTopLevelItem(self.queue.indexOfTopLevelItem(it))

    def _pick_file(self, idx):
        self.cur_file = self.files_model.file_at(idx.row())
        self.follow.setChecked(False)
        self._tick()

    # -- live refresh --------------------------------------------------------
    def _tick(self):
        for jid, (it, j) in self.items.items():
            if j.t_start is None:
                st = "cancelled" if j.cancelled else "queued"
                it.setText(1, st)
                continue
            s = j.snapshot() if j is not self.cur_job else None
            if s is None:
                continue
            self._fill_item(it, s)
        j = self.cur_job
        if j is None:
            return
        s = j.snapshot()
        if j.id in self.items:
            self._fill_item(self.items[j.id][0], s)
        self.title.setText(s.title)
        frac = (s.bytes_same + s.bytes_done) / s.bytes_total if s.bytes_total else (
            1.0 if s.state == engine.J_DONE else 0.0)
        self.bar.setValue(int(frac * 1000))
        self.btn_pause.setText("Resume" if j.paused else "Pause")
        st = self.stats
        st["files"].setText(f"{s.files_done} copied, {s.files_skipped} up to date, "
                            f"{s.files_failed} failed / {s.files_total}")
        st["data"].setText(f"{fmt_bytes(s.bytes_done)} of {fmt_bytes(s.bytes_needed)}"
                           f"{'+' if s.eta_lower_bound else ''}  (total {fmt_bytes(s.bytes_total)})")
        st["same"].setText(fmt_bytes(s.bytes_same))
        st["hash"].setText(f"{fmt_bytes(s.hash_done)} of {fmt_bytes(s.hash_total)}"
                           if s.hash_total else "–")
        st["speed"].setText(f"{fmt_bytes(s.rate)}/s")
        st["wire"].setText(f"{fmt_bytes(s.wire_bytes)}  ({fmt_bytes(s.wire_rate)}/s)")
        st["elapsed"].setText(fmt_dur(s.elapsed))
        if s.state in (engine.J_DONE, engine.J_FAILED, engine.J_CANCELLED):
            st["eta"].setText(engine.J_NAMES[s.state])
        else:
            st["eta"].setText(("≥ " if s.eta_lower_bound else "") + fmt_dur(s.eta))
        if s.compression in ("none", "…"):
            st["codec"].setText(s.compression)
        else:
            lvl = f"-{j.level}"
            ratio = (f"  — wire is {s.compressed_wire / s.compressed_logical * 100:.0f}% of data"
                     if s.compressed_logical else "")
            st["codec"].setText(f"{s.compression}{lvl}{ratio}")
        st["notes"].setText("; ".join(s.notes[-2:]) or "–")
        st["notes"].setToolTip("\n".join(s.notes))
        if s.errors:
            lines = [f"{rel}: {e}" for rel, e in s.errors[-5:]]
            more = f"  (+{len(s.errors) - 5} more)" if len(s.errors) > 5 else ""
            self.errors.setText("\n".join(lines) + more)
            self.errors.show()
        else:
            self.errors.hide()
        self._fill_workers(s)
        self.files_model.tick()
        self._fill_map(j)

    def _fill_item(self, it, s):
        it.setText(1, engine.J_NAMES[s.state])
        frac = (s.bytes_same + s.bytes_done) / s.bytes_total if s.bytes_total else 0
        it.setText(2, f"{frac * 100:.1f}%  {fmt_bytes(s.bytes_done)}")
        running = s.state == engine.J_RUNNING
        it.setText(3, f"{fmt_bytes(s.rate)}/s" if running else "")
        it.setText(4, (("≥" if s.eta_lower_bound else "") + fmt_dur(s.eta)) if running else "")

    def _fill_workers(self, s):
        w = self.workers
        while w.topLevelItemCount() < len(s.workers):
            w.addTopLevelItem(QTreeWidgetItem(["", "", "", ""]))
        vmax = max([max(i.hist, default=0) for i in s.workers] + [i.rate for i in s.workers]
                   + [1.0])
        while w.topLevelItemCount() > len(s.workers):
            w.takeTopLevelItem(w.topLevelItemCount() - 1)
        for i, info in enumerate(s.workers):
            it = w.topLevelItem(i)
            it.setText(0, f"#{info.idx + 1}")
            it.setText(1, info.desc)
            if info.state == "busy" and info.block >= 0 and info.total:
                f = self.cur_job.by_id.get(info.file_id) if self.cur_job else None
                nb = f"/{f.nblocks}" if f else ""
                it.setData(2, Qt.UserRole, {
                    "segs": [(min(1.0, info.moved / info.total), SEG_ACTIVE)],
                    "text": f"{info.label or f'block {info.block + 1}{nb}'} · "
                            f"{fmt_bytes(info.moved)} / {fmt_bytes(info.total)}"})
                it.setText(2, "")
            else:
                it.setData(2, Qt.UserRole, None)
                it.setText(2, "")
            it.setData(3, Qt.UserRole, {"hist": info.hist, "rate": info.rate, "vmax": vmax})
            it.setForeground(1, QColor("#ef4444") if info.state == "reconnecting"
                             else QColor("#888") if info.state in ("idle", "stopped")
                             else QColor(self.palette().text().color()))

    def _fill_map(self, j):
        f = self.cur_file
        if self.follow.isChecked():
            with j.lock:
                act = [x for x in j.active if x.state == engine.F_WORKING]
            if act:
                f = act[0]
            elif f is None and j.files:
                f = j.files[-1] if j.state in (engine.J_DONE, engine.J_FAILED) else j.files[0]
            self.cur_file = f
            if f is not None:
                row = j.files.index(f) if f in j.files else -1
                if row >= 0 and self.files.currentIndex().row() != row:
                    self.files.selectRow(row)
        if f is None:
            self.blockmap.set_data(b"", {}, 1, 0)
            self.map_label.setText("")
            return
        with j.lock:
            blocks, prog = bytes(f.blocks), dict(f.block_progress)
        self.blockmap.set_data(blocks, prog, f.bs, f.size)
        self.map_label.setText(f"{f.rel} — {f.nblocks} blocks × {fmt_bytes(f.bs)}"
                               f" — {engine.F_NAMES[f.state]}")
