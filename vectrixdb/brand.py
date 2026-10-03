"""A company's own name, logo and colours on the dashboard, set before it is deployed.

===============================  ==============================================
``VECTRIXDB_BRAND_NAME``         The name beside the logo, in the tab and in
                                 sign-in emails. ``VectrixDB`` unless set.
``VECTRIXDB_BRAND_LOGO``         A square logo: SVG, PNG or JPEG, as a file or
                                 as a ``data:`` address holding the image.
``VECTRIXDB_BRAND_LOGO_DARK``    The same logo for the dark theme. Optional.
``VECTRIXDB_BRAND_ACCENT``       One colour, ``#rrggbb``. VectrixDB works out the rest.
``VECTRIXDB_BRAND_WORDMARK``     ``on``: the name is set as a wordmark, larger and
                                 in the accent, the way a company writes its own.
``VECTRIXDB_BRAND_COPYRIGHT``    Whose the line under the sidebar names:
                                 ``Northwind`` reads ``© 2026 Northwind``.
                                 ``VectrixDB`` unless set.
``VECTRIXDB_BRAND_PALETTE``      The grounds, inks and lines for each theme, as
                                 JSON or a JSON file. See ``PALETTE`` below.
===============================  ==============================================

The brand changes what people see in the dashboard. The API, the command line,
the logs and the documentation still say VectrixDB. Nobody who has not signed
in sees the name VectrixDB on a branded dashboard; an admin finds it, with its
version, the Apache licence and the NOTICE, under About in the account menu.
Apache 2.0 asks that the licence and the notice travel with the work, and
they do: in the package, and on that page.

A logo is told apart by its first bytes, not its name, and is 512 KB at most.
A ``data:`` address is for a host that keeps settings and no files, such as a
function app: the image travels inside the setting and is checked the same way.
A PNG or JPEG is at least 64 pixels on its shorter side, since it is shown at
28 and a screen may double that. JPEG cannot be transparent, so it shows as a
tile; SVG or PNG looks cleaner. An SVG that could run anything (a script, an
event handler, a foreignObject, a ``javascript:`` address, or anything fetched
from elsewhere) is refused at start-up, and every logo is served as an image
under a policy that lets it run nothing. A mistake in any of this stops the
server from starting: a bank's deployment that quietly came up as somebody
else's brand would be worse than one that did not come up.

The colour is one accent. From it: the fill for buttons, the ink that sits on
the fill (black or white, whichever reads better), a shade dark enough to read
as text on paper and one light enough on the dark theme, and a tint behind
what is selected. An accent close to the red that means something is wrong is
allowed, with a warning, since plenty of companies are red.

The palette is for a company whose own apps have a look of their own. It names
what it paints, not how the stylesheet spells it, and any of it may be left
out, the rest staying VectrixDB's::

    {"light": {"page": "#f4f6f4", "sidebar": "#ffffff", "card": "#ffffff",
               "field": "#eef2ef", "subtle": "#e3ebe5", "text": "#15241c",
               "text-2": "#33483c", "muted": "#4b6155", "line": "#dde6df",
               "border": "#cfdad2"},
     "dark":  {...}}

``success``, ``warning``, ``danger`` and ``info`` may be given too; they mean
state, so each must still read as text on a card. Every ink is measured on
the grounds it sits on, and a palette where one does not reach 4.5 to 1 stops
the server, with the pair named: a colour people cannot read is not a brand.

The QR code an authenticator app scans at enrolment carries the logo in its
middle, or the layers mark in the accent when there is no logo, and is drawn
in the light theme's text on its page, on either theme. A palette whose two
are less than 7 to 1 apart gets VectrixDB's own ink and paper for the code
instead: a camera sees less than an eye.
"""

from __future__ import annotations

import base64
import binascii
import html
import json
import logging
import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional
from urllib.parse import unquote_to_bytes

from .exceptions import ConfigurationError

__all__ = [
    "COPYRIGHT",
    "PALETTE",
    "Brand",
    "Logo",
    "contrast",
]


# ============================================================================
# SETTINGS: the copyright line, the logo's limits, what an SVG may not hold,
#           and the palette's names
# ============================================================================
#
# The line under the sidebar and whose it is unless a setting says, how big
# and how small a logo may be, the SVG elements that would run code, and what
# a palette may paint, with VectrixDB's own colours for what it leaves out.

logger = logging.getLogger("vectrixdb.brand")

#: The year on the line under the sidebar, and whose it is unless set.
YEAR = 2026
HOLDER = "VectrixDB"
#: The line a dashboard with no brand shows.
COPYRIGHT = f"© {YEAR} {HOLDER}"
MAX_LOGO_BYTES = 512 * 1024
MIN_LOGO_PX = 64
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
#: What an SVG may not contain, because each one can run or fetch something.
_SVG_FORBIDDEN = (
    re.compile(r"<\s*script", re.I),
    re.compile(r"<\s*foreignobject", re.I),
    re.compile(r"<\s*(?:iframe|embed|object|image|use|animate|set|handler|listener)\b", re.I),
    re.compile(r"\son[a-z]+\s*=", re.I),
    re.compile(r"javascript\s*:", re.I),
    re.compile(r"(?:href|src)\s*=\s*[\"']?\s*(?:https?:|//|data:)", re.I),
    re.compile(r"url\s*\(\s*[\"']?\s*(?:https?:|//|data:)", re.I),
    re.compile(r"@import", re.I),
    re.compile(r"<!\s*entity", re.I),
)
# The page's own grounds, which the text shades are measured against: paper
# in the light theme, the dark card in the other, whose colour is also the ink.
_PAPER, _NIGHT = (247, 245, 241), (26, 23, 15)
_INK_DARK, _WHITE = _NIGHT, (255, 255, 255)

#: What a palette may paint, and the stylesheet's name for each.
PALETTE = {
    "page": "--g0",
    "sidebar": "--g1",
    "card": "--g2",
    "field": "--field",
    "subtle": "--g3",
    "text": "--t1",
    "text-2": "--t2",
    "muted": "--t3",
    "line": "--line",
    "border": "--line2",
    "success": "--ok",
    "warning": "--warn",
    "danger": "--bad",
    "info": "--info",
}
#: VectrixDB's own colours, as app.css has them: what a palette leaves out is measured as these.
_THEMES = {
    "light": {
        "page": "#f7f5f1",
        "sidebar": "#fdfcf9",
        "card": "#ffffff",
        "field": "#fdfcf9",
        "subtle": "#f5f1e8",
        "text": "#1a170f",
        "text-2": "#5f5b50",
        "muted": "#6f6b61",
        "line": "#ece9e2",
        "border": "#d6d1c6",
        "success": "#0f6b50",
        "warning": "#a3541a",
        "danger": "#c0362c",
        "info": "#2f5f8a",
    },
    "dark": {
        "page": "#120f09",
        "sidebar": "#14110a",
        "card": "#1a170f",
        "field": "#14110a",
        "subtle": "#242015",
        "text": "#f7f5f1",
        "text-2": "#b3a99b",
        "muted": "#9a958a",
        "line": "#2a2619",
        "border": "#3a3527",
        "success": "#7ec97e",
        "warning": "#e59a5c",
        "danger": "#d3736a",
        "info": "#8fb4d9",
    },
}
#: Each ink, and the grounds it is written on.
_READS_ON = {
    "text": ("page", "sidebar", "card", "field", "subtle"),
    "text-2": ("page", "sidebar", "card", "subtle"),
    "muted": ("page", "sidebar", "card"),
    "success": ("card",),
    "warning": ("card",),
    "danger": ("card",),
    "info": ("card",),
}
#: How strong the tint behind a state colour is, per theme, as app.css has it.
_TINT = {"light": 0.10, "dark": 0.14}
#: VectrixDB's gold: the buttons and the mark wherever no accent is set.
_GOLD = "#e8a81a"
#: How far apart a QR code's dots and tile must be: more than reading needs, since a camera sees less than an eye.
CODE_CONTRAST = 7.0


# ============================================================================
# THE LOGO, AND THE COLOURS
# ============================================================================
#
# INPUT   a logo file; two colours
# OUTPUT  the logo read, and refused when it holds a script; the WCAG contrast
#         of two colours, 1 to 21; the accent moved toward a target only as
#         far as it takes to read on its ground
#
# A logo that tries to run code never loads; an accent that cannot be read is
# nudged, not replaced.


@dataclass
class Logo:
    data: bytes
    mime: str
    path: str

    @property
    def file(self) -> str:
        return self.path if self.path == DATA_URL else Path(self.path).name


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    value = hex_colour.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def _luminance(rgb: tuple[float, float, float]) -> float:
    def channel(c: float) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    """The WCAG contrast ratio of two colours, 1 to 21."""
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _ink_on(rgb: tuple[int, int, int]) -> str:
    """The ink that sits on a fill of this colour: white or near black, whichever reads better."""
    return "#ffffff" if contrast(rgb, _WHITE) >= contrast(rgb, _INK_DARK) else "#1a170f"


def _toward(
    rgb: tuple[int, int, int],
    target: tuple[int, int, int],
    *grounds: tuple[int, int, int],
    wanted: float = 4.5,
) -> tuple[float, float, float]:
    """The accent, moved toward ``target`` only as far as it takes to read on every one of ``grounds``."""
    for step in range(0, 101):
        t = step / 100
        r, g, b = (c + (d - c) * t for c, d in zip(rgb, target))
        if all(contrast((r, g, b), ground) >= wanted for ground in grounds):
            return (r, g, b)
    return target


def _is_red(rgb: tuple[int, int, int]) -> bool:
    r, g, b = (c / 255 for c in rgb)
    high, low = max(r, g, b), min(r, g, b)
    if high - low < 0.25 or high != r:
        return False
    hue = (60 * ((g - b) / (high - low))) % 360
    return hue < 20 or hue > 340


def _png_size(data: bytes) -> Optional[tuple[int, int]]:
    if len(data) >= 24 and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    return None


def _jpeg_size(data: bytes) -> Optional[tuple[int, int]]:
    at = 2
    while at + 9 < len(data):
        if data[at] != 0xFF:
            return None
        marker = data[at + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            at += 2
            continue
        length = struct.unpack(">H", data[at + 2 : at + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[at + 5 : at + 9])
            return width, height
        at += 2 + length
    return None


# ============================================================================
# THE BRAND FROM THE ENVIRONMENT
# ============================================================================
#
# INPUT   VECTRIXDB_BRAND_NAME, LOGO, LOGO_DARK and ACCENT
# OUTPUT  the logo, checked; the brand as the dashboard shows it, its name, a
#         logo for each theme, and the accent with its tint, its text shade
#         and the ink on it
#
# The copyright line is not a setting: the software is VectrixDB's, always.


#: How a logo that came inside its setting is named where a file's name would be.
DATA_URL = "a data: address"


def _data_url(name: str, value: str) -> bytes:
    """The image inside ``data:[type][;base64],...``. The type it claims is not trusted: the bytes are sniffed like a file's."""
    head, comma, body = value.partition(",")
    if not comma:
        raise ConfigurationError(
            f"{name} starts like a data: address and has no comma before the image. Write it as data:image/svg+xml;base64,..."
        )
    if len(body) > MAX_LOGO_BYTES * 2:
        raise ConfigurationError(
            f"{name}: the logo is over {MAX_LOGO_BYTES // 1024} KB. Keep it under {MAX_LOGO_BYTES // 1024} KB"
        )
    if head.lower().endswith(";base64"):
        try:
            return base64.b64decode("".join(body.split()), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ConfigurationError(
                f"{name} says base64 and what follows is not base64: {exc}"
            ) from exc
    return unquote_to_bytes(body)


def _logo(env: Mapping[str, str], name: str) -> Optional[Logo]:
    path = str(env.get(name, "") or "").strip()
    if not path:
        return None
    if path[:5].lower() == "data:":
        data, path = _data_url(name, path), DATA_URL
    else:
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            raise ConfigurationError(f"{name} names {path}, which cannot be read: {exc}") from exc
    if len(data) > MAX_LOGO_BYTES:
        raise ConfigurationError(
            f"{name}: the logo is {len(data) // 1024} KB. Keep it under {MAX_LOGO_BYTES // 1024} KB"
        )
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime, size = "image/png", _png_size(data)
    elif data.startswith(b"\xff\xd8\xff"):
        mime, size = "image/jpeg", _jpeg_size(data)
    else:
        text = data.decode("utf-8", errors="replace")
        head = text.lstrip("﻿ \t\r\n")[:512].lower()
        if "<svg" not in head and not (head.startswith("<?xml") and "<svg" in text.lower()):
            raise ConfigurationError(f"{name}: {path} is not an SVG, PNG or JPEG image")
        for pattern in _SVG_FORBIDDEN:
            if pattern.search(text):
                raise ConfigurationError(
                    f"Brand logo refused: {Logo(data, '', path).file} has a script, an event handler or a link to somewhere else in it. "
                    "Export it again as a plain image."
                )
        return Logo(data=data, mime="image/svg+xml", path=path)
    if size is None:
        raise ConfigurationError(f"{name}: {path} could not be read as an image")
    width, height = size
    if min(width, height) < MIN_LOGO_PX:
        raise ConfigurationError(
            f"{name}: the logo is {width}x{height}. Use one at least {MIN_LOGO_PX} pixels on each side"
        )
    if max(width, height) > 2 * min(width, height):
        logger.warning(
            "%s is %sx%s. A square logo fits the folded sidebar and the browser tab best",
            name,
            width,
            height,
        )
    return Logo(data=data, mime=mime, path=path)


def _palette(env: Mapping[str, str]) -> dict:
    """The palette, checked: known names, ``#rrggbb`` colours, and every ink readable on its grounds."""
    given = str(env.get("VECTRIXDB_BRAND_PALETTE", "") or "").strip()
    if not given:
        return {}
    text = given
    if given[:5].lower() == "data:":
        # For a host that keeps settings and no files, as a logo can be.
        text = _data_url("VECTRIXDB_BRAND_PALETTE", given).decode("utf-8", errors="replace")
    elif not given.startswith("{"):
        try:
            text = Path(given).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigurationError(
                f"VECTRIXDB_BRAND_PALETTE names {given}, which cannot be read: {exc}"
            ) from exc
    try:
        palette = json.loads(text)
    except ValueError as exc:
        raise ConfigurationError(
            f'VECTRIXDB_BRAND_PALETTE must be JSON, for example {{"light": {{"page": "#f4f6f4"}}}}: {exc}'
        ) from exc
    if not isinstance(palette, dict) or not palette:
        raise ConfigurationError(
            'VECTRIXDB_BRAND_PALETTE is an object with "light", "dark" or both'
        )
    out: dict = {}
    for theme, colours in palette.items():
        if theme not in _THEMES:
            raise ConfigurationError(
                f"VECTRIXDB_BRAND_PALETTE names the theme {theme!r}. The themes are light and dark"
            )
        if not isinstance(colours, dict):
            raise ConfigurationError(
                f"VECTRIXDB_BRAND_PALETTE: {theme} is an object of colours by name"
            )
        for key, colour in colours.items():
            if key not in PALETTE:
                raise ConfigurationError(
                    f"VECTRIXDB_BRAND_PALETTE names {key!r} in {theme}. What it can set is {', '.join(PALETTE)}"
                )
            if not isinstance(colour, str) or not _HEX.match(colour.strip()):
                raise ConfigurationError(
                    f"VECTRIXDB_BRAND_PALETTE: {theme} {key} is {colour!r}. Write it as #rrggbb"
                )
        out[theme] = {key: colour.strip().lower() for key, colour in colours.items()}
        seen = {**_THEMES[theme], **out[theme]}
        for ink, grounds in _READS_ON.items():
            for ground in grounds:
                ratio = contrast(_rgb(seen[ink]), _rgb(seen[ground]))
                if ratio < 4.5:
                    raise ConfigurationError(
                        f"VECTRIXDB_BRAND_PALETTE: in the {theme} theme, {ink} {seen[ink]} on {ground} {seen[ground]} is {ratio:.1f} to 1, "
                        "and text needs 4.5 to 1 to be read. Darken or lighten one of them"
                    )
    return out


@dataclass
class Brand:
    name: str = "VectrixDB"
    logo: Optional[Logo] = None
    logo_dark: Optional[Logo] = None
    accent: Optional[str] = None
    #: Whose the line under the sidebar names.
    holder: str = HOLDER
    #: The name set as a wordmark: larger, and in the accent.
    wordmark: bool = False
    #: Colours by name, per theme, as the palette gave them.
    palette: dict = field(default_factory=dict)

    @property
    def custom(self) -> bool:
        return (
            self.name != "VectrixDB"
            or self.logo is not None
            or self.accent is not None
            or self.holder != HOLDER
            or self.wordmark
            or bool(self.palette)
        )

    @property
    def copyright(self) -> str:
        return f"© {YEAR} {self.holder}"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Brand":
        env = os.environ if env is None else env
        name = " ".join(str(env.get("VECTRIXDB_BRAND_NAME", "") or "").split())
        if len(name) > 40:
            raise ConfigurationError(
                "VECTRIXDB_BRAND_NAME is over 40 characters. It sits beside a logo in a narrow sidebar"
            )
        accent = str(env.get("VECTRIXDB_BRAND_ACCENT", "") or "").strip() or None
        if accent is not None and not _HEX.match(accent):
            raise ConfigurationError(
                f"VECTRIXDB_BRAND_ACCENT is {accent!r}. Write it as #rrggbb, for example #0b5cad"
            )
        logo, dark = _logo(env, "VECTRIXDB_BRAND_LOGO"), _logo(env, "VECTRIXDB_BRAND_LOGO_DARK")
        if dark is not None and logo is None:
            raise ConfigurationError(
                "VECTRIXDB_BRAND_LOGO_DARK is set and VECTRIXDB_BRAND_LOGO is not. Set the light one first"
            )
        if accent is not None and _is_red(_rgb(accent)):
            logger.warning(
                "the brand accent %s is close to the red that means something is wrong. It is allowed",
                accent,
            )
        holder = " ".join(str(env.get("VECTRIXDB_BRAND_COPYRIGHT", "") or "").split())
        if holder.startswith("©") or holder.lower().startswith(("(c)", "copyright")):
            raise ConfigurationError(
                f"VECTRIXDB_BRAND_COPYRIGHT is whose it is, {holder.lstrip('©').strip() or 'Northwind'}; the © and the year are added"
            )
        if len(holder) > 60:
            raise ConfigurationError(
                "VECTRIXDB_BRAND_COPYRIGHT is over 60 characters. It is one short line under the sidebar"
            )
        wordmark = str(env.get("VECTRIXDB_BRAND_WORDMARK", "") or "").strip().lower()
        if wordmark not in ("", "on", "off", "true", "false", "yes", "no", "1", "0"):
            raise ConfigurationError(f"VECTRIXDB_BRAND_WORDMARK is {wordmark!r}. It is on or off")
        return cls(
            name=name or "VectrixDB",
            logo=logo,
            logo_dark=dark,
            accent=accent.lower() if accent else None,
            holder=holder or HOLDER,
            wordmark=wordmark in ("on", "true", "yes", "1"),
            palette=_palette(env),
        )

    def tokens(self) -> dict:
        """The stylesheet's colours for each theme that the brand changes: the palette's, and the accent's worked out on its grounds."""
        out: dict = {"light": {}, "dark": {}}
        for theme, colours in self.palette.items():
            for key, colour in colours.items():
                out[theme][PALETTE[key]] = colour
                if key in ("success", "warning", "danger", "info"):
                    r, g, b = _rgb(colour)
                    out[theme][PALETTE[key] + "-bg"] = f"rgba({r}, {g}, {b}, {_TINT[theme]})"
            if "text" in colours:
                r, g, b = _rgb(colours["text"])
                out[theme]["--hover"] = (
                    f"rgba({r}, {g}, {b}, {0.04 if theme == 'light' else 0.045})"
                )
        if self.accent:
            rgb = _rgb(self.accent)
            on_fill = _ink_on(rgb)
            r, g, b = rgb
            for theme, target, tint in (
                ("light", (0, 0, 0), 0.12),
                ("dark", (255, 255, 255), 0.18),
            ):
                seen = {**_THEMES[theme], **self.palette.get(theme, {})}
                # As text, the accent sits on the page and on cards: moved only as far as it takes to read on both.
                shade = _toward(rgb, target, _rgb(seen["page"]), _rgb(seen["card"]))
                out[theme].update(
                    {
                        "--acc": _hex(shade),
                        "--acc-fill": self.accent,
                        "--acc-ink": on_fill,
                        "--acc-bg": f"rgba({r}, {g}, {b}, {tint})",
                    }
                )
        return {theme: tokens for theme, tokens in out.items() if tokens}

    def css(self) -> str:
        tokens = self.tokens()
        if not tokens:
            return ""
        dark = " ".join(f"{k}: {v};" for k, v in tokens.get("dark", {}).items())
        light = " ".join(f"{k}: {v};" for k, v in tokens.get("light", {}).items())
        return f':root {{ {dark} }}\n:root[data-theme="light"] {{ {light} }}'

    def code_look(self) -> dict:
        """How this dashboard draws the QR code an authenticator app scans, as ``vectrixdb.signin.qr.svg`` takes it.

        Dark on light on either theme: the light theme's text on its page, or
        VectrixDB's ink and paper when a palette's two are less than 7 to 1
        apart. In the middle, the logo, or else the layers mark in the accent
        with the ink that reads on it, as the sidebar has them.
        """
        light = {**_THEMES["light"], **self.palette.get("light", {})}
        ink, tile = light["text"], light["page"]
        if contrast(_rgb(ink), _rgb(tile)) < CODE_CONTRAST or _luminance(_rgb(ink)) > _luminance(
            _rgb(tile)
        ):
            ink, tile = _THEMES["light"]["text"], _THEMES["light"]["page"]
        fill = self.accent or _GOLD
        return {
            "ink": ink,
            "tile": tile,
            "logo": (self.logo.data, self.logo.mime) if self.logo else None,
            "mark_fill": fill,
            "mark_ink": _ink_on(_rgb(fill)),
        }

    def public(self) -> dict:
        """What the page is told about the brand."""
        return {
            "name": self.name,
            "custom": self.custom,
            "logo": "/brand/logo" if self.logo else None,
            "logo_dark": "/brand/logo-dark" if self.logo_dark else None,
            "accent": self.accent,
            "wordmark": self.wordmark,
            "copyright": self.copyright,
        }

    def banner(self) -> Optional[str]:
        """The line the server prints at start-up, when there is a brand to speak of."""
        if not self.custom:
            return None
        parts = [self.name + (" as a wordmark" if self.wordmark else "")]
        if self.logo:
            parts.append(self.logo.file + (f" and {self.logo_dark.file}" if self.logo_dark else ""))
        if self.accent:
            parts.append(f"accent {self.accent}")
        if self.palette:
            parts.append(f"a palette for {' and '.join(sorted(self.palette))}")
        if self.holder != HOLDER:
            parts.append(self.copyright)
        return ", ".join(parts)

    def render_index(self, page: str, visible: Optional[Callable[[str], str]] = None) -> str:
        """The dashboard's page with the brand in it before it is sent, so nothing flashes the default first.

        ``visible`` turns a route into the path a caller reaches it by, when a
        gateway publishes the server under paths of its own.
        """
        if not self.custom:
            return page
        at = visible or (lambda route: route)
        name = html.escape(self.name)
        page = page.replace("<title>VectrixDB</title>", f"<title>{name}</title>", 1)
        page = page.replace('id="brand-name">VectrixDB<', f'id="brand-name">{name}<')
        if self.holder != HOLDER:
            page = page.replace(
                'id="copyline">&copy; 2026 VectrixDB<',
                f'id="copyline">{html.escape(self.copyright)}<',
            )
        if self.wordmark:
            page = page.replace(
                '<div class="word"><b id="brand-name">',
                '<div class="word wordmark"><b id="brand-name">',
            )
        if self.logo:
            images = f'<img class="logo-light" src="{html.escape(at("/brand/logo"))}" alt="">' + (
                f'<img class="logo-dark" src="{html.escape(at("/brand/logo-dark"))}" alt="">'
                if self.logo_dark
                else ""
            )
            page = page.replace(
                '<div class="mark" id="brand-mark" data-icon="layers" data-size="16"></div>',
                f'<div class="mark logo" id="brand-mark">{images}</div>',
            )
            page = page.replace(
                '<link rel="icon" href="favicon.svg">',
                f'<link rel="icon" href="{html.escape(at("/brand/logo"))}" type="{self.logo.mime}">',
            )
        # Data, not a script: the page's policy runs no inline script, and a data
        # block is never run. A "<" is written escaped, so no name can end the block.
        told = self.public()
        for key in ("logo", "logo_dark"):
            if told.get(key):
                told[key] = at(told[key])
        data = json.dumps(told).replace("<", "\\u003c")
        script = f'<script type="application/json" id="vx-brand-data">{data}</script>'
        style = f'<style id="vx-brand">{self.css()}</style>' if self.accent or self.palette else ""
        return page.replace("</head>", f"{style}{script}</head>", 1)
