"""Entry point.

    rsync-commander                       # GUI
    rsync-commander tui [LEFT] [RIGHT]    # two-panel terminal UI
    rsync-commander batch PROFILE.toml    # unattended profile run (cron/systemd)
    rsync-commander copy SRC... DST       # one-shot; host:/path for remotes
    rsync-commander install-app           # launcher with name + icon
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
                          checksum=not a.quick, compression=a.compress, level=a.level,
                          direct=not a.no_direct, server_side=not a.no_server_side,
                          forward_agent=a.forward_agent)
    from .sshauth import AuthError, ensure_auth
    masters = []
    try:
        for ep in {id(e): e for e in [srcs[0][0], dst_ep]}.values():
            if not ep.is_local:
                m = ensure_auth(ep, a.ssh_mux, log=lambda m: print(m, file=sys.stderr))
                if m:
                    masters.append(m)
    except AuthError as ex:
        for m in masters:
            m.close()
        sys.exit(f"error: {ex}")
    try:
        return _copy_run(a, srcs, dst_ep, dst, opts)
    finally:
        for m in masters:
            m.close()


def _copy_run(a, srcs, dst_ep, dst, opts):
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
        print(f"note: {n}")
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
    from . import __version__
    p = argparse.ArgumentParser(prog="rsync-commander")
    p.add_argument("--version", action="version", version=f"rsync-commander {__version__}")
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
    c.add_argument("--ssh-mux", choices=("auto", "always", "never"), default="auto",
                   help="auth once per host (see `batch --help`)")
    c.add_argument("--no-direct", action="store_true",
                   help="NAS-to-NAS: relay through this machine instead of box-to-box")
    c.add_argument("--no-server-side", action="store_true",
                   help="same box: relay through this machine instead of copying on the box")
    c.add_argument("--forward-agent", action="store_true",
                   help="box-to-box: forward your ssh agent to the source box so it can "
                        "log in to the destination (root on that box could use it meanwhile)")
    c.add_argument("--python", default="auto",
                   help="remote interpreter (default: first of python3, python, python2)")
    pr = sub.add_parser("probe", help="show a host's python and codecs")
    pr.add_argument("host")
    iz = sub.add_parser("install-zstd",
                        help="put the zstandard wheel in HOST:~/.rcommander/lib")
    iz.add_argument("host")
    t = sub.add_parser("tui", help="two-panel commander in the terminal")
    t.add_argument("left", nargs="?", help="path or host:/path for the left panel")
    t.add_argument("right", nargs="?", help="path or host:/path for the right panel")
    b = sub.add_parser("batch", help="run a TOML sync profile unattended")
    b.add_argument("profile")
    b.add_argument("-n", "--dry-run", action="store_true",
                   help="resolve sources and list what would be synced; transfer nothing")
    b.add_argument("--detect", action="store_true",
                   help="only show which paths the profile's globs resolve to")
    b.add_argument("-v", "--verbose", action="store_true", help="with --dry-run: list files")
    b.add_argument("--interval", type=int, default=30,
                   help="seconds between progress log lines when not on a terminal")
    b.add_argument("-o", "--option", action="append", default=[], metavar="KEY=VALUE",
                   help="override an [options] value, e.g. -o workers=2 -o compression=gzip")
    b.add_argument("--ssh-mux", choices=("auto", "always", "never"), default="auto",
                   help="auto: use keys if they work (parallel connections), else ask "
                        "for the password once and share one connection")
    ic = sub.add_parser("install-cli", help="put `rsync-commander` on your PATH "
                        "(symlink to this binary, default ~/.local/bin)")
    ic.add_argument("--dir", default="~/.local/bin")
    sub.add_parser("install-app", help="install a launcher with name + icon "
                   "(~/Applications on macOS, a .desktop file on Linux)")
    if argv is None and len(sys.argv) > 1 and sys.argv[1] == "askpass":
        # ssh's SSH_ASKPASS helper (frozen builds re-run this binary)
        from .askpass import main as askpass_main
        sys.exit(askpass_main(sys.argv[2:]))
    a = p.parse_args(argv)
    if a.cmd == "tui":
        from .tui import run as run_tui
        run_tui(a.left, a.right)
        return
    if a.cmd == "batch":
        from .batch import run as run_batch
        ov = {}
        for kv in a.option:
            k, _, v = kv.partition("=")
            ov[k] = {"true": True, "false": False}.get(v.lower(), int(v) if v.isdigit() else v)
        sys.exit(run_batch(a.profile, dry_run=a.dry_run, detect=a.detect, verbose=a.verbose,
                           interval=a.interval, overrides=ov, ssh_mux=a.ssh_mux))
    if a.cmd == "install-cli":
        from .install import install_cli
        print(install_cli(a.dir))
        return
    if a.cmd == "install-app":
        from .install import install_app
        print(f"installed {install_app()}")
        return
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
    try:
        from .gui.app import run
    except ImportError as ex:
        sys.exit(f"GUI unavailable ({ex}).\n"
                 "Install it with `uv sync --group gui` (or pip install 'rsync-commander[gui]'),\n"
                 "or use the terminal UI: rsync-commander tui")
    sys.exit(run())


if __name__ == "__main__":
    main()
