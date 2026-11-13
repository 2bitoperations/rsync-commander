"""Route ssh's interactive prompts (password, key passphrase, new host key) to
our UI, for the GUI and TUI where there is no terminal to type into.

ssh runs $SSH_ASKPASS with the prompt text as argv[1] and reads the answer from
its stdout. Our askpass is a tiny script that runs `main()` below. main() relays
the prompt over a private Unix socket to the AskpassServer in the app, which
asks the user and sends back the answer. The socket lives in a 0700 temp dir
and is removed once authentication is done.

Prompt kinds sent to the UI's prompter(prompt, kind) -> str | None:
  "secret"   password / passphrase (input should be masked)
  "text"     free text, e.g. a "(yes/no/[fingerprint])" host-key question
  "confirm"  yes/no (SSH_ASKPASS_PROMPT=confirm); a truthy answer means yes
  "info"     a message to show (SSH_ASKPASS_PROMPT=none)
"""

import json
import os
import shlex
import shutil
import socket
import sys
import tempfile
import threading

SOCK_ENV = "RC_ASKPASS_SOCK"


def prompt_kind(prompt, env=os.environ):
    p = env.get("SSH_ASKPASS_PROMPT", "")
    if p == "confirm":
        return "confirm"
    if p == "none":
        return "info"
    if "(yes/no" in prompt:
        return "text"
    return "secret"


class AskpassServer:
    def __init__(self, prompter):
        self.prompter = prompter
        self.dir = tempfile.mkdtemp(prefix="rc-", dir="/tmp")   # 0700
        self.sock_path = os.path.join(self.dir, "a")
        self.script = os.path.join(self.dir, "askpass")
        self.prompts = 0
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.sock_path)
        self._srv.listen(4)
        self._closed = False
        if getattr(sys, "frozen", False):
            cmd = f"exec {shlex.quote(sys.executable)} askpass \"$@\""
        else:
            cmd = f"exec {shlex.quote(sys.executable)} -m rcommander.askpass \"$@\""
        with open(self.script, "w") as f:
            f.write("#!/bin/sh\n" + cmd + "\n")
        os.chmod(self.script, 0o700)
        threading.Thread(target=self._serve, daemon=True, name="askpass").start()

    def env(self):
        return {"SSH_ASKPASS": self.script, "SSH_ASKPASS_REQUIRE": "force",
                "DISPLAY": os.environ.get("DISPLAY") or ":0", SOCK_ENV: self.sock_path}

    def _serve(self):
        while not self._closed:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            with conn:
                try:
                    req = json.loads(conn.makefile("r").readline())
                    self.prompts += 1
                    ans = self.prompter(req["prompt"], req["kind"])
                except Exception:
                    ans = None
                try:
                    conn.sendall((json.dumps({"answer": ans}) + "\n").encode())
                except OSError:
                    pass

    def close(self):
        self._closed = True
        try:
            self._srv.close()
        except OSError:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)


def main(argv=None):
    """The askpass program itself (run by ssh)."""
    argv = sys.argv[1:] if argv is None else argv
    prompt = argv[0] if argv else ""
    kind = prompt_kind(prompt)
    path = os.environ.get(SOCK_ENV)
    if not path:
        return 1
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(path)
        s.sendall((json.dumps({"prompt": prompt, "kind": kind}) + "\n").encode())
        ans = json.loads(s.makefile("r").readline()).get("answer")
    except (OSError, ValueError):
        return 1
    if ans is None or (kind == "confirm" and not ans):
        return 1
    if kind not in ("confirm", "info"):
        sys.stdout.write(str(ans) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
