"""GUI: zstd requested but the remote lacks it -> Install / Fall back / Cancel."""
import os
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("PySide6")
from test_engine import PY38  # noqa: E402

SCRIPT = textwrap.dedent("""
    import sys
    sys.path.insert(0, {tests!r})
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QMessageBox
    import rcommander.gui.app as A
    from rcommander import engine, helpers
    from rcommander.endpoint import LocalEndpoint
    import test_zstd_prompt
    from test_zstd_prompt import NoZstd
    test_zstd_prompt.PY38 = {py38!r}   # HOME is a temp dir here: uv can't find it

    CHOICE = {choice!r}
    prompts, installed, started = [], [], []
    def fake_exec(box):
        prompts.append(box.text())
        for b in box.buttons():
            if b.text() == CHOICE or (CHOICE == "Cancel" and
                                       box.buttonRole(b) == QMessageBox.RejectRole):
                b.click()
                return 0
        raise SystemExit("no button " + CHOICE)
    A.QMessageBox.exec = fake_exec
    helpers.install_zstd = lambda ep, log=None: installed.append(ep.host) or True
    rem = NoZstd()
    z = engine.Options(compression="zstd")

    orig = A.MainWindow.__init__
    def init(self, *a, **k):
        orig(self, *a, **k)
        def go():
            self._ensure_zstd(LocalEndpoint(), rem, z, started.append)
        def again():   # after a fallback the host isn't asked about again
            self._ensure_zstd(LocalEndpoint(), rem, z, started.append)
        def report():
            print("RESULT", len(prompts), installed, [o.compression for o in started])
            self.close()
        QTimer.singleShot(100, go)
        QTimer.singleShot(2500, again)
        QTimer.singleShot(5000, report)
    A.MainWindow.__init__ = init
    A.run()
    rem.close()
""")


@pytest.mark.skipif(not PY38, reason="needs python3.8")
@pytest.mark.parametrize("choice,expect", [
    ("Fall back to gzip (zlib)", "RESULT 1 [] ['gzip', 'gzip']"),
    ("Install zstd", "RESULT 2 ['oldbox', 'oldbox'] ['zstd', 'zstd']"),
    ("Cancel", "RESULT 2 [] []"),
])
def test_gui_zstd_prompt(tmp_path, choice, expect):
    tests = os.path.dirname(os.path.abspath(__file__))
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "RC_NO_CONFIRM": "1",
           "HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, "-c", SCRIPT.format(tests=tests, choice=choice, py38=PY38)],
                       env=env, capture_output=True, text=True, timeout=60)
    assert expect in r.stdout, r.stdout + r.stderr[-2000:]
