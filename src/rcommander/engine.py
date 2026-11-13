"""Block-level sync engine.

A Job copies one or more source items (files or directory trees) into a
destination directory. Each file is split into fixed-size blocks. When the
destination already has a basis for a file (an existing final file, or a
.rcpart left by an interrupted run), both sides hash it per block and only
mismatched blocks are sent. Every block sent is SHA-1 verified on read and
again on the receiving side before it is written.

N worker threads each own their own session per endpoint, so remote traffic
runs over N parallel ssh/TCP streams. A scheduler under one lock hands out
work items: prepare, hash, clone, block, finalize. Files are worked roughly in
order, with workers spread across the blocks of the same few files.

Destination writes go to `.<name>.rcpart` and are renamed into place only
when the file is complete, so an interrupted run leaves no half-written final
files and resumes from the partial.
"""

import collections
import itertools
import posixpath
import threading
import time
from dataclasses import dataclass, field

from .endpoint import ConnectionLost, RemoteError, peer_argv, same_box
from .fmt import fmt_bytes as fmt_size

# Block states (stored in a bytearray per file for cheap painting).
B_UNKNOWN, B_PENDING, B_HASHING, B_SAME, B_ACTIVE, B_DONE, B_FAILED = range(7)
B_NAMES = ["unknown", "pending", "hashing", "same", "active", "done", "failed"]

# File states.
F_QUEUED, F_PREPARING, F_WORKING, F_FINALIZING, F_DONE, F_SKIPPED, F_FAILED = range(7)
F_NAMES = ["queued", "preparing", "working", "finalizing", "done", "up to date", "failed"]
F_TERMINAL = (F_DONE, F_SKIPPED, F_FAILED)

# Job states.
J_PLANNING, J_RUNNING, J_PAUSED, J_DONE, J_FAILED, J_CANCELLED = range(6)
J_NAMES = ["planning", "running", "paused", "done", "done with errors", "cancelled"]


COMPRESSIONS = ("none", "gzip", "zstd")
DEFAULT_LEVEL = {"gzip": 1, "zstd": 3}
CODEC_OF = {"none": None, "gzip": "zlib", "zstd": "zstd"}


@dataclass
class Source:
    """One thing to copy. `to` is its path under the destination directory
    (default: its own basename); `filter` is a filters.Filter."""
    path: str
    to: str = None
    filter: object = None
    follow_symlinks: bool = False

    @property
    def name(self):
        return posixpath.basename(self.path.rstrip("/")) or "/"


@dataclass
class Options:
    block_size: int = 4 << 20
    workers: int = 4
    checksum: bool = True      # False = skip files whose size+mtime match
    compression: str = "zstd"  # none | gzip | zstd  (wire only; remote legs)
    level: int = 0             # 0 = codec default (gzip 1, zstd 3)
    hash_batch: int = 16       # blocks per hash request
    max_retries: int = 6
    # Files up to small_file_max (and one block) go in batches: one round trip
    # per batch per step instead of 3-4 per file.
    small_file_max: int = 1 << 20
    batch_files: int = 64
    batch_bytes: int = 8 << 20
    # NAS-to-NAS: same box -> copy on the box itself; different boxes -> the
    # source box pushes straight to the destination (needs its own ssh key for
    # it, or forward_agent). Either falls back to relaying through this machine.
    server_side: bool = True
    direct: bool = True
    forward_agent: bool = False


class FileTask:
    _ids = itertools.count()

    def __init__(self, rel, src_path, dst_path, size, mtime_ns, mode, bs):
        self.id = next(self._ids)
        self.rel = rel
        self.src_path = src_path
        self.dst_path = dst_path
        self.size = size
        self.mtime_ns = mtime_ns
        self.mode = mode
        self.bs = bs
        self.nblocks = (size + bs - 1) // bs
        self.blocks = bytearray(self.nblocks)          # B_* per block
        self.block_progress = {}                       # idx -> bytes moved (active)
        self.state = F_QUEUED
        self.error = None
        self.basis = None
        self.basis_size = 0
        self.partial = None
        self.need_clone = False
        self.cloned = False
        self.cloning = False
        self.hash_queue = collections.deque()          # (side, start, count)
        self.hash_results = {}                         # start -> {side: hashes}
        self.pending = collections.deque()             # block indices to send
        self.remaining = self.nblocks                  # blocks not SAME/DONE
        self.inflight = 0
        self.bytes_needed = 0                          # bytes that must be sent
        self.bytes_done = 0                            # bytes sent (incl. partial)
        self.bytes_same = 0
        self.hash_total = 0                            # compare bytes (one side)
        self.hash_done = 0                             # hashed bytes, both sides /2
        self.started_at = None
        self.finished_at = None

    def blen(self, i):
        return min(self.bs, self.size - i * self.bs)

    @property
    def progress(self):
        if self.state in (F_DONE, F_SKIPPED):
            return 1.0
        if self.size == 0:
            return 0.0
        moving = sum(self.block_progress.values())
        return min(1.0, (self.bytes_same + self.bytes_done + moving) / self.size)


@dataclass
class Work:
    kind: str                 # prepare | hash | clone | block | finalize | batch
    f: FileTask
    side: str = ""
    start: int = 0
    count: int = 0
    tries: int = 0
    files: list = None        # batch: the small FileTasks
    moved: float = 0.0        # batch: bytes moved so far (in flight)


WORKER_HIST = 120      # samples of per-stream throughput kept for sparklines
WORKER_SAMPLE = 0.5    # seconds between samples


@dataclass
class WorkerInfo:
    idx: int
    desc: str = "idle"
    file_id: int = -1
    block: int = -1
    moved: int = 0
    total: int = 0
    state: str = "idle"       # idle | busy | reconnecting | stopped
    t0: float = 0.0
    label: str = ""           # progress-bar label override (e.g. "batch of 64")
    bytes: float = 0.0        # cumulative logical bytes this stream has moved
    rate: float = 0.0         # bytes/s over the last few seconds (set by snapshot)
    hist: list = field(default_factory=list)   # recent rates, oldest first


@dataclass
class Snapshot:
    state: int
    title: str
    files_total: int
    files_done: int
    files_skipped: int
    files_failed: int
    bytes_total: int
    bytes_needed: int
    bytes_done: int
    bytes_same: int
    hash_total: int
    hash_done: int
    wire_bytes: int
    rate: float               # logical bytes/s, recent window
    wire_rate: float
    elapsed: float
    eta: float                # seconds, or -1 if unknown
    eta_lower_bound: bool     # hashing still running: more may be needed
    compression: str = "none"     # effective codec for this job
    compressed_logical: int = 0   # logical bytes of blocks that crossed a network
    compressed_wire: int = 0      # their size on the wire
    errors: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    workers: list = field(default_factory=list)


class Job:
    _ids = itertools.count(1)

    def __init__(self, src_ep, src_paths, dst_ep, dst_dir, opts=None, title=None, dry_run=False):
        self.id = next(self._ids)
        self.src_ep, self.dst_ep = src_ep, dst_ep
        self.sources = [s if isinstance(s, Source) else Source(s) for s in src_paths]
        self.src_paths = [s.path for s in self.sources]
        self.dst_dir = dst_dir
        self.opts = opts or Options()
        names = ", ".join(posixpath.basename(p.rstrip("/")) for p in self.src_paths[:3])
        if len(self.src_paths) > 3:
            names += f" +{len(self.src_paths) - 3}"
        self.title = title or f"{names}  →  {dst_ep.label(dst_dir)}"
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.state = J_PLANNING
        self.files = []
        self.by_id = {}
        self.active = []
        self.next_idx = 0
        self.errors = []          # (rel, message)
        self.log = []
        self.cancelled = False
        self.paused = False
        self.t_start = None
        self.t_end = None
        self.bytes_done = 0
        self.wire_bytes = 0
        self.net_logical = 0      # logical bytes that crossed a network leg
        self.hash_done = 0
        self.compression = "none"
        self.codec = None
        self.level = 1
        self.mode = "relay"       # relay | server (same box) | direct (box to box)
        self.peer = None          # direct: argv the source box runs to reach the peer
        self._samples = collections.deque(maxlen=64)   # (t, logical, wire)
        self._wsamples = collections.deque(maxlen=64)  # (t, [bytes per worker])
        self._whist = []                                # per worker: deque of rates
        self._whist_t = 0.0
        self.workers = []
        self.winfo = []
        self._threads = []
        self.on_finished = None   # callback(job) from the last worker thread
        self.dry_run = dry_run    # plan only: walk + filter, transfer nothing
        self.batch_retry = collections.deque()   # batches whose connection dropped
        self.batches_inflight = 0
        self.batch_moved = 0.0    # in-flight bytes of running batches
        self.filtered_out = 0

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        t = threading.Thread(target=self._run, name=f"job{self.id}", daemon=True)
        t.start()
        self._threads.append(t)

    def wait(self, timeout=None):
        end = None if timeout is None else time.time() + timeout
        for t in list(self._threads):
            t.join(None if end is None else max(0, end - time.time()))

    def cancel(self):
        with self.lock:
            self.cancelled = True
            self.cv.notify_all()
        for w in self.workers:
            w.abort()

    def set_paused(self, paused):
        with self.lock:
            self.paused = paused
            if self.state in (J_RUNNING, J_PAUSED):
                self.state = J_PAUSED if paused else J_RUNNING
            self.cv.notify_all()

    def _run(self):
        self.t_start = time.time()
        try:
            self._plan()
        except Exception as ex:  # planning failed outright
            with self.lock:
                self.errors.append(("(plan)", str(ex)))
                self.state = J_FAILED
                self.t_end = time.time()
            if self.on_finished:
                self.on_finished(self)
            return
        if self.dry_run:
            with self.lock:
                self.t_end = time.time()
                self.state = J_FAILED if self.errors else J_DONE
            if self.on_finished:
                self.on_finished(self)
            return
        with self.lock:
            if self.cancelled:
                self.state = J_CANCELLED
            else:
                self.state = J_PAUSED if self.paused else J_RUNNING
        n = max(1, self.opts.workers)
        self.winfo = [WorkerInfo(i) for i in range(n)]
        self._whist = [collections.deque(maxlen=WORKER_HIST) for _ in range(n)]
        self.workers = [Worker(self, self.winfo[i]) for i in range(n)]
        threads = [threading.Thread(target=w.run, name=f"job{self.id}-w{w.info.idx}",
                                    daemon=True) for w in self.workers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if self.mode == "direct":
            self.src_ep.forward_agent = False
        with self.lock:
            self.t_end = time.time()
            if self.cancelled:
                self.state = J_CANCELLED
            elif any(f.state == F_FAILED for f in self.files) or self.errors:
                self.state = J_FAILED
            else:
                self.state = J_DONE
        if self.on_finished:
            self.on_finished(self)

    # -- planning ------------------------------------------------------------
    def _plan(self):
        s_src = self.src_ep.session()
        s_dst = self.dst_ep.session()
        try:
            self._resolve_codec(s_src, s_dst)
            self._choose_mode()
            dirs = []
            for src in self.sources:
                sp = src.path.rstrip("/") or "/"
                base = posixpath.dirname(sp)
                req = {"op": "walk", "path": sp}
                if src.follow_symlinks:
                    req["follow_symlinks"] = True
                w = s_src.call(req)[0]
                if not w.get("exists"):
                    self.errors.append((sp, "source does not exist"))
                    continue
                entries = w["entries"]
                top = entries[0]["rel"] if entries else src.name
                if src.filter:
                    before = len(entries)
                    entries = src.filter.apply(entries, prefix=top)
                    self.filtered_out += before - len(entries)
                to = (src.to or top).strip("/")
                for e in entries:
                    if self.cancelled:
                        return
                    # rel under the source's own name -> rel under `to`
                    rel = e["rel"]
                    out_rel = to + rel[len(top):] if rel.startswith(top) else rel
                    if e["type"] == "d":
                        dirs.append(posixpath.join(self.dst_dir, out_rel))
                    elif e["type"] == "f":
                        ft = FileTask(out_rel, posixpath.join(base, rel),
                                      posixpath.join(self.dst_dir, out_rel),
                                      e["size"], e["mtime_ns"], e["mode"],
                                      self.opts.block_size)
                        self.files.append(ft)
                        self.by_id[ft.id] = ft
                    elif e["type"] == "l":
                        self.log.append(f"skipped symlink {rel}")
                    elif e["type"] == "err":
                        self.errors.append((rel, e.get("error", "unreadable")))
            if self.dry_run:
                return
            if dirs:
                for i in range(0, len(dirs), 500):
                    s_dst.mkdirs(dirs[i:i + 500])
        finally:
            s_src.close()
            s_dst.close()

    def _choose_mode(self):
        src, dst = self.src_ep, self.dst_ep
        if self.opts.server_side and same_box(src, dst):
            self.mode = "server"
            self.log.append(f"same box ({src.host}): copying on the box itself")
            return
        remote = lambda e: getattr(e, "is_local", True) is False   # noqa: E731
        if not (self.opts.direct and remote(src) and remote(dst)):
            return
        argv, why = peer_argv(dst)
        if argv is None:
            self.log.append(f"box-to-box direct unavailable: {why}; relaying through here")
            return
        src.forward_agent = self.opts.forward_agent
        probe = src.session()
        try:
            info = probe.peer_open(argv)
            self.mode, self.peer = "direct", argv
            self.log.append(f"direct box-to-box: {src.host} → {dst.host} "
                            f"(peer python {info.get('python')}"
                            f"{', agent forwarded' if self.opts.forward_agent else ''})")
        except (RemoteError, ConnectionLost) as ex:
            src.forward_agent = False
            hint = ("" if self.opts.forward_agent else
                    f"; if {src.host} has no ssh key for {dst.host}, enable agent forwarding")
            self.log.append(f"box-to-box direct failed ({ex}): relaying through here{hint}")
        finally:
            probe.close()

    @property
    def codec_name(self):
        return CODEC_OF.get(self.opts.compression)

    def _resolve_codec(self, s_src, s_dst):
        """Use the requested codec if every network leg can decode it; else
        fall back to gzip (zlib is in every Python's stdlib)."""
        want = self.opts.compression if self.opts.compression in COMPRESSIONS else "none"
        legs = [s for s in (s_src, s_dst) if s.wire_compress]
        if want == "none" or not legs:
            self.compression, self.codec = "none", None
            return
        codec = CODEC_OF[want]
        from . import agent
        missing = [s for s in legs if codec not in s.codecs or codec not in agent.CODECS]
        if missing:
            where = ", ".join(getattr(getattr(s, "ep", None), "host", "local") for s in missing)
            self.log.append(f"{want} not available on {where}; using gzip instead")
            want, codec = "gzip", "zlib"
        self.compression, self.codec = want, codec
        self.level = self.opts.level or DEFAULT_LEVEL[want]

    # -- scheduler (all under self.lock) -------------------------------------
    def next_work(self, info):
        with self.lock:
            while True:
                if self.cancelled:
                    return None
                if not self.paused:
                    w = self._pick()
                    if w is not None:
                        return w
                    if self._all_done():
                        self.cv.notify_all()
                        return None
                info.state, info.desc = "idle", "waiting"
                self.cv.wait(0.5)

    def _all_done(self):
        return (self.next_idx >= len(self.files) and not self.active
                and not self.batches_inflight and not self.batch_retry)

    def _is_small(self, f):
        return f.size <= min(self.opts.small_file_max, self.opts.block_size)

    def _pick(self):
        if self.batch_retry:
            self.batches_inflight += 1
            return self.batch_retry.popleft()
        if self.mode == "server":
            if self.next_idx < len(self.files):
                return self._gather_batch(kind="server", max_bytes=1 << 30,
                                          small_only=False)
            return None
        for f in self.active:
            w = self._pick_in(f)
            if w is not None:
                return w
        if self.next_idx < len(self.files) and self._is_small(self.files[self.next_idx]):
            return self._gather_batch()
        max_open = self.opts.workers + 2
        if self.next_idx < len(self.files) and len(self.active) < max_open:
            f = self.files[self.next_idx]
            self.next_idx += 1
            self.active.append(f)
            f.state = F_PREPARING
            f.started_at = time.time()
            f.inflight += 1
            return Work("prepare", f)
        return None

    def _gather_batch(self, kind="batch", max_bytes=None, small_only=True):
        """Consecutive (small) files from the queue, up to the batch limits."""
        batch, total = [], 0
        now = time.time()
        max_bytes = max_bytes or self.opts.batch_bytes
        while self.next_idx < len(self.files) and len(batch) < self.opts.batch_files:
            f = self.files[self.next_idx]
            if (small_only and not self._is_small(f)) or (batch and total + f.size > max_bytes):
                break
            self.next_idx += 1
            batch.append(f)
            total += f.size
            f.state = F_PREPARING
            f.started_at = now
            f.bytes_needed = f.size            # assume it's sent until probed
        self.batches_inflight += 1
        return Work(kind, batch[0], files=batch, count=total)

    def _pick_in(self, f):
        if f.state == F_QUEUED:  # prepare being retried after a lost connection
            f.state = F_PREPARING
            f.inflight += 1
            return Work("prepare", f)
        if f.state != F_WORKING:
            return None
        if f.hash_queue:
            side, start, count = f.hash_queue.popleft()
            for i in range(start, start + count):
                if f.blocks[i] == B_UNKNOWN:
                    f.blocks[i] = B_HASHING
            f.inflight += 1
            return Work("hash", f, side=side, start=start, count=count)
        if f.need_clone and not f.cloned:
            if not f.cloning:
                f.cloning = True
                f.inflight += 1
                return Work("clone", f)
            return None
        if f.pending:
            i = f.pending.popleft()
            f.blocks[i] = B_ACTIVE
            f.block_progress[i] = 0
            f.inflight += 1
            return Work("block", f, start=i)
        if f.remaining == 0 and f.inflight == 0:
            f.state = F_FINALIZING
            f.inflight += 1
            return Work("finalize", f)
        return None

    def _retire(self, f, state, error=None):
        f.state = state
        f.error = error
        f.finished_at = time.time()
        if error:
            self.errors.append((f.rel, error))
        if f in self.active:
            self.active.remove(f)
        self.cv.notify_all()

    # -- batch results --------------------------------------------------------
    def batch_progress(self, w, n):
        with self.lock:
            w.moved += n
            self.batch_moved += n

    def batch_sending(self, files):
        with self.lock:
            for f in files:
                f.state = F_WORKING
                for i in range(f.nblocks):
                    f.blocks[i] = B_ACTIVE

    def done_batch(self, w, same, sent, failed, wire, legs):
        """same: identical files; sent: files written; failed: [(f, msg)]."""
        with self.lock:
            self.batches_inflight -= 1
            self.batch_moved = max(0.0, self.batch_moved - w.moved)
            w.moved = 0.0
            self.wire_bytes += wire
            for f in same:
                f.blocks[:] = bytes([B_SAME]) * f.nblocks
                f.bytes_needed, f.bytes_same, f.remaining = 0, f.size, 0
                self._retire(f, F_SKIPPED)
            for f in sent:
                f.blocks[:] = bytes([B_DONE]) * f.nblocks
                f.bytes_done, f.remaining = f.size, 0
                self.bytes_done += f.size
                self.net_logical += f.size * legs
                self._retire(f, F_DONE)
            for f, msg in failed:
                f.blocks[:] = bytes([B_FAILED]) * f.nblocks
                self._retire(f, F_FAILED, msg)
            self.cv.notify_all()

    def requeue_batch(self, w):
        with self.lock:
            self.batches_inflight -= 1
            self.batch_moved = max(0.0, self.batch_moved - w.moved)
            w.moved = 0.0
            for f in w.files:
                f.state = F_PREPARING
                f.blocks[:] = bytes(f.nblocks)
            self.batch_retry.append(w)
            self.cv.notify_all()

    def fail_batch(self, w, msg):
        self.done_batch(w, [], [], [(f, f"batch: {msg}") for f in w.files], 0, 0)

    # -- results (called by workers) -----------------------------------------
    def done_prepare(self, w, resp):
        f = w.f
        with self.lock:
            f.inflight -= 1
            f.basis = resp.get("basis")
            f.basis_size = resp.get("basis_size", 0)
            f.partial = resp["partial"]
            if (f.basis == "final" and not self.opts.checksum
                    and f.basis_size == f.size
                    and resp.get("basis_mtime_ns", 0) // 10**9 == f.mtime_ns // 10**9):
                for i in range(f.nblocks):
                    f.blocks[i] = B_SAME
                f.bytes_same = f.size
                f.remaining = 0
                self._retire(f, F_SKIPPED)
                return
            cmp_bytes = min(f.size, f.basis_size) if f.basis else 0
            ncmp = (cmp_bytes + f.bs - 1) // f.bs
            f.hash_total = cmp_bytes
            for s in range(0, ncmp, self.opts.hash_batch):
                c = min(self.opts.hash_batch, ncmp - s)
                f.hash_queue.append(("src", s, c))
                f.hash_queue.append(("dst", s, c))
                f.hash_results[s] = {}
            for i in range(ncmp, f.nblocks):
                f.blocks[i] = B_PENDING
                f.pending.append(i)
                f.bytes_needed += f.blen(i)
            if f.basis == "final" and f.basis_size != f.size:
                f.need_clone = True
            f.state = F_WORKING
            self.cv.notify_all()

    def done_hash(self, w, hashes):
        f = w.f
        with self.lock:
            f.inflight -= 1
            r = f.hash_results[w.start]
            r[w.side] = hashes
            nbytes = sum(f.blen(i) for i in range(w.start, w.start + w.count))
            f.hash_done += nbytes / 2
            self.hash_done += nbytes / 2
            if "src" in r and "dst" in r:
                del f.hash_results[w.start]
                for k in range(w.count):
                    i = w.start + k
                    a, b = r["src"][k], r["dst"][k]
                    if a is not None and a == b:
                        f.blocks[i] = B_SAME
                        f.bytes_same += f.blen(i)
                        f.remaining -= 1
                    else:
                        f.blocks[i] = B_PENDING
                        f.pending.append(i)
                        f.bytes_needed += f.blen(i)
                        if f.basis == "final":
                            f.need_clone = True
            self.cv.notify_all()

    def done_clone(self, w):
        with self.lock:
            w.f.inflight -= 1
            w.f.cloned = True
            w.f.cloning = False
            self.cv.notify_all()

    def block_progress(self, w, n):
        with self.lock:
            w.f.block_progress[w.start] = w.f.block_progress.get(w.start, 0) + n

    def done_block(self, w, wire, net_logical=0):
        f, i = w.f, w.start
        with self.lock:
            self.net_logical += net_logical
            f.inflight -= 1
            f.block_progress.pop(i, None)
            f.blocks[i] = B_DONE
            f.remaining -= 1
            n = f.blen(i)
            f.bytes_done += n
            self.bytes_done += n
            self.wire_bytes += wire
            self.cv.notify_all()

    def done_finalize(self, w):
        with self.lock:
            w.f.inflight -= 1
            f = w.f
            unchanged = f.basis == "final" and not f.cloned and f.bytes_done == 0
            self._retire(f, F_SKIPPED if unchanged else F_DONE)

    def failed(self, w, msg):
        """A non-retryable error: fail the file."""
        f = w.f
        with self.lock:
            f.inflight -= 1
            if w.kind == "block":
                f.blocks[w.start] = B_FAILED
                f.block_progress.pop(w.start, None)
            if f.state not in F_TERMINAL:
                f.pending.clear()
                f.hash_queue.clear()
                self._retire(f, F_FAILED, f"{w.kind}: {msg}")

    def requeue(self, w):
        """Connection dropped mid-op: put the work back for a retry."""
        f = w.f
        with self.lock:
            f.inflight -= 1
            if f.state in F_TERMINAL:
                return
            if w.kind == "prepare":
                f.state = F_QUEUED
            elif w.kind == "hash":
                f.hash_queue.appendleft((w.side, w.start, w.count))
            elif w.kind == "clone":
                f.cloning = False
            elif w.kind == "block":
                f.block_progress.pop(w.start, None)
                f.blocks[w.start] = B_PENDING
                f.pending.appendleft(w.start)
            elif w.kind == "finalize":
                f.state = F_WORKING
            self.cv.notify_all()

    # -- stats ---------------------------------------------------------------
    def _worker_stats(self, now):
        """Per-stream throughput: rate over the last ~3 s plus a short history,
        sampled at most every WORKER_SAMPLE seconds. Called under self.lock."""
        cur = [w.bytes for w in self.winfo]
        self._wsamples.append((now, cur))
        old = next((s for s in self._wsamples if now - s[0] <= 3.0), None)
        running = self.state in (J_RUNNING, J_PAUSED)
        rates = [0.0] * len(cur)
        if old and now - old[0] > 0.2 and len(old[1]) == len(cur):
            dt = now - old[0]
            rates = [max(0.0, (c - o) / dt) for c, o in zip(cur, old[1])]
        if running and now - self._whist_t >= WORKER_SAMPLE:
            self._whist_t = now
            for h, r in zip(self._whist, rates):
                h.append(r)
        out = []
        for i, w in enumerate(self.winfo):
            c = WorkerInfo(**{k: v for k, v in vars(w).items() if k != "hist"})
            c.rate = rates[i] if running else 0.0
            c.hist = list(self._whist[i]) if i < len(self._whist) else []
            out.append(c)
        return out

    def snapshot(self):
        now = time.time()
        with self.lock:
            inflight = (sum(sum(f.block_progress.values()) for f in self.active)
                        + self.batch_moved)
            done = self.bytes_done + inflight
            self._samples.append((now, done, self.wire_bytes))
            rate = wire_rate = 0.0
            # rate over the last ~10 s of samples
            old = None
            for s in self._samples:
                if now - s[0] <= 10.0:
                    old = s
                    break
            if old and now - old[0] > 0.5:
                rate = (done - old[1]) / (now - old[0])
                wire_rate = (self.wire_bytes - old[2]) / (now - old[0])
            if self.state in (J_DONE, J_FAILED, J_CANCELLED) and self.t_start:
                dur = max(0.001, (self.t_end or now) - self.t_start)
                rate, wire_rate = self.bytes_done / dur, self.wire_bytes / dur
            needed = sum(f.bytes_needed for f in self.files if f.state != F_SKIPPED)
            same = sum(f.bytes_same for f in self.files)
            total = sum(f.size for f in self.files)
            hash_total = sum(f.hash_total for f in self.files)
            unresolved = total - same - needed
            hashing = self.state in (J_PLANNING,) or unresolved > 0
            # Files not yet prepared are assumed to need everything.
            for f in self.files:
                if f.state == F_QUEUED:
                    needed += f.size
            left = max(0, needed - done)
            eta = 0 if left == 0 else (left / rate if rate > 1 else -1)
            if self.state in (J_DONE, J_FAILED, J_CANCELLED):
                eta = 0
            end = self.t_end or now
            counts = collections.Counter(f.state for f in self.files)
            return Snapshot(
                state=self.state, title=self.title,
                files_total=len(self.files), files_done=counts[F_DONE],
                files_skipped=counts[F_SKIPPED], files_failed=counts[F_FAILED],
                bytes_total=total, bytes_needed=needed, bytes_done=done,
                bytes_same=same, hash_total=hash_total, hash_done=int(self.hash_done),
                wire_bytes=self.wire_bytes, rate=rate, wire_rate=wire_rate,
                elapsed=end - (self.t_start or now), eta=eta,
                eta_lower_bound=hashing and self.state == J_RUNNING,
                compression=self.compression if self.state != J_PLANNING else "…",
                compressed_logical=self.net_logical, compressed_wire=self.wire_bytes,
                errors=list(self.errors[-50:]), notes=list(self.log[-20:]),
                workers=self._worker_stats(now),
            )


class Worker:
    def __init__(self, job, info):
        self.job = job
        self.info = info
        self.src = None
        self.dst = None
        self._sessions = []
        self._lock = threading.Lock()

    def _sess(self, which):
        ep = self.job.src_ep if which == "src" else self.job.dst_ep
        cur = getattr(self, which)
        if cur is None:
            cur = ep.session()
            with self._lock:
                self._sessions.append(cur)
            setattr(self, which, cur)
        return cur

    def _drop_sessions(self):
        with self._lock:
            for s in self._sessions:
                if not getattr(s, "shared", False):
                    s.close()
            self._sessions = []
        self.src = self.dst = None

    def abort(self):
        self._drop_sessions()

    def run(self):
        job, info = self.job, self.info
        try:
            while True:
                w = job.next_work(info)
                if w is None:
                    break
                self._do(w)
        finally:
            info.state, info.desc = "stopped", "stopped"
            self._drop_sessions()

    def _do_batch(self, w):
        """Many small files in a handful of round trips:
        probe dst (+ src hashes if needed) -> read_many -> put_many."""
        job, info, files = self.job, self.info, w.files
        info.block, info.total, info.moved = 0, w.count, 0
        info.label = f"batch of {len(files)}"
        info.desc = f"{len(files)} small files ({fmt_size(w.count)}) from {files[0].rel}"
        src, dst = self._sess("src"), self._sess("dst")
        checksum = job.opts.checksum
        probes = dst.probe_many([f.dst_path for f in files], hash=checksum)
        send, same, touch, need_hash = [], [], [], []
        for f, p in zip(files, probes):
            if p is None or p["size"] != f.size:
                send.append(f)
            elif not checksum:
                (same if p["mtime_ns"] // 10**9 == f.mtime_ns // 10**9 else send).append(f)
            else:
                need_hash.append((f, p))
        if need_hash:
            shs = src.probe_many([f.src_path for f, _ in need_hash], hash=True)
            for (f, p), sp in zip(need_hash, shs):
                if sp and sp.get("sha1") and sp["sha1"] == p.get("sha1"):
                    same.append(f)
                    if p["mtime_ns"] != f.mtime_ns:
                        touch.append((f.dst_path, f.mtime_ns))
                else:
                    send.append(f)
        src_local, dst_local = job.src_ep.is_local, job.dst_ep.is_local
        if job.mode == "direct":
            sent, failed = [], []
            if send or touch:
                job.batch_sending(send)
                r = src.push_many(job.peer, [{"src": f.src_path, "final": f.dst_path,
                                              "size": f.size, "mtime_ns": f.mtime_ns,
                                              "mode": f.mode} for f in send],
                                  job.codec_name, job.level, touch=touch)
                errs = iter(r["errors"])
                for f, rerr in zip(send, r["read_errors"]):
                    e = rerr or next(errs)
                    (failed.append((f, e)) if e else sent.append(f))
                info.bytes += sum(f.size for f in sent)
                job.done_batch(w, same, sent, failed, r["wire"], 1)
            else:
                job.done_batch(w, same, [], [], 0, 0)
            return

        def prog(k):
            info.moved += k
            info.bytes += k
            job.batch_progress(w, k)
        sent, failed, wire = [], [], 0
        if send:
            job.batch_sending(send)
            data, win = src.read_many([f.src_path for f in send], job.codec, job.level,
                                      progress=None if src_local else prog)
            ok, blobs, metas = [], [], []
            for f, (d, sha, err) in zip(send, data):
                if err or len(d) != f.size:
                    failed.append((f, err or "source changed size while reading"))
                    continue
                ok.append(f)
                blobs.append(d)
                metas.append({"final": f.dst_path, "len": len(d), "sha1": sha,
                              "mtime_ns": f.mtime_ns, "mode": f.mode})
            errs, terrs, wout = dst.put_many(
                metas, blobs, job.codec, job.level,
                progress=prog if src_local and not dst_local else None, touch=touch)
            for f, e in zip(ok, errs):
                (failed.append((f, e)) if e else sent.append(f))
            wire = (0 if src_local else win) + (0 if dst_local else wout)
            if src_local and dst_local:
                info.bytes += sum(f.size for f in sent)
        elif touch:
            dst.put_many([], [], touch=touch)
        legs = (0 if src_local else 1) + (0 if dst_local else 1)
        job.done_batch(w, same, sent, failed, wire, legs)

    def _do_server(self, w):
        """Same box: the box copies its own files (CoW clone where possible)."""
        job, info, files = self.job, self.info, w.files
        info.block, info.total, info.moved, info.label = 0, w.count, 0, f"on-box copy of {len(files)}"
        info.desc = f"copying {len(files)} file(s) on the box ({fmt_size(w.count)}) from {files[0].rel}"
        job.batch_sending(files)
        res = self._sess("dst").copy_many(
            [{"src": f.src_path, "dst": f.dst_path, "size": f.size, "mtime_ns": f.mtime_ns,
              "mode": f.mode} for f in files], job.opts.checksum)
        same, sent, failed = [], [], []
        for f, r in zip(files, res):
            if r == "same":
                same.append(f)
            elif r == "copied":
                sent.append(f)
            else:
                failed.append((f, r))
        job.done_batch(w, same, sent, failed, 0, 0)

    def _do(self, w):
        job, info = self.job, self.info
        info.state, info.file_id, info.block, info.moved, info.total = "busy", w.f.id, -1, 0, 0
        info.label = ""
        info.t0 = time.time()
        f = w.f
        try:
            if w.kind == "prepare":
                info.desc = f"prepare {f.rel}"
                job.done_prepare(w, self._sess("dst").prepare(f.dst_path))
            elif w.kind == "hash":
                info.desc = f"hash {w.side} {f.rel} [{w.start}-{w.start + w.count - 1}]"
                if w.side == "src":
                    path = f.src_path
                else:
                    path = f.partial if f.basis == "partial" else f.dst_path
                hs = self._sess(w.side).hash(path, f.bs, w.start, w.count, f.size)
                job.done_hash(w, hs)
            elif w.kind == "clone":
                info.desc = f"clone basis {f.rel}"
                self._sess("dst").clone(f.dst_path, f.partial)
                job.done_clone(w)
            elif w.kind == "block":
                i = w.start
                n = f.blen(i)
                info.block, info.total = i, n
                info.desc = f"{f.rel}  block {i + 1}/{f.nblocks}"
                src_local = job.src_ep.is_local
                dst_local = job.dst_ep.is_local
                if job.mode == "direct":
                    info.desc = f"{f.rel}  block {i + 1}/{f.nblocks}  (box to box)"
                    r = self._sess("src").push_block(job.peer, f.src_path, i * f.bs, n,
                                                     f.partial, job.codec_name, job.level)
                    if r["rawlen"] != n:
                        raise RemoteError(f"short read at block {i}: source changed?")
                    info.bytes += n
                    job.done_block(w, r["wire"], n)
                    return
                # Progress is counted once: on the remote leg of the transfer.
                def prog(k):
                    info.moved += k
                    info.bytes += k
                    job.block_progress(w, k)
                rprog = prog if not src_local else None
                wprog = prog if src_local and not dst_local else None
                data, sha, win = self._sess("src").read(
                    f.src_path, i * f.bs, n, job.codec, job.level, progress=rprog)
                if len(data) != n:
                    raise RemoteError(f"short read at block {i}: source changed?")
                wout = self._sess("dst").write(
                    f.partial, i * f.bs, data, sha, job.codec, job.level, progress=wprog)
                wire = (0 if src_local else win) + (0 if dst_local else wout)
                legs = (0 if src_local else 1) + (0 if dst_local else 1)
                if not rprog and not wprog:
                    info.bytes += n    # local->local: no in-flight progress, count on completion
                job.done_block(w, wire, n * legs)
            elif w.kind == "batch":
                self._do_batch(w)
            elif w.kind == "server":
                self._do_server(w)
            elif w.kind == "finalize":
                info.desc = f"finalize {f.rel}"
                d = self._sess("dst")
                if f.basis == "final" and not f.cloned:
                    d.touch_meta(f.dst_path, f.mtime_ns)
                else:
                    d.finalize(f.partial, f.dst_path, f.size, f.mtime_ns, f.mode)
                job.done_finalize(w)
        except ConnectionLost as ex:
            self._drop_sessions()
            w.tries += 1
            batch = w.kind in ("batch", "server")
            if job.cancelled:
                (job.requeue_batch if batch else job.requeue)(w)
                return
            if w.tries > job.opts.max_retries:
                msg = f"connection lost {w.tries} times: {ex}"
                job.fail_batch(w, msg) if batch else job.failed(w, msg)
                return
            (job.requeue_batch if batch else job.requeue)(w)
            info.state = "reconnecting"
            info.desc = f"reconnecting ({ex})"
            delay = min(30, 2 ** w.tries)
            end = time.time() + delay
            while time.time() < end and not job.cancelled:
                time.sleep(0.2)
        except RemoteError as ex:
            job.fail_batch(w, str(ex)) if w.files else job.failed(w, str(ex))
        except Exception as ex:  # bug or unexpected: fail the file(s), keep going
            msg = f"{type(ex).__name__}: {ex}"
            job.fail_batch(w, msg) if w.files else job.failed(w, msg)
        finally:
            info.state = "idle" if info.state == "busy" else info.state
