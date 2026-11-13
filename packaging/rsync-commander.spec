# -*- mode: python -*-
# PyInstaller spec: one binary that is the GUI by default and the full CLI with
# arguments (tui, batch, copy, ...).  Built by packaging/build_{macos,linux}.sh.
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(ROOT, "src")
PKG = os.path.join(SRC, "rcommander")
sys.path.insert(0, SRC)
from rcommander import __version__  # noqa: E402

datas = [
    # streamed to remote pythons as *source*, so it must ship as a file
    (os.path.join(PKG, "agent.py"), "rcommander"),
    (os.path.join(PKG, "assets"), "rcommander/assets"),
]
datas += collect_data_files("textual")          # .tcss and friends
hidden = (collect_submodules("rcommander") + collect_submodules("textual")
          + collect_submodules("rich"))          # both import lazily

# Qt modules the app never touches (keeps the bundle a lot smaller)
excludes = [f"PySide6.{m}" for m in (
    "QtWebEngineCore", "QtWebEngineWidgets", "QtWebEngineQuick", "QtWebChannel",
    "QtWebSockets", "QtQuick", "QtQuick3D", "QtQuickWidgets", "QtQml", "Qt3DCore",
    "Qt3DRender", "Qt3DExtras", "Qt3DInput", "Qt3DLogic", "Qt3DAnimation", "QtCharts",
    "QtDataVisualization", "QtGraphs", "QtMultimedia", "QtMultimediaWidgets", "QtPdf",
    "QtPdfWidgets", "QtPositioning", "QtLocation", "QtSensors", "QtSerialPort",
    "QtBluetooth", "QtNfc", "QtRemoteObjects", "QtScxml", "QtSql", "QtTest",
    "QtTextToSpeech", "QtDesigner", "QtHelp", "QtOpenGL", "QtOpenGLWidgets",
    "QtSpatialAudio", "QtHttpServer", "QtCanvasPainter")] + ["tkinter", "pytest"]

a = Analysis([os.path.join(SPECPATH, "entry.py")], pathex=[SRC], datas=datas,
             hiddenimports=hidden, excludes=excludes, noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="rsync-commander",
          console=sys.platform != "darwin",   # macOS .app; still prints when run from a shell
          debug=False, strip=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="rsync-commander")

if sys.platform == "darwin":
    app = BUNDLE(
        coll, name="rsync-commander.app",
        icon=os.path.join(PKG, "assets", "icon.icns"),
        bundle_identifier="io.github.2bitoperations.rsync-commander",
        version=__version__,
        info_plist={
            "CFBundleName": "rsync-commander",
            "CFBundleDisplayName": "rsync-commander",
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "NSHumanReadableCopyright": "rsync-commander by 2bitoperations",
        },
    )
