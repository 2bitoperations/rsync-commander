"""The app icon, drawn in code so it is crisp at every size.

    uv run python -m rcommander.gui.icon     # regenerate assets/icon.png + icon.icns

Design: a night-sky squircle (it's for astrophotography) with two Norton
Commander-blue panels, an orange transfer arrow between them, and a row of
block-map cells (identical / sent / in flight / pending) along the bottom.
"""

import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QIcon, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap, QRadialGradient)

ASSETS = (Path(sys._MEIPASS) / "rcommander" / "assets" if getattr(sys, "frozen", False)
          else Path(__file__).resolve().parent.parent / "assets")


def render(size=1024):
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 1024, size / 1024)

    # macOS icon grid: ~824 px body centred on a 1024 canvas
    body = QRectF(100, 100, 824, 824)
    squircle = QPainterPath()
    squircle.addRoundedRect(body, 185, 185)

    # drop shadow
    p.save()
    p.translate(0, 12)
    p.fillPath(squircle, QColor(0, 0, 0, 70))
    p.restore()

    sky = QLinearGradient(0, 100, 0, 924)
    sky.setColorAt(0, QColor("#0b1026"))
    sky.setColorAt(0.6, QColor("#151d45"))
    sky.setColorAt(1, QColor("#2a1f4f"))
    p.fillPath(squircle, sky)
    p.setClipPath(squircle)

    # faint nebula glow + stars
    neb = QRadialGradient(QPointF(700, 260), 360)
    neb.setColorAt(0, QColor(170, 90, 200, 70))
    neb.setColorAt(1, QColor(170, 90, 200, 0))
    p.fillRect(body, neb)
    rnd = random.Random(598)  # NGC 598
    p.setPen(Qt.NoPen)
    for _ in range(90):
        x, y = rnd.uniform(110, 914), rnd.uniform(110, 914)
        r = rnd.choice([1.5, 2, 2.5, 3, 4]) if rnd.random() < 0.9 else 6
        p.setBrush(QColor(255, 255, 255, rnd.randint(90, 230)))
        p.drawEllipse(QPointF(x, y), r, r)

    # two panels
    def panel(x):
        r = QRectF(x, 250, 300, 420)
        p.setPen(QPen(QColor("#5ee7ff"), 10))
        g = QLinearGradient(0, 250, 0, 670)
        g.setColorAt(0, QColor("#1238c9"))
        g.setColorAt(1, QColor("#0a1f86"))
        p.setBrush(g)
        p.drawRoundedRect(r, 26, 26)
        # file rows
        p.setPen(Qt.NoPen)
        for i in range(6):
            y = 300 + i * 55
            w = [200, 150, 220, 120, 180, 160][i]
            p.setBrush(QColor(255, 255, 255, 200 if i != 2 else 255))
            if i == 2:  # selected row
                p.setBrush(QColor("#5ee7ff"))
                p.drawRoundedRect(QRectF(x + 18, y - 10, 264, 40), 8, 8)
                p.setBrush(QColor("#0a1f86"))
            p.drawRoundedRect(QRectF(x + 34, y, w, 20), 10, 10)

    panel(160)
    panel(564)

    # transfer arrow across the gap
    arrow = QPainterPath()
    arrow.moveTo(350, 438)
    arrow.lineTo(620, 438)
    arrow.lineTo(620, 392)
    arrow.lineTo(730, 460)
    arrow.lineTo(620, 528)
    arrow.lineTo(620, 482)
    arrow.lineTo(350, 482)
    arrow.closeSubpath()
    p.setPen(QPen(QColor("#1a1030"), 10, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    ag = QLinearGradient(330, 0, 735, 0)
    ag.setColorAt(0, QColor("#ffb020"))
    ag.setColorAt(1, QColor("#ff7a18"))
    p.setBrush(QBrush(ag))
    p.drawPath(arrow)

    # block map strip
    colors = (["#2f6f4f"] * 4 + ["#22c55e"] * 4 + ["#f59e0b"] * 2 + ["#6b7280"] * 3)
    cell, gap = 44, 10
    x0 = 512 - (len(colors) * (cell + gap) - gap) / 2
    p.setPen(Qt.NoPen)
    for i, c in enumerate(colors):
        p.setBrush(QColor(c))
        p.drawRoundedRect(QRectF(x0 + i * (cell + gap), 735, cell, cell), 9, 9)
    # the in-flight cell is half full
    p.setBrush(QColor("#6b7280"))
    p.drawRoundedRect(QRectF(x0 + 9 * (cell + gap), 735, cell, cell / 2), 9, 9)

    p.setClipping(False)
    p.setPen(QPen(QColor(255, 255, 255, 40), 4))
    p.setBrush(Qt.NoBrush)
    p.drawPath(squircle)
    p.end()
    return img


def app_icon():
    """QIcon for the window/dock: prefer the shipped PNG, else draw it."""
    png = ASSETS / "icon.png"
    if png.exists():
        return QIcon(str(png))
    icon = QIcon()
    for s in (16, 32, 64, 128, 256, 512):
        icon.addPixmap(QPixmap.fromImage(render(s)))
    return icon


def write_assets():
    ASSETS.mkdir(exist_ok=True)
    render(1024).save(str(ASSETS / "icon.png"))
    if sys.platform == "darwin" and shutil.which("iconutil"):
        with tempfile.TemporaryDirectory() as tmp:
            iset = Path(tmp) / "icon.iconset"
            iset.mkdir()
            for s in (16, 32, 128, 256, 512):
                render(s).save(str(iset / f"icon_{s}x{s}.png"))
                render(s * 2).save(str(iset / f"icon_{s}x{s}@2x.png"))
            subprocess.run(["iconutil", "-c", "icns", str(iset), "-o",
                            str(ASSETS / "icon.icns")], check=True)
    print(f"wrote {ASSETS}")


if __name__ == "__main__":
    from PySide6.QtGui import QGuiApplication
    _app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    write_assets()
