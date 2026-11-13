"""`rsync-commander install-app`: a launcher with a proper name and icon.

macOS: ~/Applications/rsync-commander.app, a small bundle whose executable is a
       shell script that runs this checkout through uv. It can go in Spotlight,
       the Dock and Launchpad.
Linux: ~/.local/share/applications/rsync-commander.desktop plus a hicolor icon.

The launcher runs `uv run --project <this checkout>`, so it always uses the
current code. Run install-app again if the checkout moves.
"""

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__

NAME = "rsync-commander"
BUNDLE_ID = "io.github.2bitoperations.rsync-commander"
ASSETS = Path(__file__).resolve().parent / "assets"
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
              "LaunchServices.framework/Support/lsregister")


def _project_root():
    root = Path(__file__).resolve().parents[2]
    if not (root / "pyproject.toml").exists():
        raise SystemExit("install-app must be run from a source checkout")
    return root


def _launch_cmd():
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv not found on PATH")
    return [uv, "run", "--project", str(_project_root()), NAME]


def install_macos(dest=None):
    app = Path(dest or Path.home() / "Applications") / f"{NAME}.app"
    if app.exists():
        shutil.rmtree(app)
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources").mkdir()
    shutil.copy(ASSETS / "icon.icns", app / "Contents" / "Resources" / "icon.icns")
    exe = app / "Contents" / "MacOS" / NAME
    # Finder-launched apps get a minimal PATH: give ssh/zstd the usual places.
    exe.write_text(
        "#!/bin/sh\n"
        'export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"\n'
        f"exec {shlex.join(_launch_cmd())} \"$@\"\n")
    exe.chmod(0o755)
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({
            "CFBundleName": NAME,
            "CFBundleDisplayName": NAME,
            "CFBundleIdentifier": BUNDLE_ID,
            "CFBundleExecutable": NAME,
            "CFBundleIconFile": "icon",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
        }, f)
    if os.path.exists(LSREGISTER):
        subprocess.run([LSREGISTER, "-f", str(app)], check=False)
    return app


def install_linux():
    from PySide6.QtGui import QGuiApplication
    from .gui.icon import render
    _app = QGuiApplication.instance() or QGuiApplication([NAME])  # noqa: F841
    share = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    for s in (32, 64, 128, 256, 512):
        d = share / "icons" / "hicolor" / f"{s}x{s}" / "apps"
        d.mkdir(parents=True, exist_ok=True)
        render(s).save(str(d / f"{NAME}.png"))
    desk = share / "applications" / f"{NAME}.desktop"
    desk.parent.mkdir(parents=True, exist_ok=True)
    desk.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={NAME}\n"
        "Comment=Dual-panel file commander with block-level ssh sync\n"
        f"Exec={shlex.join(_launch_cmd())}\n"
        f"Icon={NAME}\n"
        f"StartupWMClass={NAME}\n"
        "Categories=Utility;FileTransfer;Network;\n"
        "Terminal=false\n")
    for tool in (["update-desktop-database", str(desk.parent)],
                 ["gtk-update-icon-cache", "-q", str(share / "icons" / "hicolor")]):
        if shutil.which(tool[0]):
            subprocess.run(tool, check=False)
    return desk


def install_app():
    if sys.platform == "darwin":
        return install_macos()
    if sys.platform.startswith("linux"):
        return install_linux()
    raise SystemExit(f"unsupported platform {sys.platform}")
