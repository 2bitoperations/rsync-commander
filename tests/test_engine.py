import os
import shlex
import subprocess
import hashlib
import shutil
import sys

import pytest

from rcommander import engine
from rcommander.endpoint import (AGENT_SRC, BOOTSTRAP, LocalEndpoint, RemoteEndpoint,
                                 SshSession)


def _py38():
    try:
        return subprocess.check_output(["uv", "python", "find", "3.8"],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


class FakeRemote(RemoteEndpoint):
    """Runs the agent over pipes exactly as over ssh, minus ssh. Run under old
    interpreters to prove the agent stays compatible with the boxes we target."""

    def __init__(self, python, host="fake"):
        super().__init__(host, python=python)

    def agent_argv(self):
        return ["sh", "-c",
                f"{shlex.quote(self.python)} -u -c {shlex.quote(BOOTSTRAP)} {len(AGENT_SRC)}"]


PY38 = _py38()
# Python 2.7 (old shared hosts). Set RC_PY27 to a python2.7/pypy2.7 binary to enable.
PY27 = os.environ.get("RC_PY27") or shutil.which("python2.7") or shutil.which("pypy")
PYCUR = sys.executable   # 3.14 here: has compression.zstd
REMOTES = {"fake38": PY38, "fake27": PY27, "fakecur": PYCUR}
ENDPOINTS = ["local"] + [k for k in ("fake38", "fake27") if REMOTES[k]]


def ep_for(kind):
    return LocalEndpoint() if kind == "local" else FakeRemote(REMOTES[kind])


def sha(path):
    return hashlib.sha1(open(path, "rb").read()).hexdigest()


def mkfile(path, size, seed=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    import random
    r = random.Random(seed)
    with open(path, "wb") as f:
        f.write(r.randbytes(size))


def run_job(src_ep, srcs, dst_ep, dst, **kw):
    opts = engine.Options(block_size=kw.pop("bs", 64 * 1024), workers=kw.pop("workers", 3), **kw)
    j = engine.Job(src_ep, srcs, dst_ep, dst, opts)
    j.start()
    j.wait(60)
    return j, j.snapshot()


@pytest.mark.parametrize("src_kind", ENDPOINTS)
@pytest.mark.parametrize("dst_kind", ENDPOINTS)
def test_copy_tree(tmp_path, src_kind, dst_kind):
    src = tmp_path / "src" / "M42"
    mkfile(str(src / "light_001.fits"), 1_000_003, 1)
    mkfile(str(src / "sub" / "dark_001.fits"), 200_000, 2)
    mkfile(str(src / "empty.txt"), 0, 3)
    os.makedirs(src / "emptydir")
    os.makedirs(src / "@eaDir")
    mkfile(str(src / "@eaDir" / "thumb.jpg"), 100, 4)
    os.utime(src / "light_001.fits", (1_600_000_000, 1_600_000_000))
    dst = tmp_path / "dst"
    dst.mkdir()
    j, snap = run_job(ep_for(src_kind), [str(src)], ep_for(dst_kind), str(dst))
    assert snap.state == engine.J_DONE, snap.errors
    for rel in ["light_001.fits", "sub/dark_001.fits", "empty.txt"]:
        assert sha(src / rel) == sha(dst / "M42" / rel)
    assert (dst / "M42" / "emptydir").is_dir()
    assert not (dst / "M42" / "@eaDir").exists()
    assert int(os.stat(dst / "M42" / "light_001.fits").st_mtime) == 1_600_000_000
    assert not [p for p in (dst / "M42").rglob("*.rcpart")]
    assert snap.files_done == 3
    assert snap.bytes_done == 1_200_003


@pytest.mark.parametrize("dst_kind", ENDPOINTS)
def test_delta_only_changed_blocks(tmp_path, dst_kind):
    bs = 64 * 1024
    src = tmp_path / "a" / "big.fits"
    mkfile(str(src), bs * 20 + 123, 7)
    dst = tmp_path / "b"
    j, snap = run_job(LocalEndpoint(), [str(src)], ep_for(dst_kind), str(dst), bs=bs)
    assert snap.state == engine.J_DONE
    # Corrupt two blocks of the destination.
    with open(dst / "big.fits", "r+b") as f:
        f.seek(bs * 3 + 10); f.write(b"XXXX")
        f.seek(bs * 17); f.write(b"YYYY")
    j, snap = run_job(LocalEndpoint(), [str(src)], ep_for(dst_kind), str(dst), bs=bs)
    assert snap.state == engine.J_DONE, snap.errors
    assert sha(src) == sha(dst / "big.fits")
    assert snap.bytes_done == 2 * bs
    f = j.files[0]
    assert f.blocks.count(engine.B_DONE) == 2
    assert f.blocks.count(engine.B_SAME) == 19
    # Third run: nothing to do, no partial left behind.
    j, snap = run_job(LocalEndpoint(), [str(src)], ep_for(dst_kind), str(dst), bs=bs)
    assert snap.bytes_done == 0 and snap.state == engine.J_DONE
    assert not list(dst.glob(".*.rcpart"))


def test_size_change_grow_and_shrink(tmp_path):
    bs = 64 * 1024
    src = tmp_path / "a" / "f.bin"
    dst = tmp_path / "b"
    mkfile(str(src), bs * 5, 1)
    run_job(LocalEndpoint(), [str(src)], LocalEndpoint(), str(dst), bs=bs)
    with open(src, "ab") as fh:
        fh.write(b"z" * (bs + 7))
    j, snap = run_job(LocalEndpoint(), [str(src)], LocalEndpoint(), str(dst), bs=bs)
    assert sha(src) == sha(dst / "f.bin")
    assert snap.bytes_done == bs + 7
    os.truncate(src, bs * 2 + 5)
    j, snap = run_job(LocalEndpoint(), [str(src)], LocalEndpoint(), str(dst), bs=bs)
    assert sha(src) == sha(dst / "f.bin"), snap.errors
    assert os.path.getsize(dst / "f.bin") == bs * 2 + 5


def test_resume_from_partial(tmp_path):
    bs = 64 * 1024
    src = tmp_path / "a" / "f.bin"
    dst = tmp_path / "b"
    dst.mkdir()
    mkfile(str(src), bs * 10, 9)
    # Simulate an interrupted run: first 6 blocks already in the partial.
    with open(src, "rb") as s, open(dst / ".f.bin.rcpart", "wb") as d:
        d.write(s.read(bs * 6))
    j, snap = run_job(LocalEndpoint(), [str(src)], LocalEndpoint(), str(dst), bs=bs)
    assert sha(src) == sha(dst / "f.bin")
    assert snap.bytes_done == bs * 4
    assert not (dst / ".f.bin.rcpart").exists()


def test_quick_mode_skips_by_size_mtime(tmp_path):
    src = tmp_path / "a" / "f.bin"
    dst = tmp_path / "b"
    mkfile(str(src), 300_000, 1)
    run_job(LocalEndpoint(), [str(src)], LocalEndpoint(), str(dst))
    j, snap = run_job(LocalEndpoint(), [str(src)], LocalEndpoint(), str(dst), checksum=False)
    assert snap.files_skipped == 1 and snap.bytes_done == 0


def test_connection_loss_is_retried(tmp_path):
    if not PY38:
        pytest.skip("needs a subprocess agent")
    bs = 64 * 1024
    src = tmp_path / "a" / "f.bin"
    mkfile(str(src), bs * 40, 3)
    dst = tmp_path / "b"
    ep = FakeRemote(PY38)
    orig = SshSession.call
    calls = {"n": 0}

    def flaky(self, req, payload=b"", progress=None):
        if req.get("op") == "write":
            calls["n"] += 1
            if calls["n"] % 7 == 0:
                self.proc.kill()
        return orig(self, req, payload, progress)

    SshSession.call = flaky
    try:
        opts = engine.Options(block_size=bs, workers=3, max_retries=10)
        j = engine.Job(LocalEndpoint(), [str(src)], ep, str(dst), opts)
        orig_sleep = engine.time.sleep
        j.start()
        j.wait(120)
    finally:
        SshSession.call = orig
    snap = j.snapshot()
    assert snap.state == engine.J_DONE, snap.errors
    assert sha(src) == sha(dst / "f.bin")


def test_missing_source_reports_error(tmp_path):
    j, snap = run_job(LocalEndpoint(), [str(tmp_path / "nope")], LocalEndpoint(), str(tmp_path))
    assert snap.state == engine.J_FAILED
    assert snap.errors


def fits_like(path, size, seed=0):
    """16-bit sky-ish data: compressible like a real sub."""
    import random
    import struct
    r = random.Random(seed)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    vals = [max(0, min(65535, int(r.gauss(1200, 40)))) for _ in range(size // 2)]
    with open(path, "wb") as f:
        f.write(struct.pack(">%dH" % len(vals), *vals))


@pytest.mark.parametrize("remote", [k for k, v in REMOTES.items() if v])
@pytest.mark.parametrize("comp", ["none", "gzip", "zstd"])
@pytest.mark.parametrize("direction", ["push", "pull"])
def test_compression(tmp_path, remote, comp, direction):
    bs = 64 * 1024
    src = tmp_path / "a" / "sub.fits"
    fits_like(str(src), bs * 6 + 100, 1)
    rem = FakeRemote(REMOTES[remote])
    if direction == "push":
        j, snap = run_job(LocalEndpoint(), [str(src)], rem, str(tmp_path / "b"),
                          bs=bs, compression=comp)
        out = tmp_path / "b" / "sub.fits"
    else:
        j, snap = run_job(rem, [str(src)], LocalEndpoint(), str(tmp_path / "b"),
                          bs=bs, compression=comp)
        out = tmp_path / "b" / "sub.fits"
    assert snap.state == engine.J_DONE, snap.errors
    assert sha(src) == sha(out)
    has_zstd = "zstd" in rem.browse().codecs
    expect = comp if (comp != "zstd" or has_zstd) else "gzip"
    assert snap.compression == expect, snap.notes
    if expect == "none":
        assert snap.compressed_wire == snap.compressed_logical
    else:
        assert snap.compressed_wire < snap.compressed_logical * 0.85
    if comp == "zstd" and not has_zstd:
        assert any("using gzip" in n for n in snap.notes)
    rem.close()


def test_incompressible_blocks_sent_raw(tmp_path):
    src = tmp_path / "a" / "noise.bin"
    mkfile(str(src), 200_000, 5)
    rem = FakeRemote(PYCUR)
    j, snap = run_job(LocalEndpoint(), [str(src)], rem, str(tmp_path / "b"), compression="zstd")
    assert sha(src) == sha(tmp_path / "b" / "noise.bin")
    assert snap.compressed_wire == snap.compressed_logical


def test_agent_source_is_py27_compatible():
    vermin = shutil.which("vermin") or shutil.which("uvx")
    if not vermin:
        pytest.skip("vermin/uvx not available")
    import rcommander.agent as a
    cmd = ([vermin] if vermin.endswith("vermin") else [vermin, "vermin"]) + [
        "--no-tips", "--violations", "-t=2.7-", "-t=3.8-", a.__file__]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert all(b < 128 for b in open(a.__file__, "rb").read()), "agent must be ASCII"


def test_per_stream_throughput(tmp_path):
    """Each stream reports a rate and a history for the sparklines."""
    if not PY38:
        pytest.skip("needs a subprocess agent")
    import time as _t
    bs = 64 * 1024
    src = tmp_path / "a" / "f.bin"
    mkfile(str(src), bs * 60, 4)
    orig = SshSession.call

    def slow(self, req, payload=b"", progress=None):
        if req.get("op") == "write":
            _t.sleep(0.05)
        return orig(self, req, payload, progress)

    SshSession.call = slow
    try:
        j = engine.Job(LocalEndpoint(), [str(src)], FakeRemote(PY38), str(tmp_path / "b"),
                       engine.Options(block_size=bs, workers=3))
        j.start()
        seen_rate = False
        for _ in range(60):
            _t.sleep(0.25)
            s = j.snapshot()
            if any(w.rate > 0 for w in s.workers):
                seen_rate = True
            if s.state == engine.J_DONE:
                break
    finally:
        SshSession.call = orig
    assert seen_rate
    s = j.snapshot()
    assert len(s.workers) == 3
    assert all(w.bytes > 0 for w in s.workers)
    assert sum(w.bytes for w in s.workers) >= bs * 60
    assert any(len(w.hist) >= 2 for w in s.workers)


def _naive(j):
    """The O(all files) totals snapshot() used to compute; it must agree."""
    with j.lock:
        fs = list(j.files)
        needed = sum(f.bytes_needed for f in fs if f.state != engine.F_SKIPPED)
        needed += sum(f.size for f in fs if f.state == engine.F_QUEUED)
        return dict(files_total=len(fs), bytes_total=sum(f.size for f in fs),
                    bytes_same=sum(f.bytes_same for f in fs),
                    hash_total=sum(f.hash_total for f in fs), bytes_needed=needed,
                    files_done=sum(f.state == engine.F_DONE for f in fs),
                    files_skipped=sum(f.state == engine.F_SKIPPED for f in fs),
                    files_failed=sum(f.state == engine.F_FAILED for f in fs))


def test_snapshot_totals_incremental(tmp_path):
    """snapshot() keeps running totals instead of rescanning every file; check
    them against a full scan, mid-transfer and at the end, on a mix of small
    (batched), large (block), identical and changed files."""
    import time as _t
    bs = 64 * 1024
    src, dst = tmp_path / "src" / "T", tmp_path / "dst"
    for i in range(300):
        mkfile(str(src / f"s{i:03}"), 100 + i, i)          # small: batched
    for i in range(6):
        mkfile(str(src / f"big{i}"), bs * 20 + i, 1000 + i)  # block path
    run_job(LocalEndpoint(), [str(src)], LocalEndpoint(), str(dst))
    for i in range(0, 300, 7):                              # change some, keep the rest
        mkfile(str(src / f"s{i:03}"), 100 + i, 5000 + i)
    mkfile(str(src / "big2"), bs * 20 + 2, 9999)
    opts = engine.Options(block_size=bs, workers=3, checksum=True)
    j = engine.Job(LocalEndpoint(), [str(src)], LocalEndpoint(), str(dst), opts)
    import threading
    j.lock = threading.RLock()            # so the test can hold it across both views
    j.cv = threading.Condition(j.lock)
    j.start()
    checks = 0
    while j.state not in (engine.J_DONE, engine.J_FAILED, engine.J_CANCELLED):
        with j.lock:
            s, n = j.snapshot(), _naive(j)
        if j.state == engine.J_RUNNING:
            for k, v in n.items():
                assert getattr(s, k) == v, (k, getattr(s, k), v)
            checks += 1
        _t.sleep(0.001)
    assert checks > 0
    j.wait(60)
    s, n = j.snapshot(), _naive(j)
    assert s.state == engine.J_DONE, j.errors
    for k, v in n.items():
        assert getattr(s, k) == v, (k, getattr(s, k), v)
    assert s.files_total == 306 and s.files_failed == 0


def test_filetask_paths_compact():
    """FileTask keeps (base, rel) instead of full path strings; the paths it
    hands out must be the same either way, including a remapped source rel."""
    f = engine.FileTask("T/a/b.fits", ("/src", "T/a/b.fits"), ("/dst", "T/a/b.fits"), 5, 0, 0o644, 4)
    assert (f.src_path, f.dst_path) == ("/src/T/a/b.fits", "/dst/T/a/b.fits")
    assert f._src_rel is None and f._dst_rel is None          # rel stored once
    g = engine.FileTask("New/b.fits", ("/src", "Old/b.fits"), ("/dst", "New/b.fits"), 5, 0, 0o644, 4)
    assert (g.src_path, g.dst_path) == ("/src/Old/b.fits", "/dst/New/b.fits")
    h = engine.FileTask("b", "/x/y/b", "/d/b", 5, 0, 0o644, 4)    # full paths still work
    assert (h.src_path, h.dst_path) == ("/x/y/b", "/d/b")
    assert not hasattr(h, "__dict__")


def test_copy_with_remapped_dest(tmp_path):
    """Source.to renames the top dir; src and dst paths must still resolve."""
    src = tmp_path / "src" / "M42"
    for i in range(3):
        mkfile(str(src / "sub" / f"f{i}"), 5000 + i, i)
    mkfile(str(src / "big"), 64 * 1024 * 5, 9)
    dst = tmp_path / "dst"
    j, s = run_job(LocalEndpoint(), [engine.Source(str(src), to="Orion")], LocalEndpoint(), str(dst))
    assert s.state == engine.J_DONE, j.errors
    for rel in ("sub/f0", "sub/f1", "sub/f2", "big"):
        assert sha(str(dst / "Orion" / rel)) == sha(str(src / rel))
