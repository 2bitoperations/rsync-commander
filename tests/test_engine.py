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
