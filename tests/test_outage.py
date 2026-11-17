"""Network outages (wifi dropped, interface gone, host asleep): the job waits
for the host to come back instead of burning per-file retries, and gives up
cleanly after max_outage."""
import sys
import threading
import time

import pytest

from rcommander import engine
from rcommander.endpoint import (AGENT_SRC, BOOTSTRAP, ConnectFailed, ConnectionLost,
                                 LocalEndpoint, SshSession)

from test_engine import FakeRemote, mkfile, sha

DOWN = ["sh", "-c", "echo 'ssh: Could not resolve hostname box.local' >&2; exit 255"]


class FlakyHost(FakeRemote):
    """A FakeRemote whose 'network' can be switched off: while down, new
    sessions fail the way ssh does (exit 255) and live ones are killed."""

    def __init__(self):
        super().__init__(sys.executable)
        self.down = False
        self.live = []
        self.connects = 0

    def agent_argv(self):
        self.connects += 1
        # no sh wrapper: killing the process must really cut the connection
        return DOWN if self.down else [sys.executable, "-u", "-c", BOOTSTRAP, str(len(AGENT_SRC))]

    def session(self):
        s = super().session()
        self.live.append(s)
        if self.down:            # connected just before the drop: dies too
            s.proc.kill()
        return s

    def go_down(self):
        self.down = True
        for s in self.live:
            if s.proc.poll() is None:
                s.proc.kill()


@pytest.fixture
def fast_backoff(monkeypatch):
    orig = engine.Worker._sleep
    monkeypatch.setattr(engine.Worker, "_sleep", lambda self, s: orig(self, min(s, 0.2)))


def _job(tmp_path, ep, **kw):
    bs = 64 * 1024
    src = tmp_path / "a" / "f.bin"
    mkfile(str(src), bs * 200, 5)
    opts = engine.Options(block_size=bs, workers=3, compression="none", **kw)
    return src, engine.Job(LocalEndpoint(), [str(src)], ep, str(tmp_path / "b"), opts)


def test_outage_is_waited_out(tmp_path, fast_backoff):
    ep = FlakyHost()
    orig = SshSession.call
    writes = {"n": 0}

    def call(self, req, payload=b"", progress=None):
        if req.get("op") == "write":
            writes["n"] += 1
            if writes["n"] == 20:       # mid-transfer: the network goes away
                ep.go_down()
                threading.Timer(1.5, lambda: setattr(ep, "down", False)).start()
        return orig(self, req, payload, progress)

    SshSession.call = call
    try:
        # max_retries=1: if outages counted as retries, files would fail
        src, j = _job(tmp_path, ep, max_retries=1)
        j.start()
        j.wait(60)
    finally:
        SshSession.call = orig
    snap = j.snapshot()
    assert snap.state == engine.J_DONE, snap.errors
    assert sha(src) == sha(tmp_path / "b" / "f.bin")
    assert any(m.startswith("connection lost") for m in j.log), j.log
    assert any(m.startswith("reconnected to fake after") for m in j.log), j.log
    assert ep.connects > 3 + 3   # it really did fail to connect for a while


def test_gives_up_after_max_outage(tmp_path, fast_backoff):
    ep = FlakyHost()
    orig = SshSession.call

    def call(self, req, payload=b"", progress=None):
        if req.get("op") == "write":
            ep.go_down()                # and it never comes back
        return orig(self, req, payload, progress)

    SshSession.call = call
    try:
        src, j = _job(tmp_path, ep, max_outage=1)
        t0 = time.time()
        j.start()
        j.wait(30)
    finally:
        SshSession.call = orig
    snap = j.snapshot()
    assert time.time() - t0 < 15
    assert snap.state == engine.J_FAILED
    assert any("gave up" in m for _, m in snap.errors), snap.errors
    # nothing was failed file-by-file: the partial is kept for a rerun
    assert not any(r.endswith("f.bin") for r, _ in snap.errors), snap.errors
    assert list((tmp_path / "b").glob(".f.bin*.rcpart"))


class _Argv(FakeRemote):
    def __init__(self, argv):
        super().__init__(sys.executable)
        self.argv = argv

    def agent_argv(self):
        return self.argv


def test_hung_handshake_times_out(monkeypatch):
    # e.g. ssh stuck resolving a .local name after a network change
    monkeypatch.setattr(SshSession, "HANDSHAKE_TIMEOUT", 0.5)
    t0 = time.time()
    with pytest.raises(ConnectFailed, match="no answer"):
        SshSession(_Argv(["sleep", "30"]))
    assert time.time() - t0 < 5


def test_connect_errors_classified():
    with pytest.raises(ConnectFailed, match="resolve hostname"):
        SshSession(_Argv(DOWN))
    # the remote side failing (no python) is not an outage
    with pytest.raises(ConnectionLost) as ei:
        SshSession(_Argv(["sh", "-c", "echo 'rcommander: no python' >&2; exit 127"]))
    assert not isinstance(ei.value, ConnectFailed)
