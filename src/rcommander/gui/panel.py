"""One side of the commander: endpoint picker, path bar and file list."""

import json
import posixpath
import time

from PySide6.QtCore import (QAbstractTableModel, QMimeData, QModelIndex, QSettings, Qt,
                            QUrl, Signal)
from PySide6.QtGui import QColor, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileIconProvider,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QToolButton,
                               QTreeView, QVBoxLayout, QWidget, QFileIconProvider)

from ..endpoint import browse_call
from ..fmt import fmt_bytes
from .util import run_async

MIME = "application/x-rcommander-items"
LOCAL = "Local"


class FileModel(QAbstractTableModel):
    COLS = ("Name", "Size", "Modified")

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.entries = []
        self.sort_col, self.sort_order = 0, Qt.AscendingOrder
        icons = QFileIconProvider()
        self.dir_icon = icons.icon(QFileIconProvider.Folder)
        self.file_icon = icons.icon(QFileIconProvider.File)

    def set_entries(self, entries, at_root):
        self.beginResetModel()
        self.entries = list(entries)
        self._sort()
        if not at_root:
            self.entries.insert(0, {"name": "..", "type": "up", "size": 0, "mtime_ns": 0})
        self.endResetModel()

    def _sort(self):
        rev = self.sort_order == Qt.DescendingOrder
        key = {0: lambda e: e["name"].lower(), 1: lambda e: e["size"],
               2: lambda e: e["mtime_ns"]}[self.sort_col]
        dirs = sorted((e for e in self.entries if e["type"] == "d"), key=key, reverse=rev)
        files = sorted((e for e in self.entries if e["type"] != "d"), key=key, reverse=rev)
        self.entries = dirs + files

    def sort(self, column, order=Qt.AscendingOrder):
        self.beginResetModel()
        up = [e for e in self.entries if e["type"] == "up"]
        self.entries = [e for e in self.entries if e["type"] != "up"]
        self.sort_col, self.sort_order = column, order
        self._sort()
        self.entries = up + self.entries
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent=QModelIndex()):
        return len(self.COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.COLS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        e = self.entries[index.row()]
        c = index.column()
        if role == Qt.DisplayRole:
            if c == 0:
                return "[..]" if e["type"] == "up" else e["name"]
            if c == 1:
                if e["type"] == "d":
                    return "<DIR>"
                return "" if e["type"] == "up" else fmt_bytes(e["size"])
            if c == 2 and e["type"] != "up":
                return time.strftime("%Y-%m-%d %H:%M", time.localtime(e["mtime_ns"] / 1e9))
        elif role == Qt.DecorationRole and c == 0:
            return self.dir_icon if e["type"] in ("d", "up") else self.file_icon
        elif role == Qt.TextAlignmentRole and c == 1:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        elif role == Qt.ForegroundRole and e["name"].startswith(".") and e["type"] != "up":
            return QColor("#888")
        elif role == Qt.ToolTipRole and c == 1 and e["type"] == "f":
            return f"{e['size']:,} bytes"
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemIsDropEnabled
        e = self.entries[index.row()]
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if e["type"] != "up":
            f |= Qt.ItemIsDragEnabled
        if e["type"] in ("d", "up"):
            f |= Qt.ItemIsDropEnabled
        return f

    # -- drag and drop -------------------------------------------------------
    def supportedDropActions(self):
        return Qt.CopyAction

    def supportedDragActions(self):
        return Qt.CopyAction

    def mimeTypes(self):
        return [MIME, "text/uri-list"]

    def mimeData(self, indexes):
        rows = sorted({i.row() for i in indexes})
        paths = [self.panel.join(self.entries[r]["name"]) for r in rows
                 if self.entries[r]["type"] != "up"]
        md = QMimeData()
        md.setData(MIME, json.dumps({"ep": self.panel.ep_key, "paths": paths}).encode())
        if self.panel.ep_key == LOCAL:
            md.setUrls([QUrl.fromLocalFile(p) for p in paths])
        return md

    def canDropMimeData(self, data, action, row, column, parent):
        return data.hasFormat(MIME) or data.hasUrls()

    def dropMimeData(self, data, action, row, column, parent):
        target = self.panel.cwd
        if parent.isValid() and row == -1:
            e = self.entries[parent.row()]
            if e["type"] == "d":
                target = self.panel.join(e["name"])
            elif e["type"] == "up":
                target = posixpath.dirname(self.panel.cwd.rstrip("/")) or "/"
        if data.hasFormat(MIME):
            d = json.loads(bytes(data.data(MIME)).decode())
            src_key, paths = d["ep"], d["paths"]
        elif data.hasUrls():
            src_key = LOCAL
            paths = [u.toLocalFile() for u in data.urls() if u.isLocalFile()]
        else:
            return False
        if not paths:
            return False
        if src_key == self.panel.ep_key and all(
                posixpath.dirname(p.rstrip("/")) == target.rstrip("/") or p == target
                for p in paths):
            return False  # dropped onto its own directory
        self.panel.drop_requested.emit(src_key, paths, target)
        return True


class FileView(QTreeView):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.setRootIsDecorated(False)
        self.setItemsExpandable(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)
        self.setDragDropOverwriteMode(True)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.panel.open_index(self.currentIndex())
            return
        if ev.key() == Qt.Key_Backspace:
            self.panel.go_up()
            return
        if ev.key() == Qt.Key_Insert or (ev.key() == Qt.Key_Space and not ev.modifiers()):
            # NC-style: toggle selection and move down
            idx = self.currentIndex()
            if idx.isValid():
                sm = self.selectionModel()
                sm.select(idx, sm.SelectionFlag.Toggle | sm.SelectionFlag.Rows)
                nxt = self.model().index(idx.row() + 1, 0)
                if nxt.isValid():
                    sm.setCurrentIndex(nxt, sm.SelectionFlag.NoUpdate)
            return
        super().keyPressEvent(ev)

    def focusInEvent(self, ev):
        super().focusInEvent(ev)
        self.panel.focused.emit(self.panel)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasFormat(MIME) or ev.mimeData().hasUrls():
            ev.setDropAction(Qt.CopyAction)
            ev.accept()
        else:
            super().dragEnterEvent(ev)

    def dragMoveEvent(self, ev):
        super().dragMoveEvent(ev)
        if ev.mimeData().hasFormat(MIME) or ev.mimeData().hasUrls():
            ev.setDropAction(Qt.CopyAction)
            ev.accept()

    def dropEvent(self, ev):
        # Resolve the target ourselves: dropping on a file means "this directory".
        idx = self.indexAt(ev.position().toPoint())
        parent = QModelIndex()
        if idx.isValid():
            e = self.model().entries[idx.row()]
            if e["type"] in ("d", "up"):
                parent = self.model().index(idx.row(), 0)
        if self.model().dropMimeData(ev.mimeData(), Qt.CopyAction, -1, -1, parent):
            ev.setDropAction(Qt.CopyAction)
            ev.accept()
        else:
            ev.ignore()


class Panel(QWidget):
    drop_requested = Signal(str, list, str)      # src_ep_key, paths, dst_dir
    focused = Signal(object)

    def __init__(self, name, registry, hosts):
        super().__init__()
        self.name = name
        self.registry = registry                  # callable key -> Endpoint
        self.ep_key = LOCAL
        self.cwd = "/"
        self.history = {}
        self.settings = QSettings()
        self._gen = 0

        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.setInsertPolicy(QComboBox.NoInsert)
        self.combo.addItems([LOCAL] + hosts)
        self.combo.setMinimumContentsLength(10)
        self.combo.setToolTip("Local, a Host from ~/.ssh/config, or type user@host")
        self.combo.activated.connect(lambda _i: self.set_endpoint(self.combo.currentText()))
        self.combo.lineEdit().returnPressed.connect(
            lambda: self.set_endpoint(self.combo.currentText()))
        self.path_edit = QLineEdit()
        self.path_edit.returnPressed.connect(lambda: self.navigate(self.path_edit.text()))
        up = QToolButton(text="↑")
        up.setToolTip("Parent directory (Backspace)")
        up.clicked.connect(self.go_up)
        refresh = QToolButton(text="⟳")
        refresh.setToolTip("Refresh (Ctrl+R)")
        refresh.clicked.connect(self.refresh)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.combo)
        top.addWidget(self.path_edit, 1)
        top.addWidget(up)
        top.addWidget(refresh)

        self.model = FileModel(self)
        self.view = FileView(self)
        self.view.setModel(self.model)
        self.view.sortByColumn(0, Qt.AscendingOrder)
        h = self.view.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.view.doubleClicked.connect(self.open_index)
        self.view.selectionModel().selectionChanged.connect(lambda *_: self._update_status())

        self.status = QLabel()
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addLayout(top)
        lay.addWidget(self.view, 1)
        lay.addWidget(self.status)

    # -- paths ---------------------------------------------------------------
    @property
    def endpoint(self):
        return self.registry(self.ep_key)

    def join(self, name):
        return posixpath.join(self.cwd, name)

    def selected_paths(self):
        rows = sorted({i.row() for i in self.view.selectionModel().selectedRows()})
        if not rows and self.view.currentIndex().isValid():
            rows = [self.view.currentIndex().row()]
        return [self.join(self.model.entries[r]["name"]) for r in rows
                if self.model.entries[r]["type"] != "up"]

    def selected_entries(self):
        rows = sorted({i.row() for i in self.view.selectionModel().selectedRows()})
        return [self.model.entries[r] for r in rows if self.model.entries[r]["type"] != "up"]

    # -- navigation ----------------------------------------------------------
    def restore(self):
        key = self.settings.value(f"{self.name}/endpoint", LOCAL)
        self.set_endpoint(key)

    def set_endpoint(self, key):
        key = key.strip() or LOCAL
        if key.lower() == "local":
            key = LOCAL
        if self.combo.findText(key) < 0:
            self.combo.addItem(key)
        self.combo.setCurrentText(key)
        if self.ep_key != key:
            self.history[self.ep_key] = self.cwd
        self.ep_key = key
        self.settings.setValue(f"{self.name}/endpoint", key)
        last = self.history.get(key) or self.settings.value(f"{self.name}/path/{key}")
        if last:
            self.navigate(last)
            return
        self._set_status(f"connecting to {key}…")
        ep = self.endpoint
        self._gen += 1
        gen = self._gen

        def done(home, ex):
            if gen != self._gen:
                return  # user navigated somewhere else meanwhile
            if ex:
                self._set_status(f"⚠ {key}: {ex}", error=True)
                return
            self.navigate(home)
        run_async(ep.home, done)

    def navigate(self, path, select=None):
        path = posixpath.normpath(path or "/")
        if not path.startswith("/"):
            path = posixpath.normpath(posixpath.join(self.cwd, path))
        self._gen += 1
        gen, key, ep = self._gen, self.ep_key, self.endpoint
        self._set_status(f"listing {path}…")
        old_cwd = self.cwd

        def done(listing, ex):
            if gen != self._gen:
                return  # superseded by a newer navigation
            if ex:
                self._set_status(f"⚠ {path}: {ex}", error=True)
                self.path_edit.setText(self.cwd)
                return
            self.cwd = listing["path"]
            self.path_edit.setText(self.cwd)
            self.model.set_entries(listing["entries"], at_root=self.cwd == "/")
            self.settings.setValue(f"{self.name}/path/{key}", self.cwd)
            self._select_name(select if select else (
                posixpath.basename(old_cwd) if posixpath.dirname(old_cwd) == self.cwd else None))
            self._update_status()

        run_async(lambda: browse_call(ep, lambda s: s.listdir(path)), done)

    def _select_name(self, name):
        row = 0
        if name:
            for i, e in enumerate(self.model.entries):
                if e["name"] == name:
                    row = i
                    break
        idx = self.model.index(row, 0)
        if idx.isValid():
            self.view.setCurrentIndex(idx)
            self.view.scrollTo(idx)

    def refresh(self):
        cur = self.view.currentIndex()
        name = self.model.entries[cur.row()]["name"] if cur.isValid() and self.model.entries else None
        self.navigate(self.cwd, select=name)

    def go_up(self):
        if self.cwd != "/":
            self.navigate(posixpath.dirname(self.cwd.rstrip("/")) or "/")

    def open_index(self, idx):
        if not idx.isValid():
            return
        e = self.model.entries[idx.row()]
        if e["type"] == "up":
            self.go_up()
        elif e["type"] == "d":
            self.navigate(self.join(e["name"]))

    # -- status --------------------------------------------------------------
    def _set_status(self, text, error=False):
        self.status.setStyleSheet("color: #c33;" if error else "")
        self.status.setText(text)

    def _update_status(self):
        ents = [e for e in self.model.entries if e["type"] != "up"]
        nd = sum(1 for e in ents if e["type"] == "d")
        total = sum(e["size"] for e in ents if e["type"] == "f")
        sel = self.selected_entries()
        txt = f"{len(ents) - nd} files, {nd} dirs, {fmt_bytes(total)}"
        if sel:
            ssize = sum(e["size"] for e in sel if e["type"] == "f")
            txt = f"{len(sel)} selected ({fmt_bytes(ssize)}) of " + txt
        self._set_status(txt)

    def set_active(self, active):
        self.view.setStyleSheet("" if active else "QTreeView { selection-background-color: #8899aa; }")
