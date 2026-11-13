"""NAS-to-NAS: same box copies on the box; different boxes push directly."""
import os
import shlex

import pytest

from rcommander import engine
from rcommander.endpoint import AGENT_SRC, BOOTSTRAP
from test_batches import make_tree, same_tree
from test_engine import PY27, PY38, PYCUR, FakeRemote

pytestmark = pytest.mark.skipif(not PY38, reason="needs a subprocess agent")


def run(src_ep, srcs, dst_ep, dst, **kw):
    opts = engine.Options(block_size=64 * 1024, small_file_max=16 * 1024, workers=3, **kw)
    j = engine.Job(src_ep, [str(s) for s in srcs], dst_ep, str(dst), opts)
    j.start()
    j.wait(120)
    return j, j.snapshot()


def local_peer(python):
    """What a source box would run to reach the peer: here, a local agent."""
    return ["sh", "-c", f"{shlex.quote(python)} -u -c {shlex.quote(BOOTSTRAP)} {len(AGENT_SRC)}"]


def test_same_box_copies_on_the_box(tmp_path):
    src = tmp_path / "src"
    make_tree(src, n=60)
    box = FakeRemote(PY38, host="nas1")
    j, s = run(box, [src], FakeRemote(PY38, host="nas1"), tmp_path / "dst")
    assert j.mode == "server" and s.state == engine.J_DONE, s.errors
    same_tree(src, tmp_path / "dst" / "src")
    assert s.wire_bytes == 0
    f = src / "d1" / "f0001.txt"
    f.write_bytes(f.read_bytes() + b"more")
    j, s = run(box, [src], box, tmp_path / "dst")
    assert s.files_done == 1 and s.files_skipped == s.files_total - 1
    same_tree(src, tmp_path / "dst" / "src")
    j, s = run(box, [src], box, tmp_path / "dst", checksum=False)
    assert s.files_skipped == s.files_total


@pytest.mark.parametrize("src_py", [p for p in (PY38, PY27) if p])
def test_direct_box_to_box(tmp_path, monkeypatch, src_py):
    monkeypatch.setattr(engine, "peer_argv", lambda dst: (local_peer(PYCUR), None))
    src = tmp_path / "src"
    make_tree(src, n=80)                       # small files (batches) + one big file
    a, b = FakeRemote(src_py, host="nas-a"), FakeRemote(PYCUR, host="nas-b")
    j, s = run(a, [src], b, tmp_path / "dst", compression="zstd")
    assert j.mode == "direct", j.log
    assert s.state == engine.J_DONE, s.errors
    same_tree(src, tmp_path / "dst" / "src")
    # corrupt one block of the big file on the destination: only it is re-pushed
    big = tmp_path / "dst" / "src" / "big.fits"
    with open(big, "r+b") as fh:
        fh.seek(70_000)
        fh.write(b"XXXX")
    j, s = run(a, [src], b, tmp_path / "dst")
    assert j.mode == "direct" and s.state == engine.J_DONE, s.errors
    assert s.bytes_done == 64 * 1024 and s.files_done == 1
    same_tree(src, tmp_path / "dst" / "src")


def test_direct_falls_back_to_relay(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "peer_argv", lambda dst: (["sh", "-c", "exit 255"], None))
    src = tmp_path / "src"
    make_tree(src, n=10)
    j, s = run(FakeRemote(PY38, host="nas-a"), [src], FakeRemote(PY38, host="nas-b"),
               tmp_path / "dst")
    assert j.mode == "relay" and s.state == engine.J_DONE, s.errors
    assert any("relaying" in n for n in s.notes)
    same_tree(src, tmp_path / "dst" / "src")


def test_direct_off_and_server_side_off(tmp_path):
    src = tmp_path / "src"
    make_tree(src, n=5)
    box = FakeRemote(PY38, host="nas1")
    j, s = run(box, [src], box, tmp_path / "dst", server_side=False)
    assert j.mode == "relay" and s.state == engine.J_DONE
    j, s = run(FakeRemote(PY38, host="a"), [src], FakeRemote(PY38, host="b"),
               tmp_path / "dst2", direct=False)
    assert j.mode == "relay" and s.state == engine.J_DONE
    assert os.path.exists(tmp_path / "dst2" / "src" / "big.fits")
