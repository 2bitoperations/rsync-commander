"""Main window: two panels on top, transfers below, NC-style function keys."""

import posixpath
import sys

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFormLayout, QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
                               QSpinBox, QSplitter, QToolBar, QVBoxLayout, QWidget)

from .. import engine
from ..endpoint import LocalEndpoint, RemoteEndpoint, browse_call, ssh_config_hosts
from .panel import LOCAL, Panel
from .transfers import JobManager, TransfersPane
from .util import run_async


class CopyDialog(QDialog):
    def __init__(self, parent, count, src_label, dst_ep_key, dst_dir, opts):
        super().__init__(parent)
        self.setWindowTitle("Copy")
        what = f"{count} item{'s' if count != 1 else ''}"
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"Copy {what} from <b>{src_label}</b><br>into "
                             f"<b>{dst_ep_key}</b>:"))
        self.target = QLineEdit(dst_dir)
        self.target.setMinimumWidth(480)
        lay.addWidget(self.target)
        form = QFormLayout()
        self.checksum = QCheckBox("compare existing files block-by-block (SHA-1)")
        self.checksum.setChecked(opts.checksum)
        self.checksum.setToolTip("Off: files with identical size and mtime are skipped "
                                 "without reading them")
        self.workers = QSpinBox(minimum=1, maximum=32, value=opts.workers)
        self.workers.setToolTip("Parallel ssh streams. More streams help on high-latency links.")
        self.bs = QComboBox()
        for mb in (1, 2, 4, 8, 16, 32):
            self.bs.addItem(f"{mb} MiB", mb << 20)
        self.bs.setCurrentIndex(max(0, self.bs.findData(opts.block_size)))
        self.compress = QComboBox()
        self.compress.addItem("zstd", "zstd")
        self.compress.addItem("gzip", "gzip")
        self.compress.addItem("none", "none")
        self.compress.setCurrentIndex(max(0, self.compress.findData(opts.compression)))
        self.compress.setToolTip(
            "Compresses blocks on network legs only, and only when a block shrinks by over 8%.\n"
            "FITS subs typically go to 35-50%. zstd falls back to gzip on hosts without it\n"
            "(see `rsync-commander install-zstd HOST`).")
        self.level = QSpinBox(minimum=0, maximum=19, value=opts.level)
        self.level.setSpecialValueText("default")
        self.level.setToolTip("0 = default (gzip 1, zstd 3). gzip max 9, zstd max 19.\n"
                              "Higher squeezes a few % more but costs CPU on the sending side.")
        self.compress.currentIndexChanged.connect(
            lambda _i: self.level.setEnabled(self.compress.currentData() != "none"))
        self.level.setEnabled(opts.compression != "none")
        crow = QHBoxLayout()
        crow.addWidget(self.compress)
        crow.addWidget(QLabel("level"))
        crow.addWidget(self.level)
        crow.addStretch(1)
        form.addRow("Verify:", self.checksum)
        form.addRow("Streams:", self.workers)
        form.addRow("Block size:", self.bs)
        form.addRow("Compression:", crow)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Copy")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def options(self):
        return engine.Options(block_size=self.bs.currentData(), workers=self.workers.value(),
                              checksum=self.checksum.isChecked(),
                              compression=self.compress.currentData(),
                              level=self.level.value())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("rsync-commander")
        self.settings = QSettings()
        self.endpoints = {LOCAL: LocalEndpoint()}
        hosts = ssh_config_hosts()
        self.opts = engine.Options(
            workers=int(self.settings.value("opts/workers", 4)),
            block_size=int(self.settings.value("opts/block_size", 4 << 20)),
            checksum=self.settings.value("opts/checksum", "true") in (True, "true"),
            compression=str(self.settings.value("opts/compression", "zstd")),
            level=int(self.settings.value("opts/level", 0)))

        self.left = Panel("left", self.endpoint, hosts)
        self.right = Panel("right", self.endpoint, hosts)
        self.active = self.left
        for p in (self.left, self.right):
            p.drop_requested.connect(lambda key, paths, dst, p=p: self.copy(key, paths, p, dst))
            p.focused.connect(self._set_active)

        top = QSplitter(Qt.Horizontal)
        top.addWidget(self.left)
        top.addWidget(self.right)
        self.mgr = JobManager()
        self.transfers = TransfersPane(self.mgr)
        self.transfers.job_completed.connect(self._job_done)
        main = QSplitter(Qt.Vertical)
        main.addWidget(top)
        main.addWidget(self.transfers)
        main.setSizes([420, 380])

        keys = QHBoxLayout()
        keys.setContentsMargins(4, 0, 4, 2)
        for label, fn in (("F5 Copy", self.copy_selected), ("F6 Rename", self.rename),
                          ("F7 MkDir", self.mkdir), ("F8 Delete", self.delete),
                          ("Ctrl+R Refresh", lambda: self.active.refresh()),
                          ("Ctrl+U Swap", self.swap), ("Tab Switch panel", self.switch)):
            b = QPushButton(label)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(fn)
            keys.addWidget(b)
        keys.addStretch(1)
        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(main, 1)
        cl.addLayout(keys)
        self.setCentralWidget(central)

        for seq, fn in (("F5", self.copy_selected), ("F6", self.rename), ("F7", self.mkdir),
                        ("F8", self.delete), ("Ctrl+R", lambda: self.active.refresh()),
                        ("Ctrl+U", self.swap), ("F10", self.close)):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.WindowShortcut)
            sc.activated.connect(fn)
        # Keys that mean something else in text fields only act on the file lists.
        for p in (self.left, self.right):
            for seq, fn in (("Delete", self.delete), ("Tab", self.switch)):
                sc = QShortcut(QKeySequence(seq), p.view)
                sc.setContext(Qt.WidgetShortcut)
                sc.activated.connect(fn)

        geo = self.settings.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        else:
            self.resize(1300, 900)
        self.left.restore()
        self.right.restore()
        self.left.view.setFocus()

    # -- endpoints -----------------------------------------------------------
    def endpoint(self, key):
        ep = self.endpoints.get(key)
        if ep is None:
            ep = self.endpoints[key] = RemoteEndpoint(key)
        return ep

    def other(self, panel):
        return self.right if panel is self.left else self.left

    def _set_active(self, p):
        self.active = p
        p.set_active(True)
        self.other(p).set_active(False)

    def switch(self):
        self.other(self.active).view.setFocus()

    def swap(self):
        lk, lp = self.left.ep_key, self.left.cwd
        rk, rp = self.right.ep_key, self.right.cwd
        self.left.history[rk] = rp
        self.right.history[lk] = lp
        self.left.set_endpoint(rk)
        self.right.set_endpoint(lk)

    # -- operations ----------------------------------------------------------
    def copy_selected(self):
        src = self.active
        paths = src.selected_paths()
        if paths:
            dst = self.other(src)
            self.copy(src.ep_key, paths, dst, dst.cwd)

    def copy(self, src_key, paths, dst_panel, dst_dir):
        dlg = CopyDialog(self, len(paths), src_key + ":" + posixpath.dirname(paths[0]),
                         dst_panel.ep_key, dst_dir, self.opts)
        if dlg.exec() != QDialog.Accepted:
            return
        opts = dlg.options()
        self.opts = opts
        for k, v in (("workers", opts.workers), ("block_size", opts.block_size),
                     ("checksum", opts.checksum), ("compression", opts.compression),
                     ("level", opts.level)):
            self.settings.setValue(f"opts/{k}", v)
        job = engine.Job(self.endpoint(src_key), paths, self.endpoint(dst_panel.ep_key),
                         dlg.target.text().strip() or dst_dir, opts)
        job.dst_key = dst_panel.ep_key
        self.mgr.add(job)

    def _job_done(self, job):
        for p in (self.left, self.right):
            if p.ep_key == getattr(job, "dst_key", None) and (
                    p.cwd == job.dst_dir or p.cwd.startswith(job.dst_dir.rstrip("/") + "/")):
                p.refresh()

    def _run_op(self, panel, label, fn):
        ep = panel.endpoint
        panel._set_status(f"{label}…")

        def done(_r, ex):
            if ex:
                QMessageBox.warning(self, "rsync-commander", f"{label} failed:\n{ex}")
            panel.refresh()
        run_async(lambda: browse_call(ep, fn), done)

    def mkdir(self):
        p = self.active
        name, ok = QInputDialog.getText(self, "Make directory", f"New directory in {p.cwd}:")
        if ok and name.strip():
            path = p.join(name.strip())
            self._run_op(p, f"mkdir {path}", lambda s: s.mkdir(path))
            p.history[p.ep_key] = p.cwd

    def rename(self):
        p = self.active
        paths = p.selected_paths()
        if len(paths) != 1:
            return
        old = paths[0]
        name, ok = QInputDialog.getText(self, "Rename", f"Rename {posixpath.basename(old)} to:",
                                        text=posixpath.basename(old))
        if ok and name.strip() and name.strip() != posixpath.basename(old):
            new = posixpath.join(posixpath.dirname(old), name.strip())
            self._run_op(p, f"rename {old}", lambda s: s.rename(old, new))

    def delete(self):
        p = self.active
        paths = p.selected_paths()
        if not paths:
            return
        names = "\n".join(posixpath.basename(x) for x in paths[:12])
        if len(paths) > 12:
            names += f"\n… and {len(paths) - 12} more"
        r = QMessageBox.question(
            self, "Delete",
            f"Permanently delete {len(paths)} item(s) on {p.ep_key}:{p.cwd}?\n\n{names}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r == QMessageBox.Yes:
            def fn(s):
                for x in paths:
                    s.delete(x)
            self._run_op(p, f"delete {len(paths)} item(s)", fn)

    def closeEvent(self, ev):
        running = [j for j in self.mgr.jobs if j.state in (engine.J_RUNNING, engine.J_PAUSED,
                                                             engine.J_PLANNING)
                   and j.t_start is not None]
        if running:
            r = QMessageBox.question(self, "Quit", "A transfer is running. Cancel it and quit?\n"
                                     "(It can be resumed later: partial files are kept.)")
            if r != QMessageBox.Yes:
                ev.ignore()
                return
            for j in running:
                j.cancel()
        self.settings.setValue("geometry", self.saveGeometry())
        for ep in self.endpoints.values():
            ep.close()
        ev.accept()


def run():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setOrganizationName("2bitoperations")
    app.setApplicationName("rsync-commander")
    w = MainWindow()
    w.show()
    return app.exec()
