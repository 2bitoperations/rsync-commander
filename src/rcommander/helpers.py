"""Optional helpers installed on a remote host's ~/.rcommander.

install_zstd(): hosts whose Python lacks zstd (e.g. Synology's 3.8, or 2.7)
can use the `zstandard` package. We download the right manylinux wheel here
(the remote usually has no pip) and unpack it into ~/.rcommander/lib there.
The agent adds that dir to sys.path. Remove with `rm -rf ~/.rcommander`.
"""

import glob
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

from .endpoint import RemoteEndpoint, SshSession, same_box


def _ep(host):
    """A host name or an existing RemoteEndpoint (keeps its ssh options and
    shared master connection, so password-only hosts work from the GUI)."""
    return host if isinstance(host, RemoteEndpoint) else RemoteEndpoint(host)


def zstd_missing(src_ep, dst_ep, opts):
    """Remote endpoints a zstd transfer needs but that can't do zstd.
    Empty if zstd wasn't asked for, or no network leg would compress."""
    if opts.compression != "zstd":
        return []
    if opts.server_side and same_box(src_ep, dst_ep):
        return []                      # copied on the box: nothing on the wire
    out = []
    for ep in (src_ep, dst_ep):
        if isinstance(ep, RemoteEndpoint) and ep not in out:
            if "zstd" not in ep.browse_call(lambda s: s.codecs):
                out.append(ep)
    return out


def install_hint(eps):
    hosts = [e.host for e in eps]
    return (f"zstd compression was requested, but {', '.join(hosts)} "
            f"{'has' if len(hosts) == 1 else 'have'} no zstd support. Install it with: "
            + "; ".join(f"rsync-commander install-zstd {h}" for h in hosts)
            + ' (or use compression "gzip")')


def probe(host):
    s = SshSession(_ep(host))
    try:
        hello = s.call({"op": "hello"})[0]
    finally:
        s.close()
    return hello


def _remote(host, cmd, data=None):
    ep = _ep(host)
    r = subprocess.run(ep.ssh_argv() + [ep.host, cmd], input=data, capture_output=True)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"{ep.host}: `{cmd[:60]}` failed: {err[-300:] or r.returncode}")
    return r.stdout


def install_zstd(host, log=print):
    """Returns True if the host can do zstd afterwards. host: name or endpoint."""
    ep = _ep(host)
    host = ep.host
    info = _remote(ep, "uname -m; uname -s; "
                   "for p in python3 python python2; do command -v $p >/dev/null 2>&1 && "
                   "exec $p -c 'import sys;print(\"%d%d\" % sys.version_info[:2])'; done"
                   ).decode().split()
    arch, system, pyver = info[0], info[1], info[2]
    if system != "Linux":
        raise RuntimeError(f"{host}: only Linux remotes are supported here ({system})")
    log(f"{host}: {system} {arch}, python {pyver[0]}.{pyver[1:]}")
    plats = [f"manylinux_2_17_{arch}", f"manylinux2014_{arch}", f"manylinux2010_{arch}",
             f"manylinux1_{arch}"]
    with tempfile.TemporaryDirectory() as tmp:
        # `uv run` exports $UV; a Dock-launched GUI may not have uv on PATH
        uv = os.environ.get("UV") or shutil.which("uv")
        if uv:
            pip = [uv, "run", "--no-project", "--with", "pip", "python", "-m", "pip"]
        else:
            pip = [sys.executable, "-m", "pip"]
        cmd = pip + ["download", "zstandard", "--only-binary=:all:", "--no-deps",
                     "--python-version", pyver, "--implementation", "cp", "-d", tmp, "-q"]
        if pyver == "27":
            cmd += ["--abi", "cp27mu"]
        for p in plats:
            cmd += ["--platform", p]
        log("downloading wheel: " + " ".join(cmd[len(pip):]))
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("wheel download failed: " + (r.stderr.strip()[-300:] or "?"))
        wheels = glob.glob(os.path.join(tmp, "zstandard-*.whl"))
        if not wheels:
            raise RuntimeError(f"no zstandard wheel for {arch} / python {pyver}")
        log(f"got {os.path.basename(wheels[0])}")
        buf = io.BytesIO()
        with zipfile.ZipFile(wheels[0]) as z, tarfile.open(fileobj=buf, mode="w:gz") as t:
            for zi in z.infolist():
                if zi.is_dir():
                    continue
                ti = tarfile.TarInfo(zi.filename)
                data = z.read(zi)
                ti.size = len(data)
                ti.mode = 0o644
                t.addfile(ti, io.BytesIO(data))
        _remote(ep, "rm -rf ~/.rcommander/lib/zstandard ~/.rcommander/lib/zstandard-*; "
                "mkdir -p ~/.rcommander/lib && tar -xzf - -C ~/.rcommander/lib",
                buf.getvalue())
    hello = probe(ep)
    if hasattr(ep, "reset_browse"):
        ep.reset_browse()              # its cached session predates the install
    ok = "zstd" in hello.get("codecs", {})
    log(f"{host}: codecs now {hello.get('codecs')}")
    return ok
