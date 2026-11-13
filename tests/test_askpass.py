"""The askpass relay: ssh runs our helper script, which asks the app."""
import os
import subprocess

from rcommander.askpass import AskpassServer, prompt_kind


def run_helper(srv, prompt, extra_env=None):
    env = {**os.environ, **srv.env(), **(extra_env or {})}
    return subprocess.run([srv.script, prompt], env=env, capture_output=True, text=True,
                          timeout=30)


def test_secret_round_trip():
    seen = []
    srv = AskpassServer(lambda p, k: (seen.append((p, k)), "s3cret")[1])
    try:
        r = run_helper(srv, "me@nas's password: ")
    finally:
        srv.close()
    assert r.returncode == 0 and r.stdout == "s3cret\n"
    assert seen == [("me@nas's password: ", "secret")]
    assert not os.path.exists(srv.dir)          # socket dir removed


def test_cancel_and_confirm():
    srv = AskpassServer(lambda p, k: None)
    try:
        assert run_helper(srv, "pw: ").returncode == 1            # cancelled
    finally:
        srv.close()
    srv = AskpassServer(lambda p, k: "yes" if k == "confirm" else None)
    try:
        r = run_helper(srv, "Allow use of key?", {"SSH_ASKPASS_PROMPT": "confirm"})
        assert r.returncode == 0 and r.stdout == ""
    finally:
        srv.close()


def test_prompt_kinds():
    assert prompt_kind("x's password: ", {}) == "secret"
    assert prompt_kind("Enter passphrase for key '/k': ", {}) == "secret"
    assert prompt_kind("Are you sure you want to continue connecting (yes/no/[fingerprint])? ",
                       {}) == "text"
    assert prompt_kind("Confirm?", {"SSH_ASKPASS_PROMPT": "confirm"}) == "confirm"


def test_no_server_means_failure():
    env = {k: v for k, v in os.environ.items() if k != "RC_ASKPASS_SOCK"}
    from rcommander import askpass
    old = os.environ.pop("RC_ASKPASS_SOCK", None)
    try:
        assert askpass.main(["pw: "]) == 1
    finally:
        if old:
            os.environ["RC_ASKPASS_SOCK"] = old
    assert env is not None
