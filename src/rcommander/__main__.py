"""Entry point.

    rsync-commander                       # GUI
    rsync-commander copy SRC... DST       # headless; host:/path for remotes
"""

import argparse
import sys
import time

from . import engine
from .endpoint import LocalEndpoint, RemoteEndpoint
from .fmt import fmt_bytes, fmt_dur


PYTHON = "auto"


def parse_loc(s, cache):
    """'host:/path' -> (RemoteEndpoint, path); '/path' -> (LocalEndpoint, path)."""
    if ":" in s and not s.startswith("/") and not s.startswith("."):
        host, path = s.split(":", 1)
        if host not in cache:
            cache[host] = RemoteEndpoint(host, python=PYTHON)
        return cache[host], path or "."
    if "local" not in cache:
        cache["local"] = LocalEndpoint()
    import os
    return cache["local"], os.path.abspath(s)


def cmd_copy(a):
    global PYTHON
    PYTHON = a.python
    cache = {}
    srcs = [parse_loc(s, cache) for s in a.src]
    if len({id(e) for e, _ in srcs}) != 1:
        sys.exit("all sources must be on the same endpoint")
    dst_ep, dst = parse_loc(a.dst, cache)
    opts = engine.Options(block_size=a.block_size << 20, workers=a.workers,
                          checksum=not a.quick, compression=a.compress, level=a.level)
    job = engine.Job(srcs[0][0], [p for _, p in srcs], dst_ep, dst, opts)
    job.start()
    try:
        while True:
            time.sleep(1)
            s = job.snapshot()
            eta = "?" if s.eta < 0 else fmt_dur(s.eta)
            line = (f"[{engine.J_NAMES[s.state]}] files {s.files_done + s.files_skipped}"
                    f"/{s.files_total}  sent {fmt_bytes(s.bytes_done)}/{fmt_bytes(s.bytes_needed)}"
                    f"  same {fmt_bytes(s.bytes_same)}  {fmt_bytes(s.rate)}/s"
                    f" (wire {fmt_bytes(s.wire_rate)}/s)  ETA {'≥' if s.eta_lower_bound else ''}{eta}")
            print("\r" + line[:200].ljust(120), end="", flush=True)
            if a.verbose:
                for w in s.workers:
                    print(f"\n   w{w.idx}: {w.desc}", end="")
            if s.state in (engine.J_DONE, engine.J_FAILED, engine.J_CANCELLED):
                break
    except KeyboardInterrupt:
        job.cancel()
        job.wait(10)
    print()
    s = job.snapshot()
    for n in s.notes:
        print(f"note: {n}", file=sys.stderr)
    for rel, err in s.errors:
        print(f"ERROR {rel}: {err}", file=sys.stderr)
    print(f"{engine.J_NAMES[s.state]} in {fmt_dur(s.elapsed)}: {s.files_done} copied, "
          f"{s.files_skipped} up to date, {s.files_failed} failed; "
          f"sent {fmt_bytes(s.bytes_done)} (wire {fmt_bytes(s.wire_bytes)}), "
          f"verified same {fmt_bytes(s.bytes_same)}; compression {s.compression}"
          + (f" (wire {s.compressed_wire / s.compressed_logical * 100:.0f}% of data)"
             if s.compressed_logical else ""))
    return 0 if s.state == engine.J_DONE else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="rsync-commander")
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("copy", help="copy/sync items into a destination directory")
    c.add_argument("src", nargs="+")
    c.add_argument("dst")
    c.add_argument("-j", "--workers", type=int, default=4, help="parallel streams")
    c.add_argument("-b", "--block-size", type=int, default=4, help="block size, MiB")
    c.add_argument("-q", "--quick", action="store_true",
                   help="skip files whose size and mtime match (no checksum)")
    c.add_argument("-z", "--compress", choices=engine.COMPRESSIONS, default="zstd",
                   help="wire compression (default zstd; falls back to gzip if a "
                        "host lacks zstd)")
    c.add_argument("-l", "--level", type=int, default=0,
                   help="compression level (default: gzip 1, zstd 3)")
    c.add_argument("-v", "--verbose", action="store_true")
    c.add_argument("--python", default="auto",
                   help="remote interpreter (default: first of python3, python, python2)")
    pr = sub.add_parser("probe", help="show a host's python and codecs")
    pr.add_argument("host")
    iz = sub.add_parser("install-zstd",
                        help="put the zstandard wheel in HOST:~/.rcommander/lib")
    iz.add_argument("host")
    a = p.parse_args(argv)
    if a.cmd == "copy":
        sys.exit(cmd_copy(a))
    if a.cmd == "probe":
        from .helpers import probe
        for k, v in probe(a.host).items():
            print(f"{k:10} {v}")
        return
    if a.cmd == "install-zstd":
        from .helpers import install_zstd
        sys.exit(0 if install_zstd(a.host) else 1)
    from .gui.app import run
    sys.exit(run())


if __name__ == "__main__":
    main()
