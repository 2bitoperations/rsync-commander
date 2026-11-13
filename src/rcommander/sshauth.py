"""Authenticate at most once per host, even with N parallel sessions.

Sessions run ssh with BatchMode=yes: they can't answer a password prompt, and
N of them would ask N times anyway. So before a run we:

  1. probe key auth non-interactively (`ssh -o BatchMode=yes host true`):
     if it works there are zero prompts and every session gets its own TCP
     stream (fastest on high-latency links);
  2. otherwise open ONE master connection (`ssh -M -N -f`), which prompts on
     the terminal exactly once (password, key passphrase, new host key), and
     point every session at its control socket. All sessions then multiplex
     over that one authenticated connection, so throughput is that of a
     single TCP stream. Set up a key (ssh-copy-id) to get parallel streams back.

mode: "auto" (above), "always" (use a master even if keys work), "never".
"""

import os
import shutil
import subprocess
import sys
import tempfile

MODES = ("auto", "always", "never")


class AuthError(Exception):
    pass


def _base(ep):
    return ["ssh", "-o", "ConnectTimeout=20", *ep.ssh_opts]


def key_auth_works(ep):
    r = subprocess.run(_base(ep) + ["-o", "BatchMode=yes", "-o", "ControlMaster=no",
                                    "-o", "ControlPath=none", ep.host, "true"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True)
    return r.returncode == 0, r.stderr.strip()


class SshMaster:
    def __init__(self, ep):
        self.ep = ep
        # Unix socket paths are limited (~104 bytes on macOS): keep it short.
        self.dir = tempfile.mkdtemp(prefix="rc-", dir="/tmp")
        self.path = os.path.join(self.dir, "m")

    def _ctl(self, *args):
        return subprocess.run(_base(self.ep) + ["-o", f"ControlPath={self.path}", *args,
                                                self.ep.host],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True)

    def start(self, prompter=None):
        """Without a prompter, ssh asks on the terminal. With one (GUI/TUI), every
        ssh prompt goes to prompter(prompt, kind) through an askpass relay."""
        argv = _base(self.ep) + [
            "-M", "-N", "-f", "-o", "ControlMaster=yes", "-o", f"ControlPath={self.path}",
            "-o", "ControlPersist=no", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4", self.ep.host]
        relay = None
        if prompter is None:
            r = subprocess.run(argv)                    # the single terminal prompt
            err = ""
        else:
            from .askpass import AskpassServer
            relay = AskpassServer(prompter)
            # stderr to a file, not a pipe: the backgrounded (-f) master keeps it
            # open, and subprocess.run would wait for EOF until the master exits.
            with tempfile.TemporaryFile("w+") as errf:
                try:
                    r = subprocess.run(argv, env={**os.environ, **relay.env()},
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=errf, start_new_session=True)  # no tty: askpass
                finally:
                    errf.seek(0)
                    err = errf.read().strip()
                    self.prompts = relay.prompts
                    relay.close()
        if r.returncode != 0 or self._ctl("-O", "check").returncode != 0:
            self.close()
            raise AuthError(f"could not open an ssh connection to {self.ep.host}"
                            + (f": {err.splitlines()[-1]}" if err else ""))
        self.ep.control_path = self.path
        return self

    def close(self):
        if os.path.exists(self.path):
            self._ctl("-O", "exit")
        self.ep.control_path = None
        shutil.rmtree(self.dir, ignore_errors=True)


def ensure_auth(ep, mode="auto", log=print, interactive=None, prompter=None):
    """Make `ep` usable by BatchMode sessions. Returns an SshMaster to close()
    when done, or None if key auth works (or mode is "never").
    prompter(prompt, kind) -> answer: ask through the UI instead of a terminal."""
    if mode not in MODES:
        raise AuthError(f"ssh mux mode must be one of {MODES}")
    if mode == "never":
        return None
    if mode == "auto":
        ok, err = key_auth_works(ep)
        if ok:
            log(f"ssh {ep.host}: key auth OK, using parallel connections")
            return None
        if interactive is None:
            interactive = prompter is not None or sys.stdin.isatty()
        if not interactive:
            raise AuthError(
                f"ssh {ep.host}: no working key ({err or 'auth failed'}) and no terminal to "
                f"ask for a password. Set up a key (ssh-copy-id {ep.host}) or run interactively.")
        log(f"ssh {ep.host}: no working key; opening one connection (you'll be asked once). "
            f"All streams will share it. Tip: ssh-copy-id {ep.host} for parallel streams.")
    return SshMaster(ep).start(prompter)
