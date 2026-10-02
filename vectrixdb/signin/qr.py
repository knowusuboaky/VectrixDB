"""The QR code an authenticator app scans at enrolment: drawn to be read first, and to look like the dashboard.

A code a phone cannot read stops somebody at the door, so every choice here was
measured before it was made. Each candidate was drawn by a browser at 1x and
2x, on a white card and on a dark one, and read back by three readers: OpenCV's
QR detector, its ArUco detector, and WeChat's, the most forgiving of the three.
What was kept is what all three read every time:

* Round dots, 0.92 of a module across.
* Square corner eyes, their corners rounded by half a module. Fully round eyes
  were read by WeChat's reader alone: the other two find a code by the corners
  of its eyes.
* Error correction at level Q, which repairs a quarter of the code, and an
  address that leaves out what every app assumes (SHA1, six digits, thirty
  seconds). At level H the usual address was 57 modules a side and its dots
  too small to read at 200 pixels; at Q it is 49.
* The dashboard's own mark in a clear square in the middle, over at most 7.5
  per cent of the code, so the repair keeps room for glare and blur.
* Dark on light on either theme, on a tile of its own with a margin of three
  modules. Light dots on a dark card were missed by two of the three, and not
  every scanner looks for an inverted code.
* Never less dense than what was read, 200 pixels for 55 modules: the usual
  address is drawn at 200 pixels, a long one larger.

Only the dots, the eyes' corners and the middle differ from a plain code: every
module a reader samples is where the standard puts it. The code is drawn on
the server, as SVG, with no image library and no script on the page. Without
the ``qrcode`` package there is no picture, and the enrolment page shows the
secret to type in by hand, which every authenticator app takes.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import base64
import html
import math
import re
from typing import Optional

__all__ = [
    "DOT",
    "INK",
    "LEVEL",
    "MARK_SHARE",
    "QUIET",
    "TILE",
    "available",
    "mark_side",
    "matrix",
    "size",
    "svg",
]


# ============================================================================
# SETTINGS: the level, the dots, the eyes, the mark's share, the margin, the
#           density and the colours
# ============================================================================
#
# What was read back, as numbers. The colours are VectrixDB's light theme and
# its mark; a dashboard with a brand passes its own (Brand.code_look).

#: The error correction level. Q repairs a quarter of the code.
LEVEL = "Q"
#: A dot's width, in modules.
DOT = 0.92
#: How far the corner eyes are rounded, in modules: the ring, the hollow in it, the pupil.
EYE_CORNERS = (0.5, 0.34, 0.23)
#: The most of the code the mark in the middle may cover.
MARK_SHARE = 0.075
#: The margin inside the tile, in modules, and how far the tile's corners are rounded.
QUIET = 3
TILE_CORNER = 3
#: The density that was read back: 200 CSS pixels for 55 modules, the margin included.
READ_PIXELS, READ_MODULES = 200, 55
#: The smallest the code is drawn, margin included, in CSS pixels.
MIN_SIZE = 200
#: Dark on light: VectrixDB's ink and paper.
INK, TILE = "#1a170f", "#f7f5f1"
#: The layers mark the sidebar shows when there is no logo: VectrixDB's gold, and the ink on it.
MARK_FILL, MARK_INK = "#e8a81a", "#1a170f"
#: What a logo may be, as the brand reads it from its first bytes.
IMAGES = ("image/svg+xml", "image/png", "image/jpeg")

_HEX = re.compile(r"#[0-9a-fA-F]{6}")
# The layers mark as favicon.svg draws it: lucide's layers, in a tile of 32 units.
_LAYERS = (
    "M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83z",
    "M2 12a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 12",
    "M2 17a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 17",
)


# ============================================================================
# THE CODE
# ============================================================================
#
# INPUT   the text; the ink, the tile, and the logo or the mark's colours
# OUTPUT  whether a code can be drawn here; its modules; the side of the clear
#         square in the middle; the pixels to draw it at; the SVG
#
# The modules are the qrcode package's, at level Q; only how each is drawn is
# decided here.


def available() -> bool:
    """Whether a code can be drawn here: the ``qrcode`` package is installed."""
    try:
        import qrcode  # noqa: F401
    except ImportError:
        return False
    return True


def matrix(text: str) -> Optional[list[list[bool]]]:
    """The code's modules, dark as True, at level Q in the smallest version that holds the text.

    None without the ``qrcode`` package.
    """
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_Q
    except ImportError:
        return None
    code = qrcode.QRCode(error_correction=ERROR_CORRECT_Q, border=0)
    code.add_data(text)
    code.make(fit=True)
    return [[bool(module) for module in row] for row in code.get_matrix()]


def mark_side(n: int) -> int:
    """The side of the clear square in the middle of a code ``n`` modules a side, or 0 for none.

    Odd, so it sits on the centre module, no more than ``MARK_SHARE`` of the
    code, and clear of the eyes, the timing lines and the format and version
    information, which all lie within nine modules of an edge. A code too
    small to keep a mark five modules across clear of them gets none.
    """
    side = min(int(math.sqrt(MARK_SHARE) * n), n - 18)
    side = side if side % 2 else side - 1
    return side if side >= 5 else 0


def size(n: int) -> int:
    """The CSS pixels to draw a code ``n`` modules a side at, its margin included.

    Never less dense than what was read back and never under 200: the usual
    address is drawn at 200, a long one larger.
    """
    units = n + 2 * QUIET
    return max(MIN_SIZE, -(-units * READ_PIXELS // READ_MODULES))


def _eye(r: int, c: int, n: int) -> bool:
    return (r < 7 and c < 7) or (r < 7 and c >= n - 7) or (r >= n - 7 and c < 7)


def _num(value: float) -> str:
    """A number as short as SVG takes it: 0.46 as .46, and no trailing zeros."""
    text = f"{value:g}"
    return text.replace("0.", ".", 1) if text.startswith(("0.", "-0.")) else text


def _colour(value: str, name: str) -> str:
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ValueError(f"The QR code's {name} is {value!r}. Write it as #rrggbb")
    return value.lower()


def svg(
    text: str,
    *,
    ink: str = INK,
    tile: str = TILE,
    logo: Optional[tuple[bytes, str]] = None,
    mark_fill: str = MARK_FILL,
    mark_ink: str = MARK_INK,
    label: str = "QR code for your authenticator app",
) -> Optional[str]:
    """``text`` as a QR code in SVG, or None when the ``qrcode`` package is not installed.

    ``ink`` and ``tile`` are the dots and the tile they sit on, dark on light.
    ``logo`` is the dashboard's logo as ``(bytes, type)``, SVG, PNG or JPEG. It
    goes inside as an image, so it runs nothing and fetches nothing. Without
    one, the middle is the layers mark in ``mark_fill`` and ``mark_ink``, as
    the sidebar draws it.
    """
    ink, tile = _colour(ink, "ink"), _colour(tile, "tile")
    mark_fill, mark_ink = _colour(mark_fill, "mark fill"), _colour(mark_ink, "mark ink")
    if logo is not None and logo[1] not in IMAGES:
        raise ValueError(f"The QR code's logo is {logo[1]!r}. It is one of {', '.join(IMAGES)}")
    modules = matrix(text)
    if modules is None:
        return None
    n = len(modules)
    side = mark_side(n)
    # An empty range when there is no mark: every module is drawn.
    low, high = ((n - side) // 2, (n - side) // 2 + side - 1) if side else (n, -1)

    # Every dark module outside the eyes and the middle is a dot on its centre:
    # two half circles from its left edge and back, so the next dot along the
    # row is a whole number of modules away.
    radius = DOT / 2
    arc = f"a{_num(radius)} {_num(radius)} 0 1 0 {_num(DOT)} 0a{_num(radius)} {_num(radius)} 0 1 0-{_num(DOT)} 0"
    dots = []
    for r, row in enumerate(modules):
        last = None
        for c, dark in enumerate(row):
            if not dark or _eye(r, c, n) or (low <= r <= high and low <= c <= high):
                continue
            dots.append(
                f"M{_num(c + QUIET + 0.5 - radius)} {_num(r + QUIET + 0.5)}{arc}"
                if last is None
                else f"m{c - last} 0{arc}"
            )
            last = c

    ring, hollow, pupil = EYE_CORNERS
    eyes = "".join(
        f'<rect x="{c + QUIET}" y="{r + QUIET}" width="7" height="7" rx="{ring:g}" fill="{ink}"/>'
        f'<rect x="{c + QUIET + 1}" y="{r + QUIET + 1}" width="5" height="5" rx="{hollow:g}" fill="{tile}"/>'
        f'<rect x="{c + QUIET + 2}" y="{r + QUIET + 2}" width="3" height="3" rx="{pupil:g}" fill="{ink}"/>'
        for r, c in ((0, 0), (0, n - 7), (n - 7, 0))
    )

    # The mark fills the clear square but its outermost ring of modules, which stays tile.
    at, across = low + 1 + QUIET, side - 2
    if not side:
        middle = ""
    elif logo is not None:
        data, mime = logo
        middle = (
            f'<image href="data:{mime};base64,{base64.b64encode(data).decode("ascii")}" x="{at}" y="{at}" '
            f'width="{across}" height="{across}" preserveAspectRatio="xMidYMid meet"/>'
        )
    else:
        paths = "".join(f'<path d="{d}"/>' for d in _LAYERS)
        middle = (
            f'<g transform="translate({at} {at}) scale({across / 32:g})">'
            f'<rect width="32" height="32" rx="8" fill="{mark_fill}"/>'
            f'<g transform="translate(4 4)" fill="none" stroke="{mark_ink}" stroke-width="1.9" '
            f'stroke-linecap="round" stroke-linejoin="round">{paths}</g></g>'
        )

    view, pixels = n + 2 * QUIET, size(n)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {view} {view}" width="{pixels}" height="{pixels}" '
        f'role="img" aria-label="{html.escape(label, quote=True)}">'
        f'<rect width="{view}" height="{view}" rx="{TILE_CORNER}" fill="{tile}"/>'
        f'<path fill="{ink}" d="{"".join(dots)}"/>{eyes}{middle}</svg>'
    )
