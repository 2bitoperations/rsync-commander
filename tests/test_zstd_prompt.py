"""zstd requested but a remote end lacks it: batch refuses (with the install
command), the TUI/GUI offer to install the helper or fall back to gzip."""
import asyncio
import dataclasses

import pytest

from rcommander import batch, engine, helpers
from rcommander.endpoint import LocalEndpoint

from test_engine import PY38, PYCUR, FakeRemote

need38 = pytest.mark.skipif(not PY38, reason="needs a python3.8 without zstd")


class NoZstd(FakeRemote):
    """A host whose agent reports no zstd (the agent would otherwise find this
    Mac's Homebrew zstd binary)."""

    def __init__(self, host="oldbox"):
        super().__init__(PY38, host=host)

    @staticmethod
    def _strip(s):
        s.codecs = {k: v for k, v in s.codecs.items() if k != "zstd"}
        return s

    def session(self):
        return self._strip(super().session())

    def browse(self):
        return self._strip(super().browse())


@need38
def test_zstd_missing():
    old, new, loc = NoZstd(), FakeRemote(PYCUR, host="newbox"), LocalEndpoint()
    try:
        z = engine.Options(compression="zstd")
        assert helpers.zstd_missing(loc, old, z) == [old]
        assert helpers.zstd_missing(old, loc, z) == [old]
        assert helpers.zstd_missing(loc, new, z) == []
        assert helpers.zstd_missing(new, old, z) == [old]
        assert helpers.zstd_missing(loc, old, engine.Options(compression="gzip")) == []
        # same box, copied on the box: no wire, no codec needed
        assert helpers.zstd_missing(old, old, z) == []
        hint = helpers.install_hint([old])
        assert "rsync-commander install-zstd oldbox" in hint and "gzip" in hint
    finally:
        for e in (old, new):
            e.close()


def _profile(tmp_path, compression):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "a.fits").write_bytes(b"x" * 5000)
    p = tmp_path / "p.toml"
    p.write_text(f'dest = "{tmp_path}/dst"\n[[source]]\npath = "{src}"\n'
                 f'[options]\ncompression = "{compression}"\n')
    return p


@need38
@pytest.mark.parametrize("compression", ["zstd", "gzip"])
def test_batch_refuses_missing_zstd(tmp_path, monkeypatch, capsys, compression):
    rem = NoZstd()
    monkeypatch.setattr(batch.Profile, "dest", lambda self: (rem, str(tmp_path / "dst")))
    monkeypatch.setattr(batch, "ensure_auth", lambda *a, **k: None)
    rc = batch.run(_profile(tmp_path, compression))
    out = capsys.readouterr().out
    if compression == "zstd":
        assert rc == 2
        assert "install-zstd oldbox" in out
        assert not (tmp_path / "dst").exists()       # nothing ran, no hooks
    else:
        assert rc == 0, out
        assert (tmp_path / "dst" / "src" / "a.fits").exists()
    rem.close()


@need38
@pytest.mark.parametrize("choice", ["gzip", "install", "cancel"])
def test_tui_zstd_prompt(tmp_path, monkeypatch, choice):
    from rcommander.tui import Choice, Commander
    rem = NoZstd()
    installed = []
    monkeypatch.setattr(helpers, "install_zstd",
                        lambda ep, log=None: installed.append(ep.host) or True)
    started = []
    z = engine.Options(compression="zstd")

    async def run():
        app = Commander(str(tmp_path), str(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            app._ensure_zstd(LocalEndpoint(), rem, z, started.append)
            for _ in range(50):
                await pilot.pause(0.1)
                if isinstance(app.screen, Choice):
                    break
            assert isinstance(app.screen, Choice)
            assert "oldbox" in app.screen.text
            await pilot.click(f"#{choice}")
            for _ in range(30):
                await pilot.pause(0.1)
                if started or choice == "cancel":
                    break
            if choice == "gzip":
                # remembered for this session: no second prompt
                app._ensure_zstd(LocalEndpoint(), rem, z, started.append)
                for _ in range(50):
                    await pilot.pause(0.1)
                    if len(started) == 2:
                        break
    asyncio.run(run())
    rem.close()
    if choice == "gzip":
        gz = dataclasses.replace(z, compression="gzip")
        assert started == [gz, gz] and not installed
    elif choice == "install":
        assert installed == ["oldbox"] and started == [z]
    else:
        assert not started and not installed
