# -*- coding: utf-8 -*-
"""rcommander agent: the file-ops server that runs on each endpoint.

This one file runs in two places:
  * on a remote host (e.g. a Synology NAS), streamed over ssh into the
    remote python at connect time -- nothing has to be installed on the box;
  * in-process on the local machine, via `Handler` directly.

Constraints: stdlib only, and the SAME source must run on Python 2.7 and on
3.8+ (some shared hosts have only 2.7; Synology DSM 7 ships 3.8). So:
  no f-strings, no keyword-only args, no nonlocal, no `yield from`;
  no os.scandir / os.pread / os.replace / os.truncate / st_mtime_ns /
  os.utime(ns=) / FileNotFoundError / makedirs(exist_ok) / shutil.which /
  subprocess.DEVNULL; catch EnvironmentError (py2 IOError is not OSError).
Keep this file pure ASCII.

Wire protocol (both directions):
    8-byte header  ">II"  = (json_len, payload_len)
    json_len bytes        = UTF-8 JSON object
    payload_len bytes     = raw binary payload (block data)
Requests carry {"op": name, ...}. Responses carry {"ok": true, ...} or
{"ok": false, "error": msg, "errno": n}.

Compression codecs: "zlib" (always), "zstd" (if the host has Python's
compression.zstd, the `zstandard` package -- also looked for in
~/.rcommander/lib -- or a `zstd` binary on PATH).
"""

import errno as _errno
import hashlib
import json
import os
import platform
import shutil
import socket
import stat as _stat
import struct
import subprocess
import sys
import zlib

AGENT_VERSION = 2
HDR = struct.Struct(">II")
PY2 = sys.version_info[0] == 2

# Never transferred and hidden from listings by default.
# @eaDir = Synology thumbnail/metadata dirs, #recycle = Synology recycle bin.
DEFAULT_EXCLUDES = ("@eaDir", "#recycle", ".DS_Store", "#snapshot")
PARTIAL_SUFFIX = ".rcpart"
HELPER_LIB = os.path.join(os.path.expanduser("~"), ".rcommander", "lib")


# -- py2/py3 compatibility helpers --------------------------------------------

def _which(name):
    for d in os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    for d in ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin", "/opt/bin"):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _makedirs(p):
    try:
        os.makedirs(p)
    except EnvironmentError as ex:
        if ex.errno != _errno.EEXIST or not os.path.isdir(p):
            raise


def _mtime_ns(st):
    ns = getattr(st, "st_mtime_ns", None)
    if ns is None:
        ns = int(round(st.st_mtime * 1e9))
    return ns


def _set_mtime(path, ns):
    if PY2:
        t = ns / 1e9
        os.utime(path, (t, t))
    else:
        os.utime(path, ns=(ns, ns))  # novm (py3 branch)


def _stat_or_none(p, follow=True):
    try:
        return os.stat(p) if follow else os.lstat(p)
    except EnvironmentError as ex:
        if ex.errno in (_errno.ENOENT, _errno.ENOTDIR):
            return None
        raise


def _text(name):
    """Names for JSON. On py2 the agent works in UTF-8 byte strings (see
    _py2_native), so decode them; undecodable names are listed with U+FFFD
    (such files can be seen but not transferred)."""
    if isinstance(name, bytes):
        try:
            return name.decode("utf-8")
        except UnicodeDecodeError:
            return name.decode("utf-8", "replace")
    return name


def _py2_native(obj):
    """py2: turn JSON unicode into UTF-8 byte strings so os.* calls never hit
    the ASCII filesystem encoding a bare ssh session usually has."""
    if isinstance(obj, dict):
        return dict((_py2_native(k), _py2_native(v)) for k, v in obj.items())
    if isinstance(obj, list):
        return [_py2_native(x) for x in obj]
    if not isinstance(obj, (bytes, int, float, bool, type(None))):
        return obj.encode("utf-8")
    return obj


def _devnull():
    return open(os.devnull, "wb")


def _read_at(f, off, n):
    f.seek(off)
    parts = []
    got = 0
    while got < n:
        c = f.read(min(n - got, 1 << 22))
        if not c:
            break
        parts.append(c)
        got += len(c)
    return b"".join(parts)


def _write_at(fd, off, data):
    os.lseek(fd, off, os.SEEK_SET)
    done = 0
    mv = memoryview(data)
    while done < len(data):
        done += os.write(fd, mv[done:].tobytes() if PY2 else mv[done:])


# -- compression codecs -------------------------------------------------------

def _zstd_codec():
    try:
        from compression import zstd as z  # novm (Python 3.14+, guarded)
        return (lambda d, lvl: z.compress(d, lvl)), z.decompress, "compression.zstd"
    except ImportError:
        pass
    if os.path.isdir(HELPER_LIB) and HELPER_LIB not in sys.path:
        sys.path.insert(0, HELPER_LIB)
    try:
        import zstandard as z
        return ((lambda d, lvl: z.ZstdCompressor(level=lvl).compress(d)),
                (lambda d: z.ZstdDecompressor().decompress(d)), "zstandard")
    except ImportError:
        pass
    exe = _which("zstd")
    if exe:
        def run(args, data):
            p = subprocess.Popen([exe] + args, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = p.communicate(data)
            if p.returncode != 0:
                raise AgentError("zstd failed: %s" % err.decode("utf-8", "replace"))
            return out
        return ((lambda d, lvl: run(["-q", "-c", "-%d" % max(1, min(19, lvl))], d)),
                (lambda d: run(["-q", "-d", "-c"], d)), exe)
    return None


CODECS = {"zlib": ((lambda d, lvl: zlib.compress(d, max(1, min(9, lvl)))),
                   zlib.decompress, "zlib %s" % zlib.ZLIB_VERSION)}
_z = _zstd_codec()
if _z:
    CODECS["zstd"] = _z


def compress(codec, level, data):
    return CODECS[codec][0](data, level)


def decompress(codec, data):
    return CODECS[codec][1](data)


def codec_info():
    return dict((k, v[2]) for k, v in CODECS.items())


# -- ops ---------------------------------------------------------------------

def partial_name(final_path):
    d, b = os.path.split(final_path)
    return os.path.join(d, "." + b + PARTIAL_SUFFIX)


def is_partial(name):
    return name.startswith(".") and name.endswith(PARTIAL_SUFFIX)


def _excluded(name, excludes):
    return name in excludes or is_partial(name)


def _entry(name, st):
    m = st.st_mode
    if _stat.S_ISDIR(m):
        t = "d"
    elif _stat.S_ISREG(m):
        t = "f"
    elif _stat.S_ISLNK(m):
        t = "l"
    else:
        t = "o"
    return {"name": _text(name), "type": t, "size": st.st_size,
            "mtime_ns": _mtime_ns(st), "mode": _stat.S_IMODE(m)}


class AgentError(Exception):
    def __init__(self, msg, err=0):
        Exception.__init__(self, msg)
        self.errno = err


class Handler(object):
    """Implements every op. Stateless, so safe to share across threads locally."""

    def dispatch(self, req, payload):
        if PY2:
            req = _py2_native(req)
        op = req.get("op")
        fn = getattr(self, "op_" + str(op), None)
        if fn is None:
            raise AgentError("unknown op %r" % (op,))
        return fn(req, payload)

    # -- info / browsing ---------------------------------------------------
    def op_hello(self, req, payload):
        return {"version": AGENT_VERSION, "python": platform.python_version(),
                "platform": sys.platform, "host": socket.gethostname(),
                "home": os.path.expanduser("~"), "sep": os.sep,
                "codecs": codec_info()}, b""

    def op_listdir(self, req, payload):
        path = req["path"]
        show_hidden = req.get("show_hidden", False)
        out = []
        for name in os.listdir(path):
            if not show_hidden and _excluded(name, DEFAULT_EXCLUDES):
                continue
            full = os.path.join(path, name)
            lst = _stat_or_none(full, follow=False)
            if lst is None:
                continue
            st = lst
            if _stat.S_ISLNK(lst.st_mode):
                st = _stat_or_none(full) or lst
            e = _entry(name, st)
            if _stat.S_ISLNK(lst.st_mode):
                e["link"] = True
            out.append(e)
        return {"path": os.path.abspath(path), "entries": out}, b""

    def op_stat(self, req, payload):
        p = req["path"]
        st = _stat_or_none(p)
        if st is None:
            return {"exists": False}, b""
        e = _entry(os.path.basename(p), st)
        e["exists"] = True
        return e, b""

    def op_walk(self, req, payload):
        """Recursive listing of `path` (file or dir). Returns entries with
        `rel` paths relative to dirname(path), so the top item keeps its name.
        Symlinks are reported (type l) but not followed."""
        root = req["path"].rstrip("/") or "/"
        excludes = tuple(req.get("excludes", DEFAULT_EXCLUDES))
        base = os.path.dirname(root)
        out = []
        st = _stat_or_none(root, follow=False)
        if st is None:
            return {"exists": False, "entries": []}, b""
        top = _entry(os.path.basename(root), st)
        top["rel"] = _text(os.path.basename(root))
        out.append(top)
        if top["type"] == "d":
            stack = [root]
            while stack:
                d = stack.pop()
                try:
                    names = sorted(os.listdir(d))
                except EnvironmentError as ex:
                    out.append({"rel": _text(os.path.relpath(d, base)), "type": "err",
                                "error": str(ex)})
                    continue
                for name in names:
                    if _excluded(name, excludes):
                        continue
                    full = os.path.join(d, name)
                    st = _stat_or_none(full, follow=False)
                    if st is None:
                        continue
                    e = _entry(name, st)
                    e["rel"] = _text(os.path.relpath(full, base))
                    out.append(e)
                    if e["type"] == "d":
                        stack.append(full)
        return {"exists": True, "entries": out}, b""

    # -- file management ---------------------------------------------------
    def op_mkdir(self, req, payload):
        _makedirs(req["path"])
        return {}, b""

    def op_mkdirs(self, req, payload):
        for p in req["paths"]:
            _makedirs(p)
        return {}, b""

    def op_rename(self, req, payload):
        if os.path.exists(req["dst"]):
            raise AgentError("destination exists: %s" % req["dst"], _errno.EEXIST)
        os.rename(req["src"], req["dst"])
        return {}, b""

    def op_delete(self, req, payload):
        p = req["path"]
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.unlink(p)
        return {}, b""

    # -- transfer primitives -----------------------------------------------
    def op_hash(self, req, payload):
        """SHA-1 of blocks [start, start+count) of size bs, clipped to `limit`
        bytes (the logical file size being synced) and to the real file end.
        A block that lies entirely past EOF hashes as null."""
        path, bs = req["path"], int(req["bs"])
        start, count = int(req["start"]), int(req["count"])
        limit = req.get("limit")
        out = []
        f = open(path, "rb")
        try:
            real = os.fstat(f.fileno()).st_size
            end_cap = real if limit is None else min(real, int(limit))
            for i in range(start, start + count):
                off = i * bs
                n = min(bs, end_cap - off)
                if n <= 0:
                    out.append(None)
                    continue
                f.seek(off)
                h = hashlib.sha1()
                got = 0
                while got < n:
                    chunk = f.read(min(n - got, 1 << 22))
                    if not chunk:
                        break
                    h.update(chunk)
                    got += len(chunk)
                out.append(h.hexdigest() if got == n else None)
        finally:
            f.close()
        return {"hashes": out}, b""

    def op_read(self, req, payload):
        path, off, n = req["path"], int(req["offset"]), int(req["length"])
        f = open(path, "rb")
        try:
            data = _read_at(f, off, n)
        finally:
            f.close()
        resp = {"sha1": hashlib.sha1(data).hexdigest(), "rawlen": len(data), "z": False}
        codec = req.get("codec")
        if codec is None and req.get("compress"):  # v1 clients
            codec = "zlib"
        if codec and codec in CODECS and data:
            zd = compress(codec, int(req.get("level", 1)), data)
            if len(zd) < len(data) * 0.92:
                resp["z"] = codec
                data = zd
        return resp, data

    def op_prepare(self, req, payload):
        """Decide the basis for receiving `final`.
        Returns basis = "partial" (resume an interrupted transfer),
        "final" (existing file to diff against) or None (fresh)."""
        final = req["final"]
        part = partial_name(final)
        parent = os.path.dirname(final)
        if parent:
            _makedirs(parent)
        st = _stat_or_none(part)
        if st is not None and _stat.S_ISREG(st.st_mode):
            return {"basis": "partial", "basis_size": st.st_size,
                    "basis_mtime_ns": _mtime_ns(st), "partial": part}, b""
        st = _stat_or_none(final)
        if st is not None and _stat.S_ISREG(st.st_mode):
            return {"basis": "final", "basis_size": st.st_size,
                    "basis_mtime_ns": _mtime_ns(st), "partial": part}, b""
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        os.close(fd)
        return {"basis": None, "basis_size": 0, "partial": part}, b""

    def op_clone(self, req, payload):
        """Copy final -> partial, using a CoW clone where the FS supports it
        (btrfs on Synology, APFS on macOS) so it is instant."""
        src, dst = req["src"], req["dst"]
        tmp = dst + ".cloning"
        done = False
        null = _devnull()
        try:
            if sys.platform.startswith("linux"):
                done = subprocess.call(["cp", "--reflink=auto", "--", src, tmp],
                                       stdout=null, stderr=null) == 0
            elif sys.platform == "darwin":
                done = subprocess.call(["cp", "-c", src, tmp], stdout=null, stderr=null) == 0
        except EnvironmentError:
            done = False
        finally:
            null.close()
        if not done:
            shutil.copyfile(src, tmp)
        os.rename(tmp, dst)
        return {}, b""

    def op_write(self, req, payload):
        path, off = req["path"], int(req["offset"])
        data = payload
        z = req.get("z")
        if z:
            codec = "zlib" if z is True else z
            if codec not in CODECS:
                raise AgentError("codec %s not available here" % codec)
            data = decompress(codec, data)
        want = req.get("sha1")
        if want and hashlib.sha1(data).hexdigest() != want:
            raise AgentError("block checksum mismatch on receive at offset %d" % off)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            _write_at(fd, off, data)
        finally:
            os.close(fd)
        return {"written": len(data)}, b""

    def op_finalize(self, req, payload):
        part, final = req["partial"], req["final"]
        size, mtime_ns = int(req["size"]), int(req["mtime_ns"])
        target = part if part else final
        f = open(target, "r+b")
        try:
            f.truncate(size)
            f.flush()
            if req.get("fsync", True):
                os.fsync(f.fileno())
        finally:
            f.close()
        _set_mtime(target, mtime_ns)
        if req.get("mode") is not None:
            try:
                os.chmod(target, int(req["mode"]))
            except EnvironmentError:
                pass  # Synology ACL shares often refuse chmod; not fatal
        if part and part != final:
            os.rename(part, final)
        return {}, b""

    def op_touch_meta(self, req, payload):
        _set_mtime(req["path"], int(req["mtime_ns"]))
        return {}, b""


# -- wire framing (shared with the client side) ------------------------------

def read_exact(f, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return bytes(buf)


def read_frame(f):
    jl, pl = HDR.unpack(read_exact(f, HDR.size))
    obj = json.loads(read_exact(f, jl).decode("utf-8"))
    payload = read_exact(f, pl) if pl else b""
    return obj, payload


def write_frame(f, obj, payload=b""):
    js = json.dumps(obj, separators=(",", ":"))
    if not isinstance(js, bytes):
        js = js.encode("utf-8")
    f.write(HDR.pack(len(js), len(payload)))
    f.write(js)
    if payload:
        f.write(payload)
    f.flush()


def _errmsg(ex):
    try:
        return "%s: %s" % (ex.strerror or ex, _text(ex.filename or ""))
    except Exception:
        return repr(ex)


def serve(inp, out):
    h = Handler()
    write_frame(out, {"ok": True, "ready": True, "version": AGENT_VERSION,
                      "python": platform.python_version(), "codecs": codec_info()})
    while True:
        try:
            req, payload = read_frame(inp)
        except EOFError:
            return
        try:
            resp, data = h.dispatch(req, payload)
            resp["ok"] = True
        except AgentError as ex:
            resp, data = {"ok": False, "error": str(ex), "errno": ex.errno}, b""
        except EnvironmentError as ex:
            resp, data = {"ok": False, "error": _errmsg(ex), "errno": ex.errno or 0}, b""
        except Exception as ex:  # report, keep serving
            resp, data = {"ok": False, "error": "%s: %s" % (type(ex).__name__, ex),
                          "errno": 0}, b""
        write_frame(out, resp, data)


if __name__ == "__main__" or __name__ == "rcagent":
    serve(getattr(sys.stdin, "buffer", sys.stdin), getattr(sys.stdout, "buffer", sys.stdout))
