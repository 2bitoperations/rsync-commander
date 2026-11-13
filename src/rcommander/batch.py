"""Batch mode: run a TOML profile unattended (cron, systemd timers, ssh).

    rsync-commander batch profile.toml [--dry-run | --detect] [-v]

Profile reference (see examples/observatory.toml for a full one):

    dest = "nas:/volume1/backups/{hostname}"   # host:/path or a local path
    python = "auto"                            # remote interpreter (optional)
    ssh_mux = "auto"         # auto: keys -> parallel streams; else ONE password
                             # prompt and all streams share that connection

    [options]                # all optional; same meaning as the GUI/CLI options
    workers = 4
    block_size_mib = 4
    checksum = true          # false: skip files whose size+mtime match
    compression = "zstd"     # zstd | gzip | none
    level = 0                # 0 = codec default
    direct = true            # NAS-to-NAS: source box pushes straight to the dest
    server_side = true       # same box: copy on the box itself
    forward_agent = false    # let the source box use your ssh agent (opt-in)

    [[hook]]                 # shell commands, in order
    run = "dpkg-query -l > \"$RC_STAGE/reports/packages.txt\""
    when = "before"          # before (default) | after (gets $RC_STATUS = ok|failed)
    optional = true          # failure is a warning instead of aborting

    [[source]]
    path = ["~/a/*/Imager", "~/b/**/Imager*"]  # globs; first existing match wins
    all = false              # true: copy every match (each under `to`/its name)
    to = "images/main_imager"                  # path under dest (default: basename)
    filter = ["+ *Dark*.fit", "- *.fit"]       # rsync-style, first match wins
    include = []             # appended after `filter` as "+ ..." rules
    exclude = ["*Focus*"]    # appended after that as "- ..." rules
    follow_symlinks = true   # like rsync -L
    optional = false         # missing source: warning instead of error
    kind = "any"             # any | dir | file: what counts as a match

    [[archive]]              # packed locally into a tarball, then synced
    to = "snapshots/settings_{timestamp}.tar.xz"   # .tar.xz | .tar.gz | .tar.bz2 | .tar
    [[archive.add]]
    path = "~/Software Bisque/TheSkyX*"
    as = "TheSkyX_Config"    # name inside the archive (default: basename)
    exclude = ["*.fit", "*.fits"]
    optional = true

Templates usable in every string: {hostname} {user} {home} {date} {timestamp}
{stage} (a temp dir for hooks/archives, also exported as $RC_STAGE).
"""

import getpass
import glob
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from datetime import datetime
from pathlib import Path

from . import engine
from .endpoint import LocalEndpoint, RemoteEndpoint
from .filters import Filter
from .fmt import fmt_bytes, fmt_dur
from .sshauth import AuthError, ensure_auth


class ProfileError(Exception):
    pass


def _log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


class Profile:
    def __init__(self, path, overrides=None):
        self.path = Path(path)
        try:
            self.data = tomllib.loads(self.path.read_text())
        except (OSError, tomllib.TOMLDecodeError) as ex:
            raise ProfileError(f"{path}: {ex}") from None
        now = datetime.now()
        self.vars = {
            "hostname": socket.gethostname().split(".")[0],
            "user": getpass.getuser(),
            "home": str(Path.home()),
            "date": now.strftime("%Y-%m-%d"),
            "timestamp": now.strftime("%Y%m%d_%H%M%S"),
            "stage": "",
        }
        self.overrides = overrides or {}
        if "dest" not in self.data:
            raise ProfileError(f"{path}: 'dest' is required")

    def t(self, s):
        """Expand {vars} and ~."""
        try:
            s = s.format_map(self.vars)
        except (KeyError, ValueError) as ex:
            raise ProfileError(f"bad template {s!r}: {ex}") from None
        return os.path.expanduser(s)

    def options(self):
        o = dict(self.data.get("options", {}))
        o.update(self.overrides)
        known = {"workers", "block_size_mib", "checksum", "compression", "level",
                 "direct", "server_side", "forward_agent"}
        unknown = set(o) - known
        if unknown:
            raise ProfileError(f"unknown option(s): {', '.join(sorted(unknown))}")
        comp = o.get("compression", "zstd")
        if comp not in engine.COMPRESSIONS:
            raise ProfileError(f"compression must be one of {engine.COMPRESSIONS}")
        return engine.Options(workers=int(o.get("workers", 4)),
                              block_size=int(o.get("block_size_mib", 4)) << 20,
                              checksum=bool(o.get("checksum", True)),
                              compression=comp, level=int(o.get("level", 0)),
                              direct=bool(o.get("direct", True)),
                              server_side=bool(o.get("server_side", True)),
                              forward_agent=bool(o.get("forward_agent", False)))

    def dest(self):
        d = self.t(self.data["dest"])
        if ":" in d and not d.startswith(("/", ".")):
            host, path = d.split(":", 1)
            return RemoteEndpoint(host, python=self.data.get("python", "auto")), path
        return LocalEndpoint(), os.path.abspath(d)

    # -- sources ---------------------------------------------------------------
    def _matches(self, patterns, kind="any"):
        if isinstance(patterns, str):
            patterns = [patterns]
        for pat in patterns:
            pat = self.t(pat)
            hits = sorted(glob.glob(pat, recursive=True)) if glob.has_magic(pat) else (
                [pat] if os.path.lexists(pat) else [])
            hits = [h for h in hits if kind == "any" or
                    (kind == "dir" and os.path.isdir(h)) or (kind == "file" and os.path.isfile(h))]
            if hits:
                yield pat, hits

    def resolve_sources(self):
        """-> (list[engine.Source], warnings, errors)"""
        out, warns, errs = [], [], []
        for i, s in enumerate(self.data.get("source", [])):
            if "path" not in s:
                raise ProfileError(f"source #{i + 1}: 'path' is required")
            unknown = set(s) - {"path", "to", "filter", "include", "exclude",
                                "follow_symlinks", "optional", "all", "kind"}
            if unknown:
                raise ProfileError(f"source #{i + 1}: unknown key(s) {sorted(unknown)}")
            flt = Filter.build(s.get("filter", ()), s.get("include", ()), s.get("exclude", ()))
            found = next(self._matches(s["path"], s.get("kind", "any")), None)
            label = s.get("to") or s["path"]
            if not found:
                msg = f"source {label!r}: nothing matches {s['path']}"
                (warns if s.get("optional") else errs).append(msg)
                continue
            _pat, hits = found
            hits = hits if s.get("all") else hits[:1]
            for h in hits:
                to = s.get("to")
                if to is not None:
                    to = self.t(to)
                    if s.get("all"):
                        to = f"{to.rstrip('/')}/{os.path.basename(h.rstrip('/'))}"
                out.append(engine.Source(os.path.abspath(h), to=to, filter=flt or None,
                                         follow_symlinks=bool(s.get("follow_symlinks"))))
        return out, warns, errs

    # -- hooks -----------------------------------------------------------------
    def run_hooks(self, when, env_extra=None):
        for h in self.data.get("hook", []):
            if h.get("when", "before") != when:
                continue
            cmd = self.t(h["run"])
            env = dict(os.environ, RC_STAGE=self.vars["stage"], **(env_extra or {}))
            _log(f"hook ({when}): {cmd}")
            r = subprocess.run(cmd, shell=True, env=env, cwd=self.vars["stage"] or None)
            if r.returncode != 0:
                msg = f"hook failed (exit {r.returncode}): {cmd}"
                if h.get("optional"):
                    _log("WARNING " + msg)
                else:
                    raise ProfileError(msg)

    # -- archives --------------------------------------------------------------
    def build_archives(self, dry_run=False):
        out = []
        for i, a in enumerate(self.data.get("archive", [])):
            if "to" not in a:
                raise ProfileError(f"archive #{i + 1}: 'to' is required")
            to = self.t(a["to"])
            name = os.path.basename(to)
            mode = ("w:xz" if name.endswith(".xz") else "w:gz" if name.endswith(".gz")
                    else "w:bz2" if name.endswith(".bz2") else "w")
            items = []
            for j, add in enumerate(a.get("add", [])):
                found = next(self._matches(add["path"], add.get("kind", "any")), None)
                if not found:
                    msg = f"archive {name}: nothing matches {add['path']}"
                    if add.get("optional"):
                        _log("WARNING " + msg)
                        continue
                    raise ProfileError(msg)
                h = found[1][0]
                flt = Filter.build(add.get("filter", ()), add.get("include", ()),
                                   add.get("exclude", ()))
                items.append((h, add.get("as") or os.path.basename(h.rstrip("/")), flt))
            if dry_run:
                for h, arc, _f in items:
                    _log(f"archive {name}: would add {h} as {arc}")
                continue
            dest = Path(self.vars["stage"]) / "archives" / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            _log(f"archive: building {name}")
            t0 = time.time()
            with tarfile.open(dest, mode) as tar:
                for h, arc, flt in items:
                    def tfilter(ti, arc=arc, flt=flt):
                        sub = ti.name[len(arc):].lstrip("/") if ti.name.startswith(arc) else ti.name
                        if sub and flt and not flt.allows(sub, ti.isdir()):
                            return None
                        return ti
                    tar.add(h, arcname=arc, filter=tfilter)
            _log(f"archive: {name} {fmt_bytes(dest.stat().st_size)} in {fmt_dur(time.time() - t0)}")
            out.append(engine.Source(str(dest), to=to))
        return out


def _progress(job, interval, tty):
    last = 0
    while True:
        time.sleep(1 if tty else 2)
        s = job.snapshot()
        done = s.state in (engine.J_DONE, engine.J_FAILED, engine.J_CANCELLED)
        line = (f"[{engine.J_NAMES[s.state]}] files {s.files_done + s.files_skipped}/{s.files_total}"
                f"  sent {fmt_bytes(s.bytes_done)}/{fmt_bytes(s.bytes_needed)}"
                f"  identical {fmt_bytes(s.bytes_same)}  {fmt_bytes(s.rate)}/s"
                f"  ETA {'≥' if s.eta_lower_bound else ''}{fmt_dur(s.eta)}")
        if tty:
            print("\r" + line[:160].ljust(130), end="", flush=True)
        elif time.time() - last >= interval or done:
            _log(line)
            last = time.time()
        if done:
            if tty:
                print()
            return s


def run(profile_path, dry_run=False, detect=False, verbose=False, interval=30, overrides=None,
        ssh_mux="auto"):
    try:
        prof = Profile(profile_path, overrides)
        opts = prof.options()
        stage = tempfile.mkdtemp(prefix="rcommander-")
        prof.vars["stage"] = stage
        (Path(stage) / "reports").mkdir()
    except ProfileError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 2
    status = "failed"
    master = None
    try:
        dst_ep, dst = prof.dest()
        _log(f"profile {prof.path.name}: dest {dst_ep.label(dst)}")
        if not detect and not dst_ep.is_local:
            # Authenticate up front (at most one prompt), before slow local work.
            try:
                master = ensure_auth(dst_ep, prof.data.get("ssh_mux", ssh_mux), log=_log)
            except AuthError as ex:
                _log(f"ERROR {ex}")
                return 2
        if not detect and not dry_run:
            prof.run_hooks("before")
        sources, warns, errs = prof.resolve_sources()
        for w in warns:
            _log("WARNING " + w)
        for src in sources:
            _log(f"source {src.path}  ->  {src.to or os.path.basename(src.path)}"
                 + (f"  [{len(src.filter.rules)} filter rules]" if src.filter else "")
                 + ("  (following symlinks)" if src.follow_symlinks else ""))
        if errs:
            for e in errs:
                _log("ERROR " + e)
            return 2
        sources += prof.build_archives(dry_run=dry_run or detect)
        if detect:
            return 0
        if not sources:
            _log("nothing to do")
            return 0
        job = engine.Job(LocalEndpoint(), sources, dst_ep, dst, opts, dry_run=dry_run)
        job.start()
        if dry_run:
            job.wait()
            s = job.snapshot()
            by_top = {}
            for f in job.files:
                parts = f.rel.split("/")
                k = "/".join(parts[:2]) if len(parts) > 2 else parts[0]
                n, b = by_top.get(k, (0, 0))
                by_top[k] = (n + 1, b + f.size)
                if verbose:
                    print(f"  {fmt_bytes(f.size):>10}  {f.rel}")
            for k, (n, b) in sorted(by_top.items()):
                _log(f"would sync {k}: {n} files, {fmt_bytes(b)}")
            _log(f"dry run: {s.files_total} files, {fmt_bytes(s.bytes_total)}; "
                 f"{job.filtered_out} entries filtered out")
            for rel, e in s.errors:
                _log(f"ERROR {rel}: {e}")
            return 0 if not s.errors else 1
        _log(f"syncing {len(sources)} source(s) with {opts.workers} streams, "
             f"compression {opts.compression}, verify {'checksum' if opts.checksum else 'size+mtime'}")
        tty = sys.stdout.isatty()
        try:
            s = _progress(job, interval, tty)
        except KeyboardInterrupt:
            _log("interrupted: cancelling (partial files are kept for resume)")
            job.cancel()
            job.wait(15)
            s = job.snapshot()
        for n in s.notes:
            _log("note: " + n)
        for rel, e in s.errors:
            _log(f"ERROR {rel}: {e}")
        ratio = (f", wire {s.compressed_wire / s.compressed_logical * 100:.0f}% of data"
                 if s.compressed_logical else "")
        _log(f"{engine.J_NAMES[s.state]} in {fmt_dur(s.elapsed)}: {s.files_done} copied, "
             f"{s.files_skipped} up to date, {s.files_failed} failed; sent {fmt_bytes(s.bytes_done)}"
             f" ({s.compression}{ratio}); identical {fmt_bytes(s.bytes_same)}")
        status = "ok" if s.state == engine.J_DONE else "failed"
        return 0 if status == "ok" else 1
    except ProfileError as ex:
        _log(f"ERROR {ex}")
        return 2
    finally:
        if not detect and not dry_run:
            try:
                prof.run_hooks("after", {"RC_STATUS": status})
            except ProfileError as ex:
                _log(f"ERROR {ex}")
        if master:
            master.close()
        shutil.rmtree(stage, ignore_errors=True)
