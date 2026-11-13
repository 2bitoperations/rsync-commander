"""Optional helpers installed on a remote host's ~/.rcommander.

install_zstd(): hosts whose Python lacks zstd (e.g. Synology's 3.8, or 2.7)
can use the `zstandard` package. We download the right manylinux wheel here
(the remote usually has no pip) and unpack it into ~/.rcommander/lib there.
The agent adds that dir to sys.path. Remove with `rm -rf ~/.rcommander`.
"""

import glob
import io
import os
import subprocess
import tarfile
import tempfile
import zipfile

from .endpoint import RemoteEndpoint, SshSession


def probe(host):
    s = SshSession(RemoteEndpoint(host))
    try:
        hello = s.call({"op": "hello"})[0]
    finally:
        s.close()
    return hello


def _remote(host, cmd, data=None):
    ep = RemoteEndpoint(host)
    return subprocess.run(ep.ssh_argv() + [host, cmd], input=data,
                          capture_output=True, check=True).stdout


def install_zstd(host, log=print):
    info = _remote(host, "uname -m; uname -s; "
                   "for p in python3 python python2; do command -v $p >/dev/null 2>&1 && "
                   "exec $p -c 'import sys;print(\"%d%d\" % sys.version_info[:2])'; done"
                   ).decode().split()
    arch, system, pyver = info[0], info[1], info[2]
    if system != "Linux":
        raise SystemExit(f"{host}: only Linux remotes are supported here ({system})")
    log(f"{host}: {system} {arch}, python {pyver[0]}.{pyver[1:]}")
    plats = [f"manylinux_2_17_{arch}", f"manylinux2014_{arch}", f"manylinux2010_{arch}",
             f"manylinux1_{arch}"]
    with tempfile.TemporaryDirectory() as tmp:
        cmd = ["uv", "run", "--no-project", "--with", "pip", "python", "-m", "pip",
               "download", "zstandard", "--only-binary=:all:", "--no-deps",
               "--python-version", pyver, "--implementation", "cp", "-d", tmp, "-q"]
        if pyver == "27":
            cmd += ["--abi", "cp27mu"]
        for p in plats:
            cmd += ["--platform", p]
        log("downloading wheel: " + " ".join(cmd[7:]))
        subprocess.run(cmd, check=True)
        wheels = glob.glob(os.path.join(tmp, "zstandard-*.whl"))
        if not wheels:
            raise SystemExit("no compatible zstandard wheel found")
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
        _remote(host, "rm -rf ~/.rcommander/lib/zstandard ~/.rcommander/lib/zstandard-*; "
                "mkdir -p ~/.rcommander/lib && tar -xzf - -C ~/.rcommander/lib",
                buf.getvalue())
    hello = probe(host)
    ok = "zstd" in hello.get("codecs", {})
    log(f"{host}: codecs now {hello.get('codecs')}")
    return ok
