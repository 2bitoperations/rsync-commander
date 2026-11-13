import os
import tarfile
from pathlib import Path

import pytest

from rcommander import batch, engine
from rcommander.endpoint import LocalEndpoint
from rcommander.filters import Filter

ROOT = Path(__file__).resolve().parents[1]


# -- filters -------------------------------------------------------------------

@pytest.mark.parametrize("rules,path,is_dir,expect", [
    (["- *.fit"], "a.fit", False, False),
    (["- *.fit"], "x/y/a.fit", False, False),          # no slash: any depth
    (["- *.fit"], "a.fits", False, True),
    (["- *Focus*"], "2026-11-07/M42 @Focus3", True, False),
    (["+ *[Dd]ark*.fit", "- *.fit"], "d/Dark_001.fit", False, True),
    (["+ *[Dd]ark*.fit", "- *.fit"], "d/dark_001.fit", False, True),
    (["+ *[Dd]ark*.fit", "- *.fit"], "d/Light_001.fit", False, False),
    (["- /top.txt"], "top.txt", False, False),          # anchored
    (["- /top.txt"], "sub/top.txt", False, True),
    (["- cache/"], "a/cache", True, False),             # dir-only
    (["- cache/"], "a/cache", False, True),
    (["- a/**/z"], "a/b/c/z", False, False),
    (["- a/**/z"], "a/z", False, False),                # **/ matches zero dirs
    (["- a/*/z"], "a/b/c/z", False, True),              # * does not cross /
    ([], "anything", False, True),
])
def test_filter_rules(rules, path, is_dir, expect):
    assert Filter.build(rules).allows(path, is_dir) is expect


def test_filter_prunes_excluded_dirs():
    f = Filter.build(exclude=["*Focus*"], include=[])
    ents = [{"rel": "imgs", "type": "d"}, {"rel": "imgs/@Focus1", "type": "d"},
            {"rel": "imgs/@Focus1/Dark.fit", "type": "f"}, {"rel": "imgs/L.fit", "type": "f"}]
    assert [e["rel"] for e in f.apply(ents, "imgs")] == ["imgs", "imgs/L.fit"]
    # rsync semantics: once a directory is excluded nothing below it is considered,
    # even with an include rule listed first...
    f2 = Filter.build(["+ Dark.fit", "- *Focus*"])
    assert "imgs/@Focus1/Dark.fit" not in [e["rel"] for e in f2.apply(ents, "imgs")]
    # ...unless directories are explicitly kept, as in rsync's "+ */"
    f3 = Filter.build(["+ */", "+ Dark.fit", "- *Focus*"])
    assert "imgs/@Focus1/Dark.fit" in [e["rel"] for e in f3.apply(ents, "imgs")]


# -- engine: Source mapping + symlinks ---------------------------------------------

def _w(p, data=b"x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_source_to_and_follow_symlinks(tmp_path):
    real = tmp_path / "real"
    _w(real / "a.fit", b"A" * 1000)
    _w(real / "sub" / "b.fit", b"B" * 10)
    src = tmp_path / "src"
    src.mkdir()
    (src / "linked").symlink_to(real)
    (src / "loop").symlink_to(src)              # must not recurse forever
    _w(src / "plain.txt", b"p")
    dst = tmp_path / "dst"
    j = engine.Job(LocalEndpoint(), [engine.Source(str(src), to="x/y", follow_symlinks=True)],
                   LocalEndpoint(), str(dst), engine.Options(workers=2, block_size=1 << 16))
    j.start(); j.wait(30)
    assert j.snapshot().state == engine.J_DONE, j.errors
    assert (dst / "x/y/linked/a.fit").read_bytes() == b"A" * 1000
    assert (dst / "x/y/linked/sub/b.fit").exists()
    assert (dst / "x/y/plain.txt").exists()
    assert not (dst / "x/y/loop").exists()
    # without follow_symlinks the link is skipped
    dst2 = tmp_path / "dst2"
    j = engine.Job(LocalEndpoint(), [engine.Source(str(src))], LocalEndpoint(), str(dst2))
    j.start(); j.wait(30)
    assert (dst2 / "src/plain.txt").exists() and not (dst2 / "src/linked").exists()


# -- batch: the observatory profile end to end -------------------------------------------

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    tsx = home / "Software Bisque" / "TheSkyX Professional Edition"
    img = tsx / "Camera AutoSave" / "Imager" / "2026-11-07"
    _w(img / "Light_M42_001.fit", b"L" * 5000)
    _w(img / "Light_M42 @Focus3_001.fit", b"F")
    _w(img / "Automated Pointing Run" / "p1.fit", b"P")
    _w(img / "Closed Loop Slews" / "s1.fit", b"S")
    _w(img / "Guide_star.fit", b"G")
    gd = tsx / "Camera AutoSave" / "Guider" / "2026-11-07"
    _w(gd / "Dark_001.fit", b"D")
    _w(gd / "guide_0001.fit", b"g")                       # manual capture: kept
    _w(gd / "bad" / "Bias_001.fit", b"b")                  # in a "bad" dir: dropped
    _w(gd / "2026-11-07-TSX-CLPA Slew-Light-1.fit", b"c")  # CLPA slew: dropped
    _w(tsx / "Image Calibration" / "Imager Dark Master.fit", b"M")
    _w(tsx / "Logs" / "tsx.log", b"log")
    _w(tsx / "TPoint" / "model.dat", b"tp")
    _w(tsx / "Settings" / "prefs.ini", b"ini")
    _w(tsx / "Settings" / "stray.fits", b"no")
    monkeypatch.setenv("HOME", str(home))
    return home, tsx


def _local_profile(tmp_path, dest):
    text = (ROOT / "examples" / "observatory.toml").read_text()
    text = text.replace('dest = "nas:/volume1/backups/observatory/{hostname}"',
                        f'dest = "{dest}/{{hostname}}"')
    text = text.replace("/etc/udev/rules.d", str(tmp_path / "no-udev"))
    p = tmp_path / "obs.toml"
    p.write_text(text)
    return p


def test_observatory_profile(tmp_path, fake_home, capsys):
    home, tsx = fake_home
    dest = tmp_path / "nas"
    prof = _local_profile(tmp_path, dest)
    assert batch.run(prof, detect=True) == 0
    assert batch.run(prof, dry_run=True) == 0
    assert not dest.exists() or not any(dest.rglob("*.fit"))
    assert batch.run(prof, interval=1) == 0, capsys.readouterr().out
    hosts = list(dest.iterdir())
    assert len(hosts) == 1
    out = hosts[0]
    got = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
    snaps = [g for g in got if g.startswith("snapshots/")]
    assert len(snaps) == 1 and snaps[0].endswith(".tar.xz")
    assert [g for g in got if not g.startswith("snapshots/")] == [
        "calibration/Image Calibration/Imager Dark Master.fit",
        "images/guider/2026-11-07/Dark_001.fit",
        "images/guider/2026-11-07/guide_0001.fit",
        "images/main_imager/2026-11-07/Light_M42_001.fit",
        "logs/Logs/tsx.log",
        "logs/TPoint/model.dat",
    ]
    with tarfile.open(out / snaps[0]) as t:
        names = t.getnames()
    assert "TheSkyX_Config/Settings/prefs.ini" in names
    assert not any(n.endswith((".fit", ".fits")) for n in names)
    assert not any("Camera AutoSave" in n for n in names)
    # second run: everything identical, nothing re-sent except the new snapshot
    capsys.readouterr()
    assert batch.run(prof, interval=1) == 0
    log = capsys.readouterr().out
    assert "4 up to date" in log or "up to date" in log


def test_profile_errors(tmp_path, capsys):
    p = tmp_path / "bad.toml"
    p.write_text('dest = "/tmp/x"\n[[source]]\npath = "/does/not/exist"\n')
    assert batch.run(p) == 2
    p.write_text('dest = "/tmp/x"\n[options]\nbogus = 1\n')
    assert batch.run(p) == 2
    p.write_text('[[source]]\npath="/"\n')
    assert batch.run(p) == 2


@pytest.mark.parametrize("kind", ["fake38", "fake27"])
def test_follow_symlinks_remote_agent(tmp_path, kind):
    from test_engine import REMOTES, FakeRemote
    if not REMOTES[kind]:
        pytest.skip(f"{kind} interpreter not available")
    real = tmp_path / "real"
    _w(real / "a.fit", b"A" * 100)
    src = tmp_path / "src"
    src.mkdir()
    (src / "linked").symlink_to(real)
    (src / "loop").symlink_to(src)
    rem = FakeRemote(REMOTES[kind])
    dst = tmp_path / "dst"
    j = engine.Job(rem, [engine.Source(str(src), to="out", follow_symlinks=True,
                                       filter=Filter.build(exclude=["*.txt"]))],
                   LocalEndpoint(), str(dst), engine.Options(workers=2))
    j.start(); j.wait(30)
    assert j.snapshot().state == engine.J_DONE, j.errors
    assert (dst / "out/linked/a.fit").read_bytes() == b"A" * 100
    assert not (dst / "out/loop").exists()
    rem.close()
