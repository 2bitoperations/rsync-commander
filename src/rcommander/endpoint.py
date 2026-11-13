"""Endpoints: where files live. A LocalEndpoint runs agent ops in-process;
a RemoteEndpoint runs them in an agent streamed over ssh.

Each Endpoint hands out Sessions. A Session is one channel that does one
request at a time. For a remote host each session is a separate ssh process,
and so a separate TCP stream: parallel streams are how we beat per-stream
throughput limits on high-latency links.
"""

import hashlib
import json
import os
import posixpath
import shlex
import subprocess
import sys
import threading
from pathlib import Path

from . import agent as _agent

def _agent_source():
    """The agent's *source* (it is streamed to remote pythons). In a frozen
    build modules are bytecode, so agent.py ships as a data file next to them."""
    here = Path(_agent.__file__)
    for cand in (here, here.with_suffix(".py"),
                 Path(getattr(sys, "_MEIPASS", "")) / "rcommander" / "agent.py"):
        if cand.suffix == ".py" and cand.is_file():
            return cand.read_bytes()
    from importlib.resources import files
    return files("rcommander").joinpath("agent.py").read_bytes()


AGENT_SRC = _agent_source()

# Runs on the remote under `python -u -c`; must work on Python 2.7 and 3.x.
BOOTSTRAP = (
    "import sys;n=int(sys.argv[1]);"
    "s=getattr(sys.stdin,'buffer',sys.stdin).read(n);"
    "exec(compile(s,'rcagent','exec'),{'__name__':'rcagent'})"
)
# python="auto": first interpreter found on the remote PATH, newest first.
AUTO_PYTHONS = ("python3", "python", "python2")


def remote_agent_command(python):
    boot = f"-u -c {shlex.quote(BOOTSTRAP)} {len(AGENT_SRC)}"
    if python != "auto":
        return f"exec {shlex.quote(python)} {boot}"
    tries = "; ".join(f"command -v {p} >/dev/null 2>&1 && exec {p} {boot}"
                      for p in AUTO_PYTHONS)
    return f"{tries}; echo 'rcommander: no python on remote PATH' >&2; exit 127"


class RemoteError(Exception):
    def __init__(self, msg, err=0):
        super().__init__(msg)
        self.errno = err


class ConnectionLost(Exception):
    """The channel died; the op may be retried on a fresh session."""


def _check(resp):
    if not resp.get("ok"):
        raise RemoteError(resp.get("error", "unknown error"), resp.get("errno", 0))
    return resp


class Session:
    """API shared by local and remote sessions. Every method may raise
    RemoteError (the op failed) or ConnectionLost (the channel died)."""

    wire_compress = False          # True for sessions that cross a network
    codecs = _agent.codec_info()   # codecs the far side can (de)compress
    python = None

    def call(self, req, payload=b"", progress=None):
        """progress(nbytes) is called as payload bytes move on the wire."""
        raise NotImplementedError

    def close(self):
        pass

    # Convenience wrappers.
    def listdir(self, path, show_hidden=False):
        return self.call({"op": "listdir", "path": path, "show_hidden": show_hidden})[0]

    def stat(self, path):
        return self.call({"op": "stat", "path": path})[0]

    def walk(self, path, excludes=None):
        req = {"op": "walk", "path": path}
        if excludes is not None:
            req["excludes"] = list(excludes)
        return self.call(req)[0]

    def mkdir(self, path):
        self.call({"op": "mkdir", "path": path})

    def mkdirs(self, paths):
        self.call({"op": "mkdirs", "paths": list(paths)})

    def rename(self, src, dst):
        self.call({"op": "rename", "src": src, "dst": dst})

    def delete(self, path):
        self.call({"op": "delete", "path": path})

    def hash(self, path, bs, start, count, limit):
        return self.call({"op": "hash", "path": path, "bs": bs, "start": start,
                          "count": count, "limit": limit})[0]["hashes"]

    def read(self, path, offset, length, codec=None, level=1, progress=None):
        """Returns (data, sha1hex, wire_bytes). codec: None | "zlib" | "zstd";
        the far side only compresses if that actually shrinks the block."""
        req = {"op": "read", "path": path, "offset": offset, "length": length}
        if codec and self.wire_compress and codec in self.codecs:
            req["codec"], req["level"] = codec, level
        resp, data = self.call(req, progress=progress)
        wire = len(data)
        z = resp.get("z")
        if z:
            data = _agent.decompress("zlib" if z is True else z, data)
        if not self.wire_compress:
            wire = 0
        if hashlib.sha1(data).hexdigest() != resp["sha1"]:
            raise ConnectionLost("block checksum mismatch after read")
        return data, resp["sha1"], wire

    def write(self, path, offset, data, sha1, codec=None, level=1, progress=None):
        """Returns wire bytes sent."""
        req = {"op": "write", "path": path, "offset": offset, "sha1": sha1}
        payload = data
        if codec and self.wire_compress and data and codec in self.codecs:
            zd = _agent.compress(codec, level, data)
            if len(zd) < len(data) * 0.92:
                req["z"] = codec
                payload = zd
        if progress is not None and payload is not data:
            scale = len(data) / len(payload)   # report progress in logical bytes
            inner = progress
            progress = lambda k: inner(k * scale)  # noqa: E731
        self.call(req, payload, progress=progress)
        return len(payload)

    # -- small-file batches ---------------------------------------------------
    def probe_many(self, paths, hash=False):
        return self.call({"op": "probe_many", "paths": list(paths), "hash": hash})[0]["files"]

    def read_many(self, paths, codec=None, level=1, progress=None):
        """-> ([(data|None, sha1|None, error|None) per path], wire_bytes)"""
        req = {"op": "read_many", "paths": list(paths)}
        if codec and self.wire_compress and codec in self.codecs:
            req["codec"], req["level"] = codec, level
        resp, blob = self.call(req, progress=progress)
        wire = len(blob) if self.wire_compress else 0
        if resp.get("z"):
            blob = _agent.decompress(resp["z"], blob)
        out, off = [], 0
        for e in resp["files"]:
            if "error" in e:
                out.append((None, None, e["error"]))
                continue
            d = blob[off:off + e["len"]]
            off += e["len"]
            if hashlib.sha1(d).hexdigest() != e["sha1"]:
                raise ConnectionLost("batch checksum mismatch after read")
            out.append((d, e["sha1"], None))
        return out, wire

    def put_many(self, files, blobs, codec=None, level=1, progress=None, touch=()):
        """files: [{final, len, sha1, mtime_ns, mode}] matching blobs.
        -> (errors per file, touch errors, wire_bytes)"""
        data = b"".join(blobs)
        req = {"op": "put_many", "files": files, "touch": [list(t) for t in touch]}
        payload = data
        if codec and self.wire_compress and data and codec in self.codecs:
            zd = _agent.compress(codec, level, data)
            if len(zd) < len(data) * 0.92:
                req["z"] = codec
                payload = zd
        if progress is not None and payload is not data and payload:
            scale = len(data) / len(payload)
            inner = progress
            progress = lambda k: inner(k * scale)  # noqa: E731
        resp = self.call(req, payload, progress=progress)[0]
        return resp["errors"], resp.get("touch_errors", []), (len(payload) if self.wire_compress else 0)

    def prepare(self, final):
        return self.call({"op": "prepare", "final": final})[0]

    def clone(self, src, dst):
        self.call({"op": "clone", "src": src, "dst": dst})

    def finalize(self, partial, final, size, mtime_ns, mode):
        self.call({"op": "finalize", "partial": partial, "final": final,
                   "size": size, "mtime_ns": mtime_ns, "mode": mode})

    def touch_meta(self, path, mtime_ns):
        self.call({"op": "touch_meta", "path": path, "mtime_ns": mtime_ns})


class LocalSession(Session):
    def __init__(self):
        self._h = _agent.Handler()

    def call(self, req, payload=b"", progress=None):
        try:
            resp, data = self._h.dispatch(req, payload)
        except _agent.AgentError as ex:
            raise RemoteError(str(ex), ex.errno) from None
        except OSError as ex:
            raise RemoteError(f"{ex.strerror or ex}: {_agent._text(ex.filename or '')}",
                              ex.errno or 0) from None
        resp["ok"] = True
        return resp, data


class SshSession(Session):
    wire_compress = True

    def __init__(self, ep):
        self.ep = ep
        self._lock = threading.Lock()
        self.proc = subprocess.Popen(ep.agent_argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0)
        try:
            self.proc.stdin.write(AGENT_SRC)
            self.proc.stdin.flush()
            hello, _ = _agent.read_frame(self.proc.stdout)
        except (EOFError, OSError, ValueError) as ex:
            err = self._stderr_tail()
            self.close()
            raise ConnectionLost(f"ssh {ep.host}: could not start agent: {err or ex}") from None
        if not hello.get("ready"):
            raise ConnectionLost(f"ssh {ep.host}: bad agent greeting {hello!r}")
        self.python = hello.get("python")
        # v1 agents predate codec negotiation but always had zlib
        self.codecs = hello.get("codecs") or {"zlib": "zlib"}

    def _stderr_tail(self):
        try:
            if self.proc.poll() is None:
                return ""
            return self.proc.stderr.read().decode("utf-8", "replace").strip()[-400:]
        except Exception:
            return ""

    CHUNK = 256 * 1024

    def call(self, req, payload=b"", progress=None):
        with self._lock:
            try:
                if progress is None:
                    _agent.write_frame(self.proc.stdin, req, payload)
                    resp, data = _agent.read_frame(self.proc.stdout)
                else:
                    resp, data = self._call_progress(req, payload, progress)
            except (EOFError, OSError, ValueError) as ex:
                self.close()
                raise ConnectionLost(f"{self.ep.host}: {ex}") from None
        return _check(resp), data

    def _call_progress(self, req, payload, progress):
        out, inp = self.proc.stdin, self.proc.stdout
        js = json.dumps(req, separators=(",", ":")).encode()
        out.write(_agent.HDR.pack(len(js), len(payload)) + js)
        mv = memoryview(payload)
        for i in range(0, len(mv), self.CHUNK):
            out.write(mv[i:i + self.CHUNK])
            progress(len(mv[i:i + self.CHUNK]))
        out.flush()
        jl, pl = _agent.HDR.unpack(_agent.read_exact(inp, _agent.HDR.size))
        resp = json.loads(_agent.read_exact(inp, jl))
        # compressed reads: report progress in logical (uncompressed) bytes
        scale = (resp.get("rawlen") or pl) / pl if pl else 1
        buf = bytearray()
        while len(buf) < pl:
            c = inp.read(min(self.CHUNK, pl - len(buf)))
            if not c:
                raise EOFError("connection closed")
            buf += c
            progress(len(c) * scale)
        return resp, bytes(buf)

    def close(self):
        p = getattr(self, "proc", None)
        if p and p.poll() is None:
            try:
                p.stdin.close()
            except OSError:
                pass
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()


class Endpoint:
    name = "?"
    pathmod = posixpath

    def session(self) -> Session:
        raise NotImplementedError

    def browse(self) -> Session:
        """A long-lived shared session for UI browsing (thread-safe)."""
        raise NotImplementedError

    def join(self, *parts):
        return self.pathmod.join(*parts)

    def close(self):
        pass

    def label(self, path):
        return f"{self.name}:{path}"


class LocalEndpoint(Endpoint):
    name = "local"
    pathmod = os.path
    is_local = True

    def __init__(self):
        self._s = LocalSession()

    def session(self):
        return self._s

    def browse(self):
        return self._s

    def home(self):
        return str(Path.home())

    def label(self, path):
        return path


class RemoteEndpoint(Endpoint):
    is_local = False

    def __init__(self, host, python="auto", ssh_opts=()):
        self.host = host
        self.name = host
        self.python = python
        self.ssh_opts = list(ssh_opts)
        self.control_path = None   # set to share an authenticated ssh master
        self.master = None         # sshauth.SshMaster owned by this endpoint
        self._auth_lock = threading.Lock()
        self._authed = False
        self._browse = None
        self._browse_lock = threading.Lock()
        self._home = None

    def agent_argv(self):
        return self.ssh_argv() + [self.host, remote_agent_command(self.python)]

    def ssh_argv(self):
        return ["ssh", "-T", "-x", "-e", "none",
                "-o", "BatchMode=yes",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
                # Default: one TCP stream per session (no multiplexing). With a
                # control_path (see sshauth.SshMaster) every session rides one
                # already-authenticated master connection instead.
                "-o", "ControlMaster=no",
                "-o", f"ControlPath={self.control_path or 'none'}",
                *self.ssh_opts]

    def session(self):
        return SshSession(self)

    def browse(self):
        with self._browse_lock:
            if self._browse is None or self._browse.proc.poll() is not None:
                self._browse = SshSession(self)
            return self._browse

    def browse_call(self, fn):
        """Run fn(session) on the browse session, reconnecting once if it died."""
        try:
            return fn(self.browse())
        except ConnectionLost:
            with self._browse_lock:
                self._browse = None
            return fn(self.browse())

    def ensure_auth(self, prompter=None, mode="auto", log=lambda m: None):
        """Authenticate once for the life of this endpoint (GUI/TUI): key auth if
        it works, else one master connection whose prompts go to `prompter`."""
        from .sshauth import ensure_auth
        with self._auth_lock:
            if self._authed:
                return
            self.master = ensure_auth(self, mode, log=log, prompter=prompter)
            self._authed = True

    def home(self):
        if self._home is None:
            self._home = self.browse_call(lambda s: s.call({"op": "hello"})[0]["home"])
        return self._home

    def close(self):
        with self._browse_lock:
            if self._browse:
                self._browse.close()
                self._browse = None
        if self.master:
            self.master.close()
            self.master = None
            self._authed = False


def browse_call(ep, fn):
    if isinstance(ep, RemoteEndpoint):
        return ep.browse_call(fn)
    return fn(ep.browse())


def ssh_config_hosts():
    """Concrete Host aliases from ~/.ssh/config (no wildcards), following Include."""
    hosts, seen = [], set()

    def parse(path):
        try:
            lines = Path(path).expanduser().read_text().splitlines()
        except OSError:
            return
        for line in lines:
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            key, val = parts[0].lower(), parts[1]
            if key == "include":
                for inc in val.split():
                    p = Path(inc).expanduser()
                    if not p.is_absolute():
                        p = Path("~/.ssh").expanduser() / p
                    for m in sorted(p.parent.glob(p.name)):
                        parse(m)
            elif key == "host":
                for h in val.split():
                    if not any(c in h for c in "*?!") and h not in seen:
                        seen.add(h)
                        hosts.append(h)

    parse("~/.ssh/config")
    return hosts


if sys.platform == "win32":  # pragma: no cover
    raise SystemExit("rcommander targets macOS and Linux")
