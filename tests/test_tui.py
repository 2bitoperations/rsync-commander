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
