import pytest

from rcommander.endpoint import RemoteEndpoint
from rcommander.sshauth import AuthError, ensure_auth

UNREACHABLE = ["-o", "ConnectTimeout=3", "-o", "BatchMode=yes"]


def test_never_mode_does_nothing():
    assert ensure_auth(RemoteEndpoint("host.invalid"), "never") is None


def test_bad_mode():
    with pytest.raises(AuthError):
        ensure_auth(RemoteEndpoint("host.invalid"), "sometimes")


def test_no_key_and_no_terminal_fails_fast_without_prompting():
    ep = RemoteEndpoint("host.invalid", ssh_opts=UNREACHABLE)
    with pytest.raises(AuthError) as ei:
        ensure_auth(ep, "auto", log=lambda m: None, interactive=False)
    assert "ssh-copy-id" in str(ei.value)
    assert ep.control_path is None


def test_control_path_goes_into_ssh_argv():
    ep = RemoteEndpoint("h")
    assert "ControlPath=none" in ep.ssh_argv()
    ep.control_path = "/tmp/rc-x/m"
    argv = ep.ssh_argv()
    assert "ControlPath=/tmp/rc-x/m" in argv and "ControlMaster=no" in argv
    assert "BatchMode=yes" in argv   # sessions still never prompt
