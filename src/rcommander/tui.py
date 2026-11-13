"""Interactive terminal UI: the two-panel commander in a terminal (Textual).

    rsync-commander tui [LEFT] [RIGHT]      # e.g.  tui ~/astro nas:/volume1/astro

Keys (also shown in the footer and clickable):
  Tab switch panel   Enter open dir   Backspace parent   Space/Insert mark
  F2 host for this panel   Ctrl+L type a path   Ctrl+R refresh   Ctrl+U swap panels
  F5 copy to other panel   F6 rename   F7 mkdir   F8 delete   F9 transfers   F10 quit
"""

import os
import posixpath
import threading
import time

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (Button, Checkbox, DataTable, Footer, Input, Label, OptionList,
                             Select, Static)

from . import engine
from .endpoint import LocalEndpoint, RemoteEndpoint, browse_call, ssh_config_hosts
from .fmt import fmt_bytes, fmt_dur
from .tuistyle import graph, meter

LOCAL = "Local"

BLOCK_STYLE = {
    engine.B_UNKNOWN: "grey35", engine.B_PENDING: "grey58", engine.B_HASHING: "medium_purple1",
    engine.B_SAME: "dark_sea_green4", engine.B_ACTIVE: "orange1", engine.B_DONE: "green3",
    engine.B_FAILED: "red1",
}
FILE_STYLE = {engine.F_DONE: "green3", engine.F_SKIPPED: "dark_sea_green4",
              engine.F_FAILED: "red1", engine.F_WORKING: "orange1",
              engine.F_PREPARING: "medium_purple1", engine.F_FINALIZING: "medium_purple1"}


def rate_history(job):
    """Per-job speed samples for the braille graphs (sampled by the app)."""
    if not hasattr(job, "_rates"):
        import collections
        job._rates = collections.deque(maxlen=400)
        job._rate_t = 0.0
    return job._rates


def sample_rate(job, snap):
    h = rate_history(job)
    now = time.time()
    if now - job._rate_t >= 0.5 and snap.state in (engine.J_RUNNING, engine.J_PAUSED):
        h.append(snap.rate)
        job._rate_t = now


def block_map(blocks, progress, bs, size, width):
    """One character per block; active blocks show how full they are."""
    t = Text()
    width = max(10, width)
    partial = "▁▂▃▄▅▆▇█"
    for i, st in enumerate(blocks):
        if st == engine.B_ACTIVE:
            blen = min(bs, size - i * bs) or 1
            frac = min(1.0, progress.get(i, 0) / blen)
            t.append(partial[min(7, int(frac * 8))], style="orange1 on grey30")
        else:
            t.append("█", style=BLOCK_STYLE.get(st, "grey35"))
        if (i + 1) % width == 0:
            t.append("\n")
    return t


def legend():
    t = Text()
    for st, name in ((engine.B_UNKNOWN, "unchecked"), (engine.B_HASHING, "checksumming"),
                     (engine.B_SAME, "identical"), (engine.B_PENDING, "pending"),
                     (engine.B_ACTIVE, "transferring"), (engine.B_DONE, "sent"),
                     (engine.B_FAILED, "failed")):
        t.append("█", style=BLOCK_STYLE[st])
        t.append(f" {name}  ", style="grey70")
    return t


class JobQueue:
    """Runs jobs one at a time (they would only fight over the link)."""

    def __init__(self, on_change):
        self.jobs = []
        self.running = None
        self.on_change = on_change
        self._lock = threading.Lock()

    def add(self, job):
        job.on_finished = self._finished
        with self._lock:
            self.jobs.append(job)
        self._kick()

    def _finished(self, job):
        with self._lock:
            if job is self.running:
                self.running = None
        self._kick()
        self.on_change(job)

    def _kick(self):
        with self._lock:
            if self.running is not None:
                return
            for j in self.jobs:
                if j.t_start is None and not j.cancelled:
                    self.running = j
                    j.start()
                    return

    def cancel(self, job):
        if job.t_start is None:
            job.cancelled = True
            job.state = engine.J_CANCELLED
        else:
            job.cancel()


# -- modals ---------------------------------------------------------------------

class Ask(ModalScreen):
    """Single-line input. dismiss(str|None)."""
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title, value=""):
        super().__init__()
        self.title_text, self.value = title, value

    def compose(self):
        with Vertical(classes="dialog"):
            yield Label(self.title_text)
            yield Input(value=self.value, id="ask")
            with Horizontal(classes="buttons"):
                yield Button("OK", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def ok(self):
        self.dismiss(self.query_one("#ask", Input).value)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self):
        self.dismiss(None)


class Confirm(ModalScreen):
    BINDINGS = [Binding("escape", "no", "No"), Binding("y", "yes", "Yes")]

    def __init__(self, text):
        super().__init__()
        self.text = text

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static(self.text)
            with Horizontal(classes="buttons"):
                yield Button("Yes (y)", variant="error", id="yes")
                yield Button("No (Esc)", variant="primary", id="no")

    def on_mount(self):
        self.query_one("#no", Button).focus()

    @on(Button.Pressed, "#yes")
    def action_yes(self):
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def action_no(self):
        self.dismiss(False)


class HostPicker(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, hosts):
        super().__init__()
        self.hosts = [LOCAL] + hosts

    def compose(self):
        with Vertical(classes="dialog"):
            yield Label("Endpoint for this panel (or type user@host):")
            yield OptionList(*self.hosts, id="hosts")
            yield Input(placeholder="user@host", id="custom")

    @on(OptionList.OptionSelected)
    def picked(self, ev):
        self.dismiss(self.hosts[ev.option_index])

    @on(Input.Submitted)
    def typed(self, ev):
        if ev.value.strip():
            self.dismiss(ev.value.strip())

    def action_cancel(self):
        self.dismiss(None)


class CopyDialog(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, what, target, opts):
        super().__init__()
        self.what, self.target, self.opts = what, target, opts

    def compose(self):
        o = self.opts
        with Vertical(classes="dialog wide"):
            yield Label(self.what)
            yield Input(value=self.target, id="target")
            yield Checkbox("compare existing files block-by-block (SHA-1)", o.checksum,
                           id="checksum")
            with Horizontal(classes="row"):
                yield Label("streams ")
                yield Input(str(o.workers), id="workers", classes="num")
                yield Label("  block MiB ")
                yield Input(str(o.block_size >> 20), id="bs", classes="num")
                yield Label("  compression ")
                yield Select([(c, c) for c in engine.COMPRESSIONS], value=o.compression,
                             allow_blank=False, id="comp")
                yield Label("  level ")
                yield Input(str(o.level), id="level", classes="num")
            with Horizontal(classes="buttons"):
                yield Button("Copy", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self):
        self.query_one("#ok", Button).focus()

    @on(Button.Pressed, "#ok")
    @on(Input.Submitted)
    def ok(self):
        def num(i, d):
            try:
                return max(0, int(self.query_one(i, Input).value))
            except ValueError:
                return d
        o = engine.Options(
            workers=max(1, num("#workers", 4)), block_size=max(1, num("#bs", 4)) << 20,
            checksum=self.query_one("#checksum", Checkbox).value,
            compression=self.query_one("#comp", Select).value, level=num("#level", 0))
        self.dismiss((self.query_one("#target", Input).value.strip(), o))

    @on(Button.Pressed, "#cancel")
    def action_cancel(self):
        self.dismiss(None)


# -- panels -----------------------------------------------------------------------

class Panel(Vertical):
    def __init__(self, name, app_ref, ep_key=LOCAL, path=None):
        super().__init__(id=name, classes="panel")
        self.cmdr = app_ref
        self.ep_key = ep_key
        self.cwd = path
        self.entries = []
        self.marked = set()
        self._gen = 0

    def compose(self):
        yield Static("", classes="pathbar")
        yield DataTable(cursor_type="row", zebra_stripes=True)
        yield Static("", classes="status")

    def on_mount(self):
        t = self.table
        t.add_column("Name", key="name")
        t.add_column("Size", key="size", width=11)
        t.add_column("Modified", key="mtime", width=16)
        self.set_endpoint(self.ep_key, self.cwd)

    @property
    def table(self):
        return self.query_one(DataTable)

    @property
    def endpoint(self):
        return self.cmdr.endpoint(self.ep_key)

    def join(self, name):
        return posixpath.join(self.cwd, name)

    def set_endpoint(self, key, path=None):
        self.ep_key = key
        self.marked.clear()
        if path:
            self.navigate(path)
            return
        self.status(f"connecting to {key}…")
        self._gen += 1
        gen = self._gen
        ep = self.endpoint

        def work():
            try:
                home = ep.home()
                self.cmdr.call_from_thread(lambda: gen == self._gen and self.navigate(home))
            except Exception as ex:
                self.cmdr.call_from_thread(self.status, f"⚠ {key}: {ex}", True)
        threading.Thread(target=work, daemon=True).start()

    def navigate(self, path, select=None):
        path = posixpath.normpath(path if path.startswith("/") else
                                  posixpath.join(self.cwd or "/", path))
        self._gen += 1
        gen, ep, old = self._gen, self.endpoint, self.cwd
        self.status(f"listing {path}…")

        def work():
            try:
                listing = browse_call(ep, lambda s: s.listdir(path))
            except Exception as ex:
                self.cmdr.call_from_thread(self.status, f"⚠ {path}: {ex}", True)
                return
            if select is None and old and posixpath.dirname(old) == listing["path"]:
                sel = posixpath.basename(old)
            else:
                sel = select
            self.cmdr.call_from_thread(lambda: gen == self._gen and self._show(listing, sel))
        threading.Thread(target=work, daemon=True).start()

    def _show(self, listing, select):
        self.cwd = listing["path"]
        ents = listing["entries"]
        dirs = sorted((e for e in ents if e["type"] == "d"), key=lambda e: e["name"].lower())
        files = sorted((e for e in ents if e["type"] != "d"), key=lambda e: e["name"].lower())
        self.entries = ([{"name": "..", "type": "up", "size": 0, "mtime_ns": 0}]
                        if self.cwd != "/" else []) + dirs + files
        self.marked.clear()
        t = self.table
        t.clear()
        row = 0
        for i, e in enumerate(self.entries):
            t.add_row(*self._cells(e), key=str(i))
            if select and e["name"] == select:
                row = i
        if self.entries:
            t.move_cursor(row=row)
        self.query_one(".pathbar", Static).update(
            Text(f" {self.ep_key}:{self.cwd}" if self.ep_key != LOCAL else f" {self.cwd}",
                 style="bold"))
        self.update_status()

    def _cells(self, e):
        marked = e["name"] in self.marked
        if e["type"] == "up":
            return Text("/..", style="bold"), "", ""
        style = "bold yellow" if marked else ("bold" if e["type"] == "d" else "")
        if e["name"].startswith(".") and not marked:
            style += " dim"
        name = ("/" if e["type"] == "d" else " ") + e["name"]
        size = "<DIR>" if e["type"] == "d" else fmt_bytes(e["size"])
        mt = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["mtime_ns"] / 1e9))
        return Text(name, style=style), Text(size, justify="right", style=style), mt

    def refresh_row(self, i):
        name, size, mt = self._cells(self.entries[i])
        t = self.table
        t.update_cell(str(i), "name", name)
        t.update_cell(str(i), "size", size)

    def current(self):
        r = self.table.cursor_row
        return self.entries[r] if 0 <= r < len(self.entries) else None

    def selected_paths(self):
        if self.marked:
            return [self.join(n) for n in sorted(self.marked)]
        e = self.current()
        return [self.join(e["name"])] if e and e["type"] != "up" else []

    def toggle_mark(self):
        r = self.table.cursor_row
        if not (0 <= r < len(self.entries)) or self.entries[r]["type"] == "up":
            return
        n = self.entries[r]["name"]
        self.marked.symmetric_difference_update({n})
        self.refresh_row(r)
        self.table.move_cursor(row=min(r + 1, len(self.entries) - 1))
        self.update_status()

    def open_current(self):
        e = self.current()
        if not e:
            return
        if e["type"] == "up":
            self.go_up()
        elif e["type"] == "d":
            self.navigate(self.join(e["name"]))

    def go_up(self):
        if self.cwd and self.cwd != "/":
            self.navigate(posixpath.dirname(self.cwd.rstrip("/")) or "/")

    def refresh_listing(self):
        e = self.current()
        if self.cwd:
            self.navigate(self.cwd, select=e["name"] if e else None)

    def status(self, text, error=False):
        self.query_one(".status", Static).update(Text(text, style="red" if error else ""))

    def update_status(self):
        ents = [e for e in self.entries if e["type"] != "up"]
        nd = sum(1 for e in ents if e["type"] == "d")
        total = sum(e["size"] for e in ents if e["type"] == "f")
        txt = f"{len(ents) - nd} files, {nd} dirs, {fmt_bytes(total)}"
        if self.marked:
            ms = sum(e["size"] for e in ents if e["name"] in self.marked and e["type"] == "f")
            txt = f"{len(self.marked)} marked ({fmt_bytes(ms)}) · " + txt
        self.status(txt)


# -- transfers screen ----------------------------------------------------------------

class TransfersScreen(Screen):
    BINDINGS = [Binding("escape,f9", "app.pop_screen", "Back"),
                Binding("p", "pause", "Pause/Resume"), Binding("c", "cancel_job", "Cancel job"),
                Binding("left_square_bracket", "prev_job", "Prev job"),
                Binding("right_square_bracket", "next_job", "Next job"),
                Binding("f", "follow", "Follow active")]

    def __init__(self, queue):
        super().__init__()
        self.queue = queue
        self.idx = max(0, len(queue.jobs) - 1)
        self.file = None
        self.follow = True
        self._rows = {}

    def compose(self):
        with Horizontal(id="top"):
            yield Static(id="summary")
            yield Static(id="speedgraph")
        yield DataTable(id="workers", cursor_type="none")
        yield DataTable(id="files", cursor_type="row", zebra_stripes=True)
        yield Static(id="maphead")
        yield Static(id="map")
        yield Static(legend(), id="legend")
        yield Footer()

    def on_mount(self):
        w = self.query_one("#workers", DataTable)
        w.add_column("#", width=3)
        w.add_column("block", key="bar", width=22)
        w.add_column("activity", key="desc")
        f = self.query_one("#files", DataTable)
        for name, key, width in (("File", "file", None), ("Size", "size", 10),
                                 ("State", "state", 11), ("Progress", "prog", 17),
                                 ("Identical", "same", 10), ("Sent", "sent", 10)):
            f.add_column(name, key=key, width=width)
        self.set_interval(0.4, self.tick)
        self.tick()

    @property
    def job(self):
        jobs = self.queue.jobs
        return jobs[min(self.idx, len(jobs) - 1)] if jobs else None

    def action_prev_job(self):
        self.idx = max(0, self.idx - 1)
        self._reset()

    def action_next_job(self):
        self.idx = min(len(self.queue.jobs) - 1, self.idx + 1)
        self._reset()

    def _reset(self):
        self._rows = {}
        self.file = None
        self.follow = True
        self.query_one("#files", DataTable).clear()
        self.tick()

    def action_pause(self):
        j = self.job
        if j and j.state in (engine.J_RUNNING, engine.J_PAUSED, engine.J_PLANNING):
            j.set_paused(not j.paused)

    def action_cancel_job(self):
        if self.job:
            self.queue.cancel(self.job)

    def action_follow(self):
        self.follow = True

    @on(DataTable.RowHighlighted, "#files")
    def pick(self, ev):
        j = self.job
        if j and ev.row_key is not None and self.screen.focused is ev.data_table:
            f = j.by_id.get(int(ev.row_key.value))
            if f is not None and f is not self.file:
                self.file = f
                self.follow = False

    def tick(self):
        j = self.job
        summary = self.query_one("#summary", Static)
        if j is None:
            summary.update("No transfers yet. Mark files and press F5 in the panels.")
            return
        s = j.snapshot()
        frac = (s.bytes_same + s.bytes_done) / s.bytes_total if s.bytes_total else (
            1.0 if s.state == engine.J_DONE else 0.0)
        t = Text()
        t.append(f"Job {self.idx + 1}/{len(self.queue.jobs)}  ", style="grey70")
        t.append(s.title + "\n", style="bold")
        t.append_text(meter(frac, 50))
        t.append(f" {frac * 100:5.1f}%  {engine.J_NAMES[s.state]}"
                 f"{' (paused)' if j.paused else ''}\n")
        t.append(f"files {s.files_done} copied, {s.files_skipped} up to date, "
                 f"{s.files_failed} failed / {s.files_total}   "
                 f"sent {fmt_bytes(s.bytes_done)} of {fmt_bytes(s.bytes_needed)}"
                 f"{'+' if s.eta_lower_bound else ''}   identical {fmt_bytes(s.bytes_same)}\n")
        if s.hash_total:
            t.append_text(meter(s.hash_done / s.hash_total, 20, "hash"))
            t.append(f" checksummed {fmt_bytes(s.hash_done)} of {fmt_bytes(s.hash_total)}   ")
        t.append(f"speed {fmt_bytes(s.rate)}/s   wire {fmt_bytes(s.wire_bytes)} "
                 f"({fmt_bytes(s.wire_rate)}/s)   elapsed {fmt_dur(s.elapsed)}   ")
        if s.state in (engine.J_DONE, engine.J_FAILED, engine.J_CANCELLED):
            t.append("finished", style="bold")
        else:
            t.append(f"ETA {'≥' if s.eta_lower_bound else ''}{fmt_dur(s.eta)}", style="bold")
        ratio = (f" (wire {s.compressed_wire / s.compressed_logical * 100:.0f}% of data)"
                 if s.compressed_logical else "")
        t.append(f"\ncompression {s.compression}{ratio}")
        if s.notes:
            t.append("   note: " + s.notes[-1], style="yellow")
        for rel, err in s.errors[-3:]:
            t.append(f"\n✗ {rel}: {err}", style="red")
        summary.update(t)
        sample_rate(j, s)
        hist = rate_history(j)
        peak = max(hist) if hist else 0
        gw = max(20, self.query_one("#speedgraph", Static).size.width - 2)
        g = Text(f"speed {fmt_bytes(s.rate)}/s   peak {fmt_bytes(peak)}/s\n", style="bold")
        g.append_text(graph(hist, gw, 4))
        self.query_one("#speedgraph", Static).update(g)

        w = self.query_one("#workers", DataTable)
        if w.row_count != len(s.workers):
            w.clear()
            for info in s.workers:
                w.add_row(str(info.idx + 1), "", "", key=str(info.idx))
        for info in s.workers:
            b = ""
            if info.state == "busy" and info.block >= 0 and info.total:
                fr = min(1.0, info.moved / info.total)
                b = meter(fr, 14, "block")
                b.append(f" {fr * 100:3.0f}%")
            style = "red" if info.state == "reconnecting" else (
                "grey50" if info.state in ("idle", "stopped") else "")
            w.update_cell(str(info.idx), "bar", b)
            w.update_cell(str(info.idx), "desc", Text(info.desc, style=style))

        ft = self.query_one("#files", DataTable)
        for f in j.files:
            key = str(f.id)
            sig = (f.state, int(f.progress * 1000), f.bytes_same, f.bytes_done)
            cells = (Text(engine.F_NAMES[f.state], style=FILE_STYLE.get(f.state, "")),
                     meter(f.progress, 12).append(f" {f.progress * 100:3.0f}%"),
                     fmt_bytes(f.bytes_same), fmt_bytes(f.bytes_done))
            if key not in self._rows:
                ft.add_row(f.rel, fmt_bytes(f.size), *cells, key=key)
            elif self._rows[key] != sig:
                for col, val in zip(("state", "prog", "same", "sent"), cells):
                    ft.update_cell(key, col, val)
            self._rows[key] = sig

        f = self.file
        if self.follow:
            with j.lock:
                act = [x for x in j.active if x.state == engine.F_WORKING]
            f = act[0] if act else (f or (j.files[-1] if j.files else None))
            self.file = f
        if f is None:
            self.query_one("#maphead", Static).update("")
            self.query_one("#map", Static).update("")
            return
        with j.lock:
            blocks, prog = bytes(f.blocks), dict(f.block_progress)
        self.query_one("#maphead", Static).update(Text(
            f"{f.rel} — {f.nblocks} blocks × {fmt_bytes(f.bs)} — {engine.F_NAMES[f.state]}"
            f"{'   (following active file; f)' if self.follow else '   (f: follow active)'}",
            style="bold"))
        width = max(10, self.size.width - 2)
        self.query_one("#map", Static).update(block_map(blocks, prog, f.bs, f.size, width))


# -- app ---------------------------------------------------------------------------

class Commander(App):
    TITLE = "rsync-commander"
    CSS = """
    Screen { layout: vertical; }
    #panels { height: 1fr; }
    .panel { width: 1fr; border: round $panel-lighten-2; }
    .panel.active { border: round $accent; }
    .pathbar { height: 1; background: $boost; }
    .status { height: 1; color: $text-muted; }
    .panel DataTable { height: 1fr; }
    #strip { height: 3; background: $boost; }
    #striptext { width: 1fr; padding: 0 1; }
    #stripgraph { width: 42; }
    .dialog { width: 70; height: auto; padding: 1 2; border: thick $accent; background: $surface; }
    .dialog.wide { width: 110; }
    ModalScreen { align: center middle; }
    .buttons { height: auto; align-horizontal: right; margin-top: 1; }
    .buttons Button { margin-left: 1; }
    .row { height: auto; }
    .row Label { padding-top: 1; }
    .num { width: 8; }
    #comp { width: 14; }
    #hosts { height: auto; max-height: 15; }
    TransfersScreen #top { height: auto; }
    TransfersScreen #summary { width: 1fr; height: auto; padding: 0 1; }
    TransfersScreen #speedgraph { width: 48; height: 6; padding: 0 1; border-left: vkey $panel-lighten-2; }
    TransfersScreen #workers { height: auto; max-height: 10; }
    TransfersScreen #files { height: 1fr; }
    TransfersScreen #maphead { height: 1; padding: 0 1; }
    TransfersScreen #map { height: auto; max-height: 12; padding: 0 1; }
    TransfersScreen #legend { height: 1; padding: 0 1; }
    """
    BINDINGS = [
        Binding("tab", "switch", "Switch", priority=True),
        Binding("f2", "host", "Host"),
        Binding("f5", "copy", "Copy"),
        Binding("f6", "rename", "Rename"),
        Binding("f7", "mkdir", "MkDir"),
        Binding("f8,delete", "delete", "Delete"),
        Binding("f9", "transfers", "Transfers"),
        Binding("f10,ctrl+q", "quit", "Quit"),
        Binding("space,insert", "mark", "Mark", show=False),
        Binding("backspace", "up", "Up", show=False),
        Binding("ctrl+r", "refresh", "Refresh", show=False),
        Binding("ctrl+u", "swap", "Swap", show=False),
        Binding("ctrl+l", "goto", "Go to path", show=False),
    ]

    def __init__(self, left=None, right=None):
        super().__init__()
        self.endpoints = {LOCAL: LocalEndpoint()}
        self.hosts = ssh_config_hosts()
        self.opts = engine.Options()
        self.queue = JobQueue(lambda j: self.call_from_thread(self._job_done, j))
        self._init = [self._parse(left) if left else (LOCAL, os.getcwd()),
                      self._parse(right) if right else (LOCAL, str(os.path.expanduser("~")))]
        self.active = None

    @staticmethod
    def _parse(spec):
        if ":" in spec and not spec.startswith(("/", ".", "~")):
            host, path = spec.split(":", 1)
            return host, path or None
        return LOCAL, os.path.abspath(os.path.expanduser(spec))

    def endpoint(self, key):
        if key not in self.endpoints:
            self.endpoints[key] = RemoteEndpoint(key)
        return self.endpoints[key]

    def compose(self) -> ComposeResult:
        with Horizontal(id="panels"):
            yield Panel("left", self, *self._init[0])
            yield Panel("right", self, *self._init[1])
        with Horizontal(id="strip"):
            yield Static(id="striptext")
            yield Static(id="stripgraph")
        yield Footer()

    def on_mount(self):
        self.left = self.query_one("#left", Panel)
        self.right = self.query_one("#right", Panel)
        self._activate(self.left)
        self.set_interval(0.5, self._strip)

    # panels ---------------------------------------------------------------
    def _activate(self, p):
        self.active = p
        for q in (self.left, self.right):
            q.set_class(q is p, "active")
        p.table.focus()

    def other(self, p=None):
        p = p or self.active
        return self.right if p is self.left else self.left

    @on(DataTable.RowSelected)
    def _row_selected(self, ev):
        for p in (self.left, self.right):
            if ev.data_table is p.table:
                self._activate(p)
                p.open_current()

    def on_descendant_focus(self, ev):
        for p in (self.left, self.right):
            if ev.widget is p.table and self.active is not p:
                self._activate(p)

    def action_switch(self):
        if isinstance(self.screen, (ModalScreen, TransfersScreen)):
            self.screen.focus_next()
            return
        self._activate(self.other())

    def action_mark(self):
        self.active.toggle_mark()

    def action_up(self):
        self.active.go_up()

    def action_refresh(self):
        self.active.refresh_listing()

    def action_swap(self):
        l, r = (self.left.ep_key, self.left.cwd), (self.right.ep_key, self.right.cwd)
        self.left.set_endpoint(*r)
        self.right.set_endpoint(*l)

    def action_host(self):
        p = self.active
        self.push_screen(HostPicker(self.hosts),
                         lambda key: key and p.set_endpoint(LOCAL if key.lower() == "local" else key))

    def action_goto(self):
        p = self.active
        self.push_screen(Ask("Go to path:", p.cwd or "/"), lambda v: v and p.navigate(v))

    # file ops -------------------------------------------------------------
    def _op(self, panel, label, fn):
        ep = panel.endpoint
        panel.status(f"{label}…")

        def work():
            try:
                browse_call(ep, fn)
                self.call_from_thread(panel.refresh_listing)
            except Exception as ex:
                self.call_from_thread(self.notify, f"{label} failed: {ex}", severity="error")
                self.call_from_thread(panel.refresh_listing)
        threading.Thread(target=work, daemon=True).start()

    def action_mkdir(self):
        p = self.active
        self.push_screen(Ask(f"New directory in {p.cwd}:"),
                         lambda v: v and v.strip() and self._op(
                             p, "mkdir", lambda s: s.mkdir(p.join(v.strip()))))

    def action_rename(self):
        p = self.active
        e = p.current()
        if not e or e["type"] == "up":
            return
        old = p.join(e["name"])

        def done(v):
            if v and v.strip() and v.strip() != e["name"]:
                new = posixpath.join(p.cwd, v.strip())
                self._op(p, "rename", lambda s: s.rename(old, new))
        self.push_screen(Ask(f"Rename {e['name']} to:", e["name"]), done)

    def action_delete(self):
        p = self.active
        paths = p.selected_paths()
        if not paths:
            return
        names = "\n".join("  " + posixpath.basename(x) for x in paths[:10])
        if len(paths) > 10:
            names += f"\n  … and {len(paths) - 10} more"

        def done(yes):
            if yes:
                def fn(s):
                    for x in paths:
                        s.delete(x)
                self._op(p, f"delete {len(paths)} item(s)", fn)
        self.push_screen(Confirm(f"Permanently delete {len(paths)} item(s) on "
                                 f"{p.ep_key}:{p.cwd}?\n\n{names}"), done)

    def action_copy(self):
        src, dst = self.active, self.other()
        paths = src.selected_paths()
        if not paths or not dst.cwd:
            return
        what = (f"Copy {len(paths)} item(s) from {src.ep_key}:{src.cwd}\n"
                f"into {dst.ep_key}:")

        def done(res):
            if not res:
                return
            target, opts = res
            self.opts = opts
            job = engine.Job(self.endpoint(src.ep_key), paths, self.endpoint(dst.ep_key),
                             target or dst.cwd, opts)
            job.dst_key = dst.ep_key
            self.queue.add(job)
            src.marked.clear()
            src.refresh_listing()
            self.notify(f"queued: {job.title}")
        self.push_screen(CopyDialog(what, dst.cwd, self.opts), done)

    def action_transfers(self):
        if isinstance(self.screen, TransfersScreen):
            self.pop_screen()
        else:
            self.push_screen(TransfersScreen(self.queue))

    def _job_done(self, job):
        s = job.snapshot()
        sev = "information" if s.state == engine.J_DONE else "error"
        self.notify(f"{engine.J_NAMES[s.state]}: {job.title}", severity=sev)
        for p in (self.left, self.right):
            if p.ep_key == getattr(job, "dst_key", None) and p.cwd and (
                    p.cwd == job.dst_dir or p.cwd.startswith(job.dst_dir.rstrip("/") + "/")):
                p.refresh_listing()

    def _strip(self):
        strip = self.query_one("#striptext", Static)
        sg = self.query_one("#stripgraph", Static)
        j = self.queue.running or (self.queue.jobs[-1] if self.queue.jobs else None)
        if j is None:
            strip.update(Text("No transfers. Mark files (Space), F5 to copy, F9 for details.",
                              style="grey50"))
            sg.update("")
            return
        s = j.snapshot()
        frac = (s.bytes_same + s.bytes_done) / s.bytes_total if s.bytes_total else 0
        queued = sum(1 for x in self.queue.jobs if x.t_start is None and not x.cancelled)
        t = Text()
        t.append(s.title[:100] + (f"   (+{queued} queued)" if queued else "") + "\n")
        t.append_text(meter(frac, 40))
        t.append(f" {frac * 100:5.1f}%  {engine.J_NAMES[s.state]}  "
                 f"{fmt_bytes(s.rate)}/s  ETA {'≥' if s.eta_lower_bound else ''}{fmt_dur(s.eta)}"
                 f"  files {s.files_done + s.files_skipped}/{s.files_total}  F9: details")
        strip.update(t)
        sample_rate(j, s)
        sg.update(graph(rate_history(j), max(10, sg.size.width), 3))

    async def action_quit(self):
        running = [j for j in self.queue.jobs if j.t_start and j.state in
                   (engine.J_RUNNING, engine.J_PAUSED, engine.J_PLANNING)]
        if not running:
            self.exit()
            return

        def done(yes):
            if yes:
                for j in running:
                    j.cancel()
                self.exit()
        self.push_screen(Confirm("A transfer is running. Cancel it and quit?\n"
                                 "(Partial files are kept; rerun to resume.)"), done)

    def on_unmount(self):
        for ep in self.endpoints.values():
            ep.close()


def run(left=None, right=None):
    Commander(left, right).run()
