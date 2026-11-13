"""Quitting the GUI must exit cleanly (it used to SIGSEGV in PySide's exit-time
widget sweep: double delete of a QComboBox)."""
import os
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("PySide6")

SCRIPT = textwrap.dedent("""
    import sys
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialog
    import rcommander.gui.app as A
    A.CopyDialog.exec = lambda self: QDialog.Rejected
    orig = A.MainWindow.__init__
    def init(self, *a, **k):
        orig(self, *a, **k)
        QTimer.singleShot(200, lambda: self.copy("Local", ["/tmp"], self.right, "/tmp"))
        QTimer.singleShot(200, self.right.combo.showPopup)
        QTimer.singleShot(400, self.right.combo.hidePopup)
        QTimer.singleShot(800, self.close)
    A.MainWindow.__init__ = init
    sys.exit(A.run())
""")


def test_gui_quits_cleanly(tmp_path):
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "RC_NO_CONFIRM": "1",
           "HOME": str(tmp_path)}          # fresh QSettings
    r = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, f"exit {r.returncode}\n{r.stderr[-2000:]}"
