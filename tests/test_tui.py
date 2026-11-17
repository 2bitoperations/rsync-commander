import asyncio
import os

from rcommander import engine
from rcommander.tui import Commander, TransfersScreen
from rcommander.tuistyle import graph, meter


def test_meter_and_graph():
    m = meter(0.5, 10)
    assert m.plain == "■" * 10
    assert len({str(s.style) for s in m.spans[:5]}) > 1        # gradient on filled part
    g = graph([0, 1, 2, 4, 8], width=5, height=2)
    lines = g.plain.split("\n")
    assert len(lines) == 2 and all(len(l) == 5 for l in lines)
    assert any(0x2800 < ord(c) <= 0x28FF for c in g.plain)


def test_tui_copy_flow(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for i in range(3):
        (src / f"f{i}.fits").write_bytes(os.urandom(200_000))
    dst = tmp_path / "dst"
    dst.mkdir()

    async def run():
        app = Commander(str(src), str(dst))
        async with app.run_test(size=(140, 40)) as pilot:
            for _ in range(20):
                await pilot.pause(0.1)
                if len(app.left.entries) == 4:
                    break
            await pilot.press("down", "space", "space", "space")
            assert len(app.left.marked) == 3
            await pilot.press("f5")
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.press("f9")
            await pilot.pause(0.3)
            assert isinstance(app.screen, TransfersScreen)
            for _ in range(100):
                await pilot.pause(0.1)
                if app.queue.jobs and app.queue.jobs[0].state == engine.J_DONE:
                    break
            assert app.queue.jobs[0].state == engine.J_DONE
            await pilot.press("escape")
            await pilot.press("tab")
            assert app.active is app.right
            await pilot.pause(0.5)
            names = sorted(e["name"] for e in app.right.entries if e["type"] == "f")
            assert names == ["f0.fits", "f1.fits", "f2.fits"]
    asyncio.run(run())
    for i in range(3):
        assert (dst / f"f{i}.fits").read_bytes() == (src / f"f{i}.fits").read_bytes()


def test_tui_files_window(monkeypatch):
    """Big jobs show a window of rows around the active file, and browsing
    past its edge moves the window without losing the selected file."""
    import time
    from textual.widgets import DataTable
    from rcommander.endpoint import LocalEndpoint
    monkeypatch.setattr(TransfersScreen, "FILES_WINDOW", 100)
    j = engine.Job(LocalEndpoint(), ["/tmp"], LocalEndpoint(), "/tmp/x")
    for i in range(5000):
        j.files.append(engine.FileTask(f"d/{i}", ("/a", f"d/{i}"), ("/b", f"d/{i}"),
                                       10, 0, 0o644, 4, idx=i))
    j.t_start, j.state, j.next_idx = time.time(), engine.J_RUNNING, 3000
    with j.lock:
        for f in j.files[:2990]:
            j._retire(f, engine.F_DONE)
        for f in j.files[2990:3000]:
            f.state = engine.F_WORKING
            j.active.append(f)

    class Q:
        jobs = [j]

        def cancel(self, job):
            pass

    async def run():
        app = Commander("/tmp", "/tmp")
        async with app.run_test(size=(140, 45)) as pilot:
            scr = TransfersScreen(Q())
            app.push_screen(scr)
            await pilot.pause(0.3)
            scr.tick()
            ft = scr.query_one("#files", DataTable)
            assert ft.row_count == 100 and scr._lo <= 2990 < scr._hi
            ft.focus()
            await pilot.press("down")
            await pilot.pause()
            assert not scr.follow
            for _ in range(120):
                await pilot.press("down")
            await pilot.pause()
            scr.tick()
            await pilot.pause()
            assert scr._hi > 2990 + 100                     # window moved on
            assert scr._lo + ft.cursor_row == scr.file.idx  # cursor still on the file
            await pilot.press("f")
            scr.tick()
            assert scr.follow and scr._lo <= 2990 < scr._hi
    asyncio.run(run())
