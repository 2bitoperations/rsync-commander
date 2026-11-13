"""Small files go in batches: few round trips, same correctness guarantees."""
import collections
import os

import pytest

from rcommander import engine
from rcommander.endpoint import LocalEndpoint, SshSession
from test_engine import ENDPOINTS, PY38, FakeRemote, ep_for, sha


def make_tree(root, n=150, seed=1):
    import random
    r = random.Random(seed)
    for i in range(n):
        p = root / f"d{i % 7}" / f"f{i:04d}.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(r.randbytes(r.randint(0, 20000)))
    (root / "empty.txt").write_bytes(b"")
    big = root / "big.fits"
    big.write_bytes(r.randbytes(300_000))       # > small_file_max below: block path


def same_tree(a, b):
    fa = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    fb = sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file())
    assert fa == fb
    for rel in fa:
        assert sha(a / rel) == sha(b / rel), rel
        assert int(os.stat(a / rel).st_mtime) == int(os.stat(b / rel).st_mtime), rel


class OpCounter:
    def __enter__(self):
        self.ops = collections.Counter()
        self.orig = SshSession.call
        ops, orig = self.ops, self.orig

        def counting(sess, req, payload=b"", progress=None):
            ops[req.get("op")] += 1
            return orig(sess, req, payload, progress)
        SshSession.call = counting
        return self

    def __exit__(self, *a):
        SshSession.call = self.orig


def run(src_ep, src, dst_ep, dst, **kw):
    opts = engine.Options(block_size=64 * 1024, small_file_max=64 * 1024, workers=3,
                          batch_files=kw.pop("batch_files", 64), **kw)
    j = engine.Job(src_ep, [str(src)], dst_ep, str(dst), opts)
    j.start()
    j.wait(120)
    return j, j.snapshot()


@pytest.mark.parametrize("src_kind", ENDPOINTS)
@pytest.mark.parametrize("dst_kind", ENDPOINTS)
def test_small_files_roundtrip(tmp_path, src_kind, dst_kind):
    src = tmp_path / "src"
    make_tree(src)
    dst = tmp_path / "dst"
    j, s = run(ep_for(src_kind), src, ep_for(dst_kind), dst)
    assert s.state == engine.J_DONE, s.errors
    same_tree(src, dst / "src")
    assert s.files_done == 152 and not list(dst.rglob("*.rcpart"))
    # identical re-run: nothing sent, everything verified
    j, s = run(ep_for(src_kind), src, ep_for(dst_kind), dst)
    assert s.state == engine.J_DONE and s.bytes_done == 0 and s.files_skipped == 152


def test_batches_cut_round_trips(tmp_path):
    if not PY38:
        pytest.skip("needs a subprocess agent")
    src = tmp_path / "src"
    make_tree(src, n=300)
    with OpCounter() as c:
        j, s = run(LocalEndpoint(), src, FakeRemote(PY38), tmp_path / "dst")
    assert s.state == engine.J_DONE, s.errors
    same_tree(src, tmp_path / "dst" / "src")
    per_file = sum(c.ops[o] for o in ("prepare", "write", "finalize"))
    batch_ops = sum(c.ops[o] for o in ("probe_many", "put_many"))
    # 301 small files in batches of <=64: ~5 batches x 2 ops, plus the one big file
    assert batch_ops <= 2 * 6, c.ops
    assert per_file <= 3 + 5, c.ops          # only the big file goes per-block
    assert sum(c.ops.values()) < 40, c.ops   # vs ~900 one file at a time


def test_changed_and_touched_files(tmp_path):
    rem = FakeRemote(PY38) if PY38 else LocalEndpoint()
    src = tmp_path / "src"
    make_tree(src, n=40)
    dst = tmp_path / "dst"
    run(LocalEndpoint(), src, rem, dst)
    a, b = src / "d1" / "f0001.txt", src / "d2" / "f0002.txt"
    data = bytearray(a.read_bytes() or b"x")
    data[0] ^= 0xFF                                     # same size, new content
    a.write_bytes(bytes(data))
    os.utime(b, (1_500_000_000, 1_500_000_000))         # same content, new mtime
    j, s = run(LocalEndpoint(), src, rem, dst)
    assert s.state == engine.J_DONE, s.errors
    assert s.files_done == 1 and s.bytes_done == len(data)
    same_tree(src, dst / "src")                          # incl. b's mtime
    # quick mode: size+mtime only, so a same-size same-second edit is not seen,
    # but everything else is skipped without reading
    j, s = run(LocalEndpoint(), src, rem, dst, checksum=False)
    assert s.files_skipped == s.files_total and s.bytes_done == 0


def test_unreadable_small_file_fails_alone(tmp_path):
    src = tmp_path / "src"
    make_tree(src, n=20)
    bad = src / "d0" / "f0000.txt"
    bad.chmod(0)
    try:
        j, s = run(LocalEndpoint(), src, ep_for(ENDPOINTS[-1]), tmp_path / "dst")
    finally:
        bad.chmod(0o644)
    assert s.files_failed == 1 and s.files_done == s.files_total - 1
    assert any("f0000" in rel for rel, _ in s.errors)


def test_batch_survives_connection_loss(tmp_path):
    if not PY38:
        pytest.skip("needs a subprocess agent")
    src = tmp_path / "src"
    make_tree(src, n=200)
    orig = SshSession.call
    n = {"put": 0}

    def flaky(sess, req, payload=b"", progress=None):
        if req.get("op") == "put_many":
            n["put"] += 1
            if n["put"] % 2 == 1:
                sess.proc.kill()
        return orig(sess, req, payload, progress)
    SshSession.call = flaky
    try:
        j, s = run(LocalEndpoint(), src, FakeRemote(PY38), tmp_path / "dst", max_retries=8)
    finally:
        SshSession.call = orig
    assert s.state == engine.J_DONE, s.errors
    same_tree(src, tmp_path / "dst" / "src")
