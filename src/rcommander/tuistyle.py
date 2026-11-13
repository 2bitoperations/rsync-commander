"""btop-style meters and braille graphs for the terminal UI.

meter():  a row of ■ cells. Filled cells take their colour from a gradient laid
          across the full width (a 30% bar only shows the start colours);
          empty cells are dim. This is how btop draws its meters.
graph():  a braille history graph (2 samples per character, 4 dot rows per
          line), coloured bottom-to-top by a gradient like btop's net graph.
"""

from rich.color import Color
from rich.style import Style
from rich.text import Text

EMPTY = "#3a3a3a"

GRADIENTS = {
    # name: (start, mid, end)
    "progress": ("#2f6fae", "#3fb8c9", "#7ee08a"),   # blue -> teal -> green
    "block": ("#b36b00", "#f0a020", "#ffd76a"),      # orange, matches "transferring"
    "speed": ("#3b3f99", "#7d6fe6", "#d0a8ff"),      # btop-ish download purple
    "hash": ("#5b3f99", "#9d7fe6", "#d6c2ff"),
}


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


_cache = {}


def gradient(name, steps):
    key = (name, steps)
    if key not in _cache:
        s, m, e = (_hex(c) for c in GRADIENTS[name])
        out = []
        for i in range(steps):
            t = i / max(1, steps - 1)
            rgb = _lerp(s, m, t * 2) if t < 0.5 else _lerp(m, e, (t - 0.5) * 2)
            out.append(Style(color=Color.from_rgb(*rgb)))
        _cache[key] = out
    return _cache[key]


def meter(frac, width=30, kind="progress"):
    frac = max(0.0, min(1.0, frac or 0.0))
    filled = int(round(frac * width))
    if 0 < frac and filled == 0:
        filled = 1
    grad = gradient(kind, width)
    t = Text()
    for i in range(width):
        t.append("■", style=grad[i] if i < filled else EMPTY)
    return t


# braille: dots per column, bottom-up
_LEFT = (0x40, 0x04, 0x02, 0x01)
_RIGHT = (0x80, 0x20, 0x10, 0x08)


def graph(values, width=40, height=3, kind="speed", vmax=None):
    """values: oldest..newest; the newest sample sits at the right edge."""
    n = width * 2
    vals = list(values)[-n:]
    vals = [0.0] * (n - len(vals)) + vals
    top = vmax or max(vals) or 1.0
    dots = height * 4
    levels = [min(dots, int(round(v / top * dots))) if v > 0 else 0 for v in vals]
    # a non-zero sample always shows at least one dot
    levels = [max(1, l) if v > 0 else 0 for l, v in zip(levels, vals)]
    grad = gradient(kind, height)
    t = Text()
    for r in range(height):
        rb = height - 1 - r                    # row index from the bottom
        style = grad[rb]
        for c in range(width):
            bits = 0
            for lvl, col in ((levels[2 * c], _LEFT), (levels[2 * c + 1], _RIGHT)):
                k = max(0, min(4, lvl - rb * 4))
                for j in range(k):
                    bits |= col[j]
            t.append(chr(0x2800 + bits) if bits else " ", style=style)
        if r < height - 1:
            t.append("\n")
    return t


def stacked(segs, width=12):
    """btop-style cells split into coloured segments, e.g. a file's identical /
    sent / in-flight shares in the block-map colours. segs: [(fraction, colour)]."""
    t = Text()
    used = 0
    for frac, color in segs:
        n = int(round(max(0.0, frac) * width))
        if frac > 0 and n == 0:
            n = 1
        n = min(n, width - used)
        t.append("■" * n, style=color)
        used += n
    t.append("■" * (width - used), style=EMPTY)
    return t
