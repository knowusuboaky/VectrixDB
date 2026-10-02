"""A company's own name, logo and colour on the dashboard, and the line no brand removes.

What is being held to: a logo is told apart by its first bytes, never by its
file name, and is 512 KB at most; a PNG or JPEG is at least 64 pixels on its
shorter side; an SVG that could run or fetch anything is refused before the
server starts; the accent is #rrggbb, the shades made from it read against the
page's own grounds, and a red one is allowed with a warning; the page learns
the brand from Brand.public(), /brand.json and the page itself, and each logo
is served as an image that may run nothing; the dashboard page goes out
branded with an ETag of its own, so a browser holding the plain page is sent
the branded one instead of being told to keep what it has; "© 2026 VectrixDB"
is not a setting and stays; and with sign-in on, the version is for people who
signed in.
"""

from __future__ import annotations

import json
import logging
import re
import struct
import zlib
from pathlib import Path

import pytest

import vectrixdb
from vectrixdb import __version__
from vectrixdb.brand import _NIGHT, _PAPER, COPYRIGHT, MAX_LOGO_BYTES, Brand, _jpeg_size, _png_size, contrast
from vectrixdb.exceptions import ConfigurationError
from vectrixdb.signin import SignInConfig, totp

DASHBOARD = Path(vectrixdb.__file__).parent / "dashboard"
PAGE = (DASHBOARD / "index.html").read_text(encoding="utf-8")
PUBLIC = "https://vectors.example.test"
SECRET = "k" * 48
SETTINGS = (
    "VECTRIXDB_BRAND_NAME", "VECTRIXDB_BRAND_LOGO", "VECTRIXDB_BRAND_LOGO_DARK", "VECTRIXDB_BRAND_ACCENT",
    "VECTRIXDB_BRAND_WORDMARK", "VECTRIXDB_BRAND_COPYRIGHT", "VECTRIXDB_BRAND_PALETTE",
)
POLICY = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
COPYLINE = '<p class="copyline" id="copyline">&copy; 2026 VectrixDB</p>'


def create_app(**kwargs):
    """The server as it is now. Another test drops and re-imports ``vectrixdb.api``,
    and a name bound at the top of this file would go on pointing at the old copy."""
    from vectrixdb.api.server import create_app as current

    return current(**kwargs)


# ------------------------------------------------------------------ images


def png(width: int, height: int) -> bytes:
    """A real PNG of this size. ``_png_size`` reads the width and height from the
    IHDR chunk, which starts 8 bytes in, after the signature."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    rows = b"".join(b"\x00" + bytes(width * 4) for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def jpeg(width: int, height: int) -> bytes:
    """The head of a JPEG of this size. ``_jpeg_size`` walks the segments after the
    start-of-image marker, skipping each by its length, until a frame header, which
    holds the height before the width."""
    jfif = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    table = b"\xff\xdb" + struct.pack(">H", 67) + b"\x00" + bytes(range(1, 65))
    frame = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + jfif + table + frame + b"\xff\xd9"


PNG = png(64, 64)
JPEG = jpeg(64, 64)
PLAIN_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 64 64">'
    "<title>Acme</title>"
    '<defs><linearGradient id="g"><stop offset="0" stop-color="#0b5cad"/></linearGradient></defs>'
    '<a href="#top"><rect width="64" height="64" rx="12" fill="url(#g)"/></a>'
    "</svg>"
)


def svg(inner: str = "", *, attributes: str = "", before: str = "") -> str:
    return f'{before}<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"{attributes}>{inner}</svg>'


BOX = '<rect width="9" height="9"/>'
#: Each of these could run something or fetch something from elsewhere.
RUNS_OR_FETCHES = {
    "a script element": svg("<script>alert(1)</script>"),
    "a script element in capitals": svg("<SCRIPT>alert(1)</SCRIPT>"),
    "a script element with a space inside the bracket": svg("< script>alert(1)</script>"),
    "a handler on the svg element": svg(attributes=' onload="alert(1)"'),
    "a handler on a shape": svg('<rect width="9" height="9" onclick="alert(1)"/>'),
    "a handler after a line break": svg('<rect width="9" height="9"\n\tonmouseover = "alert(1)"/>'),
    "a javascript: link": svg(f'<a href="javascript:alert(1)">{BOX}</a>'),
    "a javascript: link written loosely": svg(f'<a xlink:href="JavaScript :alert(1)">{BOX}</a>'),
    "a foreignObject": svg('<foreignObject width="9" height="9"><p xmlns="http://www.w3.org/1999/xhtml">hi</p></foreignObject>'),
    "an iframe": svg("<iframe/>"),
    "an embed": svg("<embed/>"),
    "an object": svg("<object/>"),
    "an image element": svg('<image width="9" height="9" href="mark.png"/>'),
    "a use element": svg('<use href="#g"/>'),
    "an animation": svg('<animate attributeName="opacity" to="0"/>'),
    "a set element": svg('<set attributeName="opacity" to="0"/>'),
    "a handler element": svg('<handler type="application/ecmascript">alert(1)</handler>'),
    "a listener element": svg('<listener event="click" handler="#h"/>'),
    "a link to another site": svg(f'<a href="https://evil.example.test/">{BOX}</a>'),
    "a link without a scheme": svg(f'<a href="//evil.example.test/">{BOX}</a>'),
    "a data: address": svg(f'<a href="data:text/html,hi">{BOX}</a>'),
    "a source on another site": svg('<audio src="https://evil.example.test/a.mp3"/>'),
    "a style url on another site": svg("<style>rect { fill: url(https://evil.example.test/p.svg#p) }</style>"),
    "a style url quoted and without a scheme": svg("<rect width=\"9\" height=\"9\" style=\"fill: url( '//evil.example.test/p.svg#p')\"/>"),
    "a data: url in a style": svg('<style>rect { fill: url("data:image/svg+xml,x") }</style>'),
    "a style import": svg('<style>@import "https://evil.example.test/x.css";</style>'),
    "an entity declaration": svg(BOX, before='<?xml version="1.0"?>\n<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]>\n'),
}


def logo(tmp_path: Path, name: str, content) -> str:
    """Write a logo file and return its path, the way a setting names it."""
    path = tmp_path / name
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    return str(path)


def full_brand(tmp_path: Path) -> dict:
    return {
        "VECTRIXDB_BRAND_NAME": "Acme Bank",
        "VECTRIXDB_BRAND_LOGO": logo(tmp_path, "acme.svg", PLAIN_SVG),
        "VECTRIXDB_BRAND_LOGO_DARK": logo(tmp_path, "acme-dark.png", PNG),
        "VECTRIXDB_BRAND_ACCENT": "#0b5cad",
    }


def rgb(hex_colour: str) -> tuple:
    return tuple(int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))


# ----------------------------------------------------------------- sign-in


class Mail:
    def __init__(self):
        self.sent = []

    def __call__(self, to, subject, text):
        self.sent.append((to, subject, text))

    def token(self):
        return re.search(r"#/enrol\?token=([\w-]+)", self.sent[-1][2]).group(1)


def _config(root: Path, **over) -> SignInConfig:
    base = {
        "methods": ("email",),
        "secrets": (SECRET,),
        "public_url": PUBLIC,
        "users": (("ada@example.com", "admin"),),
        "store_path": root / "auth" / "signin.db",
        "access_log": root / "auth" / "access.jsonl",
        "sender": Mail(),
    }
    base.update(over)
    return SignInConfig(**base)


def enrol(client, config: SignInConfig, email: str) -> None:
    """The whole first visit, by email and an authenticator code."""
    assert client.post("/auth/email/begin", json={"email": email}).status_code == 200
    begun = client.post("/auth/email/enrol/begin", json={"token": config.sender.token()}).json()["data"]
    done = client.post("/auth/email/enrol/confirm", json={"ticket": begun["ticket"], "code": totp.code_at(begun["secret"], totp.step_now())})
    assert done.status_code == 200, done.text


# ---------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _only_the_brand_the_test_sets(monkeypatch):
    """A brand or sign-in set in the shell that runs the tests is not the one under test."""
    for name in (*SETTINGS, "VECTRIXDB_SIGNIN", "VECTRIXDB_API_KEY", "VECTRIXDB_READ_ONLY_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def serve(tmp_path, monkeypatch):
    """Build a server with its dashboard once the test has set the brand it wants. Nothing listens."""
    pytest.importorskip("fastapi", reason="the API extra is not installed")
    from fastapi.testclient import TestClient

    built = []

    def build(signin=None, **settings):
        for name, value in settings.items():
            monkeypatch.setenv(name, value)
        app = create_app(db_path=str(tmp_path / "db"), enable_dashboard=True, signin=signin)
        built.append(app)
        return TestClient(app, base_url=PUBLIC)

    yield build
    for app in built:
        if app.state.signin is not None:
            app.state.signin.close()


# ------------------------------------------------------------------- logos


class TestALogoIsToldApartByItsFirstBytes:
    @pytest.mark.parametrize(
        "name, content, mime",
        [
            ("logo.svg", PNG, "image/png"),
            ("logo.png", JPEG, "image/jpeg"),
            ("logo.jpg", PLAIN_SVG, "image/svg+xml"),
            ("logo", PNG, "image/png"),
        ],
        ids=["a PNG called .svg", "a JPEG called .png", "an SVG called .jpg", "no extension at all"],
    )
    def test_the_kind_comes_from_the_bytes_whatever_the_name_says(self, tmp_path, name, content, mime):
        brand = Brand.from_env({"VECTRIXDB_BRAND_LOGO": logo(tmp_path, name, content)})
        expected = content.encode("utf-8") if isinstance(content, str) else content
        assert brand.logo.mime == mime and brand.logo.data == expected

    @pytest.mark.parametrize(
        "content",
        [
            "﻿" + PLAIN_SVG,
            '<?xml version="1.0" encoding="UTF-8"?>\n<!-- ' + "exported by a drawing tool " * 30 + "-->\n" + PLAIN_SVG,
            "\n\n  " + PLAIN_SVG,
        ],
        ids=["after a byte order mark", "after a declaration and a long comment", "after blank lines"],
    )
    def test_an_svg_is_found_behind_what_drawing_tools_put_in_front_of_it(self, tmp_path, content):
        assert Brand.from_env({"VECTRIXDB_BRAND_LOGO": logo(tmp_path, "mark.svg", content)}).logo.mime == "image/svg+xml"

    @pytest.mark.parametrize(
        "name, content",
        [
            ("logo.png", b"GIF89a" + bytes(64)),
            ("logo.svg", "A logo, described in words."),
            ("logo.png", b"\x00\x01\x02 not an image at all"),
            ("logo.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 "),
        ],
        ids=["a GIF called .png", "words called .svg", "bytes called .png", "a WebP"],
    )
    def test_anything_else_is_refused_whatever_it_is_called(self, tmp_path, name, content):
        with pytest.raises(ConfigurationError, match="is not an SVG, PNG or JPEG image"):
            Brand.from_env({"VECTRIXDB_BRAND_LOGO": logo(tmp_path, name, content)})

    @pytest.mark.parametrize("start", [b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"], ids=["PNG", "JPEG"])
    def test_the_right_first_bytes_with_nothing_readable_after_them_are_refused(self, tmp_path, start):
        with pytest.raises(ConfigurationError, match="could not be read as an image"):
            Brand.from_env({"VECTRIXDB_BRAND_LOGO": logo(tmp_path, "logo", start + bytes(40))})

    def test_a_file_that_cannot_be_read_is_named_with_its_setting(self, tmp_path):
        with pytest.raises(ConfigurationError, match="cannot be read") as refused:
            Brand.from_env({"VECTRIXDB_BRAND_LOGO": str(tmp_path / "nowhere" / "logo.svg")})
        assert "VECTRIXDB_BRAND_LOGO" in str(refused.value)


class TestALogoIsSmall:
    def test_512_kb_is_allowed_and_one_byte_more_is_refused(self, tmp_path):
        head, tail = PLAIN_SVG[: -len("</svg>")], "</svg>"
        room = MAX_LOGO_BYTES - len(head) - len(tail) - len("<!---->")
        exact = f"{head}<!--{'x' * room}-->{tail}"
        assert len(exact.encode("utf-8")) == MAX_LOGO_BYTES == 512 * 1024
        assert Brand.from_env({"VECTRIXDB_BRAND_LOGO": logo(tmp_path, "exact.svg", exact)}).logo is not None
        over = f"{head}<!--{'x' * (room + 1)}-->{tail}"
        with pytest.raises(ConfigurationError, match="Keep it under 512 KB"):
            Brand.from_env({"VECTRIXDB_BRAND_LOGO": logo(tmp_path, "over.svg", over)})

    def test_a_raster_is_held_to_the_same_limit(self, tmp_path):
        with pytest.raises(ConfigurationError, match="Keep it under 512 KB"):
            Brand.from_env({"VECTRIXDB_BRAND_LOGO": logo(tmp_path, "big.png", PNG + bytes(MAX_LOGO_BYTES))})


class TestARasterIsLargeEnoughToStaySharp:
    def test_the_size_is_read_from_the_header(self):
        assert _png_size(png(300, 120)) == (300, 120)
        assert _jpeg_size(jpeg(300, 120)) == (300, 120), "a JPEG frame holds the height before the width"

    @pytest.mark.parametrize("make", [png, jpeg], ids=["PNG", "JPEG"])
    @pytest.mark.parametrize("width, height", [(63, 200), (200, 63), (16, 16)])
    def test_under_64_pixels_on_either_side_is_refused_with_the_size_found(self, tmp_path, make, width, height):
        with pytest.raises(ConfigurationError) as refused:
            Brand.from_env({"VECTRIXDB_BRAND_LOGO": logo(tmp_path, "logo", make(width, height))})
        assert f"the logo is {width}x{height}" in str(refused.value) and "at least 64 pixels" in str(refused.value)

    @pytest.mark.parametrize("make, mime", [(png, "image/png"), (jpeg, "image/jpeg")], ids=["PNG", "JPEG"])
    def test_64_pixels_square_is_enough(self, tmp_path, make, mime):
        assert Brand.from_env({"VECTRIXDB_BRAND_LOGO": logo(tmp_path, "logo", make(64, 64))}).logo.mime == mime

    def test_a_long_thin_logo_is_allowed_with_a_warning(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="vectrixdb.brand"):
            brand = Brand.from_env({"VECTRIXDB_BRAND_LOGO": logo(tmp_path, "wide.png", png(200, 64))})
        assert brand.logo is not None
        assert any("square logo" in r.getMessage() for r in caplog.records if r.name == "vectrixdb.brand")


class TestAnSvgThatCouldRunOrFetchAnythingIsRefused:
    @pytest.mark.parametrize("content", list(RUNS_OR_FETCHES.values()), ids=list(RUNS_OR_FETCHES))
    def test_it_is_refused_and_the_file_is_named(self, tmp_path, content):
        with pytest.raises(ConfigurationError) as refused:
            Brand.from_env({"VECTRIXDB_BRAND_LOGO": logo(tmp_path, "acme.svg", content)})
        assert str(refused.value).startswith("Brand logo refused: acme.svg")

    def test_a_plain_svg_with_only_references_inside_itself_is_accepted(self, tmp_path):
        brand = Brand.from_env({"VECTRIXDB_BRAND_LOGO": logo(tmp_path, "acme.svg", PLAIN_SVG)})
        assert brand.logo.mime == "image/svg+xml" and brand.logo.data == PLAIN_SVG.encode("utf-8")

    def test_the_dark_logo_is_held_to_the_same_rules(self, tmp_path):
        env = {
            "VECTRIXDB_BRAND_LOGO": logo(tmp_path, "light.svg", PLAIN_SVG),
            "VECTRIXDB_BRAND_LOGO_DARK": logo(tmp_path, "dark.svg", RUNS_OR_FETCHES["a script element"]),
        }
        with pytest.raises(ConfigurationError, match="Brand logo refused: dark.svg"):
            Brand.from_env(env)


class TestAMistakeStopsTheServer:
    @pytest.mark.parametrize("mistake", ["a logo with a script", "a logo too small", "a colour by name", "a dark logo alone"])
    def test_the_server_does_not_come_up_rather_than_come_up_wrong(self, tmp_path, monkeypatch, mistake):
        pytest.importorskip("fastapi", reason="the API extra is not installed")
        settings = {
            "a logo with a script": {"VECTRIXDB_BRAND_LOGO": logo(tmp_path, "acme.svg", RUNS_OR_FETCHES["a script element"])},
            "a logo too small": {"VECTRIXDB_BRAND_LOGO": logo(tmp_path, "acme.png", png(32, 32))},
            "a colour by name": {"VECTRIXDB_BRAND_ACCENT": "navy"},
            "a dark logo alone": {"VECTRIXDB_BRAND_LOGO_DARK": logo(tmp_path, "dark.svg", PLAIN_SVG)},
        }[mistake]
        for name, value in settings.items():
            monkeypatch.setenv(name, value)
        with pytest.raises(ConfigurationError):
            create_app(db_path=str(tmp_path / "db"), enable_dashboard=False)


# ------------------------------------------------------------ name, accent


class TestTheName:
    def test_spaces_are_tidied_and_a_blank_name_is_vectrixdb(self):
        assert Brand.from_env({"VECTRIXDB_BRAND_NAME": "  Acme \t  Bank "}).name == "Acme Bank"
        assert Brand.from_env({"VECTRIXDB_BRAND_NAME": "   "}).name == "VectrixDB"

    def test_forty_characters_fit_beside_the_logo_and_one_more_does_not(self):
        assert Brand.from_env({"VECTRIXDB_BRAND_NAME": "A" * 40}).name == "A" * 40
        with pytest.raises(ConfigurationError, match="over 40 characters"):
            Brand.from_env({"VECTRIXDB_BRAND_NAME": "A" * 41})


class TestTheAccent:
    @pytest.mark.parametrize("value", ["red", "#fff", "0b5cad", "#0b5cadff", "#0b5cag", "# 0b5cad", "rgb(11, 92, 173)"])
    def test_anything_but_a_hash_and_six_hex_digits_is_refused(self, value):
        with pytest.raises(ConfigurationError, match="#rrggbb"):
            Brand.from_env({"VECTRIXDB_BRAND_ACCENT": value})

    def test_it_is_kept_in_lower_case_and_blank_is_no_accent(self):
        assert Brand.from_env({"VECTRIXDB_BRAND_ACCENT": " #0B5CAD "}).accent == "#0b5cad"
        blank = Brand.from_env({"VECTRIXDB_BRAND_ACCENT": "   "})
        assert blank.accent is None and blank.tokens() == {} and blank.css() == "" and not blank.custom

    def test_contrast_is_the_wcag_ratio(self):
        white, black, grey = (255, 255, 255), (0, 0, 0), (0x77, 0x77, 0x77)
        assert contrast(white, black) == pytest.approx(21.0)
        assert contrast(black, white) == pytest.approx(21.0), "the order they are given in does not matter"
        assert contrast(grey, grey) == pytest.approx(1.0)
        assert contrast(grey, white) == pytest.approx(4.48, abs=0.005), "#777 on white, the well known near miss"

    @pytest.mark.parametrize(
        "accent", ["#0b5cad", "#e8a81a", "#c8102e", "#00a86b", "#6a1b9a", "#ffcc00", "#003366", "#767676", "#000000", "#ffffff"]
    )
    def test_the_shades_made_from_it_read_as_text_on_both_grounds(self, accent):
        tokens = Brand.from_env({"VECTRIXDB_BRAND_ACCENT": accent}).tokens()
        assert contrast(rgb(tokens["light"]["--acc"]), _PAPER) >= 4.5, "text on paper"
        assert contrast(rgb(tokens["dark"]["--acc"]), _NIGHT) >= 4.5, "text on the dark card"

    @pytest.mark.parametrize("accent", ["#0b5cad", "#e8a81a", "#c8102e", "#ffcc00", "#767676", "#000000", "#ffffff"])
    def test_the_ink_on_the_fill_is_black_or_white_whichever_reads_better(self, accent):
        tokens = Brand(accent=accent).tokens()
        ink = tokens["light"]["--acc-ink"]
        assert ink == tokens["dark"]["--acc-ink"] and ink in ("#ffffff", "#1a170f")
        other = "#1a170f" if ink == "#ffffff" else "#ffffff"
        assert contrast(rgb(accent), rgb(ink)) >= contrast(rgb(accent), rgb(other))
        assert contrast(rgb(accent), rgb(ink)) >= 3.0, "at least the WCAG floor for large text and controls"

    def test_an_accent_that_already_reads_on_paper_is_used_as_it_is(self):
        tokens = Brand(accent="#0b5cad").tokens()
        assert tokens["light"]["--acc"] == "#0b5cad"
        assert tokens["dark"]["--acc"] != "#0b5cad", "too dark for the dark card, so it is lifted toward white"

    def test_only_the_accent_moves_and_the_colours_that_mean_state_do_not(self):
        brand = Brand(accent="#0b5cad")
        tokens = brand.tokens()
        for theme in ("light", "dark"):
            assert set(tokens[theme]) == {"--acc", "--acc-fill", "--acc-ink", "--acc-bg"}
            assert tokens[theme]["--acc-fill"] == "#0b5cad"
        assert tokens["light"]["--acc-bg"] == "rgba(11, 92, 173, 0.12)" and tokens["dark"]["--acc-bg"] == "rgba(11, 92, 173, 0.18)"
        dark, light = brand.css().split("\n")
        assert dark.startswith(":root {") and light.startswith(':root[data-theme="light"] {')
        assert all(f"{k}: {v};" in dark for k, v in tokens["dark"].items())
        assert all(f"{k}: {v};" in light for k, v in tokens["light"].items())

    @pytest.mark.parametrize("accent", ["#d11a2a", "#c8102e", "#ff3b30"])
    def test_a_red_accent_is_allowed_with_a_warning(self, accent, caplog):
        with caplog.at_level(logging.WARNING, logger="vectrixdb.brand"):
            brand = Brand.from_env({"VECTRIXDB_BRAND_ACCENT": accent})
        assert brand.accent == accent and brand.tokens()["light"]["--acc-fill"] == accent
        warned = [r.getMessage() for r in caplog.records if r.name == "vectrixdb.brand"]
        assert any("close to the red" in message and accent in message for message in warned)

    @pytest.mark.parametrize("accent", ["#0b5cad", "#e8a81a", "#00a86b", "#ff00ff"])
    def test_an_accent_that_is_not_red_is_not_warned_about(self, accent, caplog):
        with caplog.at_level(logging.WARNING, logger="vectrixdb.brand"):
            Brand.from_env({"VECTRIXDB_BRAND_ACCENT": accent})
        assert not [r for r in caplog.records if r.name == "vectrixdb.brand"]


# ----------------------------------------------------- what the page is told


class TestWhatThePageIsTold:
    def test_with_nothing_set_it_is_vectrixdb_and_nothing_custom(self):
        brand = Brand.from_env({})
        assert brand.public() == {
            "name": "VectrixDB", "custom": False, "logo": None, "logo_dark": None, "accent": None,
            "wordmark": False, "copyright": "© 2026 VectrixDB",
        }
        assert brand.banner() is None

    @pytest.mark.parametrize("setting", ["name", "logo", "accent", "wordmark", "copyright", "palette"])
    def test_any_one_setting_makes_it_custom(self, tmp_path, setting):
        env = {
            "name": {"VECTRIXDB_BRAND_NAME": "Acme Bank"},
            "logo": {"VECTRIXDB_BRAND_LOGO": logo(tmp_path, "acme.svg", PLAIN_SVG)},
            "accent": {"VECTRIXDB_BRAND_ACCENT": "#0b5cad"},
            "wordmark": {"VECTRIXDB_BRAND_WORDMARK": "on"},
            "copyright": {"VECTRIXDB_BRAND_COPYRIGHT": "Acme Bank"},
            "palette": {"VECTRIXDB_BRAND_PALETTE": '{"light": {"page": "#f4f6f4"}}'},
        }[setting]
        assert Brand.from_env(env).public()["custom"] is True

    def test_a_brand_says_its_name_where_its_logos_are_and_its_accent(self, tmp_path):
        brand = Brand.from_env(full_brand(tmp_path))
        assert brand.public() == {
            "name": "Acme Bank", "custom": True, "logo": "/brand/logo", "logo_dark": "/brand/logo-dark", "accent": "#0b5cad",
            "wordmark": False, "copyright": "© 2026 VectrixDB",
        }
        assert brand.banner() == "Acme Bank, acme.svg and acme-dark.png, accent #0b5cad"

    def test_a_dark_logo_needs_the_light_one(self, tmp_path):
        with pytest.raises(ConfigurationError, match="Set the light one first"):
            Brand.from_env({"VECTRIXDB_BRAND_LOGO_DARK": logo(tmp_path, "dark.svg", PLAIN_SVG)})

    def test_brand_json_says_the_same_to_anybody_even_with_sign_in_on(self, serve, tmp_path):
        client = serve(signin=_config(tmp_path / "db"), **full_brand(tmp_path))
        reply = client.get("/brand.json")
        assert reply.status_code == 200
        assert reply.json() == client.app.state.brand.public() == Brand.from_env(full_brand(tmp_path)).public()
        assert client.get("/api/v1/collections").status_code == 401, "while everything else still asks who is there"

    def test_brand_json_with_nothing_set_is_the_default(self, serve):
        assert serve().get("/brand.json").json() == Brand().public()

    def test_sign_in_emails_come_under_the_brand_name(self, serve, tmp_path):
        config = _config(tmp_path / "db")
        client = serve(signin=config, VECTRIXDB_BRAND_NAME="Acme Bank")
        assert client.post("/auth/email/begin", json={"email": "ada@example.com"}).status_code == 200
        to, subject, text = config.sender.sent[-1]
        assert to == "ada@example.com" and subject == "Your sign-in link for Acme Bank" and "sign in to Acme Bank" in text


class TestEachLogoIsServedAsAnImageThatRunsNothing:
    @pytest.mark.parametrize("content, mime", [(PLAIN_SVG, "image/svg+xml"), (PNG, "image/png"), (JPEG, "image/jpeg")], ids=["SVG", "PNG", "JPEG"])
    def test_it_goes_out_with_its_type_no_sniffing_and_a_policy_that_runs_nothing(self, serve, tmp_path, content, mime):
        client = serve(VECTRIXDB_BRAND_LOGO=logo(tmp_path, "light", content), VECTRIXDB_BRAND_LOGO_DARK=logo(tmp_path, "dark", content))
        body = content.encode("utf-8") if isinstance(content, str) else content
        for route in ("/brand/logo", "/brand/logo-dark"):
            reply = client.get(route)
            assert reply.status_code == 200 and reply.content == body, route
            assert reply.headers["content-type"] == mime, route
            assert reply.headers["x-content-type-options"] == "nosniff", route
            assert reply.headers["content-security-policy"] == POLICY, route

    def test_the_dark_address_serves_the_dark_file(self, serve, tmp_path):
        client = serve(**full_brand(tmp_path))
        assert client.get("/brand/logo").content == PLAIN_SVG.encode("utf-8")
        dark = client.get("/brand/logo-dark")
        assert dark.content == PNG and dark.headers["content-type"] == "image/png"

    def test_with_no_logo_both_addresses_are_a_404(self, serve):
        client = serve(VECTRIXDB_BRAND_NAME="Acme Bank")
        assert client.get("/brand/logo").status_code == 404 and client.get("/brand/logo-dark").status_code == 404

    def test_a_light_logo_alone_leaves_the_dark_address_a_404(self, serve, tmp_path):
        client = serve(VECTRIXDB_BRAND_LOGO=logo(tmp_path, "acme.svg", PLAIN_SVG))
        assert client.get("/brand/logo").status_code == 200 and client.get("/brand/logo-dark").status_code == 404

    def test_the_sign_in_page_can_show_it_to_somebody_not_signed_in(self, serve, tmp_path):
        client = serve(signin=_config(tmp_path / "db"), VECTRIXDB_BRAND_LOGO=logo(tmp_path, "acme.png", PNG))
        assert client.get("/brand/logo").status_code == 200


# ------------------------------------------------------------------ the page


class TestThePageArrivesWithTheBrandInIt:
    def test_with_no_brand_the_page_is_untouched(self):
        assert Brand().render_index(PAGE) == PAGE

    def test_the_name_is_in_the_title_and_the_sidebar(self):
        page = Brand(name="Acme Bank").render_index(PAGE)
        assert "<title>Acme Bank</title>" in page and '<b id="brand-name">Acme Bank</b>' in page
        assert "<title>VectrixDB</title>" not in page and 'id="brand-name">VectrixDB<' not in page

    def test_the_name_is_escaped_where_it_becomes_markup(self):
        page = Brand(name="R&D <Labs>").render_index(PAGE)
        assert "<title>R&amp;D &lt;Labs&gt;</title>" in page
        assert '<b id="brand-name">R&amp;D &lt;Labs&gt;</b>' in page

    def test_the_mark_becomes_the_logo_and_the_tab_icon_follows(self, tmp_path):
        page = Brand.from_env({"VECTRIXDB_BRAND_LOGO": logo(tmp_path, "acme.png", PNG)}).render_index(PAGE)
        assert '<div class="mark logo" id="brand-mark"><img class="logo-light" src="/brand/logo" alt=""></div>' in page
        assert '<div class="mark" id="brand-mark" data-icon="layers" data-size="16"></div>' not in page
        assert '<link rel="icon" href="/brand/logo" type="image/png">' in page and 'href="favicon.svg"' not in page
        assert '<img class="logo-dark"' not in page

    def test_a_dark_logo_is_a_second_image_in_the_mark(self, tmp_path):
        page = Brand.from_env(full_brand(tmp_path)).render_index(PAGE)
        assert (
            '<div class="mark logo" id="brand-mark"><img class="logo-light" src="/brand/logo" alt="">'
            '<img class="logo-dark" src="/brand/logo-dark" alt=""></div>'
        ) in page
        assert '<link rel="icon" href="/brand/logo" type="image/svg+xml">' in page, "the tab shows the light one"

    def test_the_accent_and_the_brand_are_in_the_head_before_anything_paints(self):
        brand = Brand(name="Acme Bank", accent="#0b5cad")
        page = brand.render_index(PAGE)
        head = page[: page.index("</head>")]
        assert f'<style id="vx-brand">{brand.css()}</style>' in head
        told = re.search(r'<script type="application/json" id="vx-brand-data">(\{.*?\})</script>', head)
        assert told is not None and json.loads(told.group(1)) == brand.public()
        assert page.count('id="vx-brand"') == 1 and page.count('id="vx-brand-data"') == 1
        assert page.index('href="app.css"') < page.index('id="vx-brand"'), "after the stylesheet, so the accent wins"
        assert page.index('id="vx-brand-data"') < page.index('<script src="app.js">'), "told before the script asks"

    def test_without_an_accent_there_is_no_style_and_the_script_is_still_told(self):
        page = Brand(name="Acme Bank").render_index(PAGE)
        assert 'id="vx-brand"' not in page and 'id="vx-brand-data"' in page


class TestTheCopyrightLine:
    """Whose it is is a setting: "© 2026 Acme Bank". VectrixDB's own name, version and licence are under About."""

    def test_unset_it_reads_as_it_always_has(self):
        assert COPYRIGHT == "© 2026 VectrixDB" == Brand.from_env({}).copyright

    def test_the_page_carries_the_default_and_the_script_takes_the_brands(self):
        assert COPYLINE in PAGE
        assert f"const COPY = (BRAND && BRAND.copyright) || '{COPYRIGHT}';" in (DASHBOARD / "app.js").read_text(encoding="utf-8")

    def test_it_names_whoever_the_setting_names_with_the_year_and_the_sign_added(self):
        brand = Brand.from_env({"VECTRIXDB_BRAND_COPYRIGHT": "  Acme   Bank "})
        assert brand.copyright == "© 2026 Acme Bank" and brand.public()["copyright"] == "© 2026 Acme Bank"
        assert "© 2026 Acme Bank" in brand.banner()

    @pytest.mark.parametrize("given", ["© 2026 Acme", "(c) Acme", "Copyright Acme"])
    def test_the_sign_and_the_year_are_not_written_twice(self, given):
        with pytest.raises(ConfigurationError, match="the © and the year are added"):
            Brand.from_env({"VECTRIXDB_BRAND_COPYRIGHT": given})

    def test_sixty_characters_fit_and_one_more_does_not(self):
        assert Brand.from_env({"VECTRIXDB_BRAND_COPYRIGHT": "a" * 60}).holder == "a" * 60
        with pytest.raises(ConfigurationError, match="over 60 characters"):
            Brand.from_env({"VECTRIXDB_BRAND_COPYRIGHT": "a" * 61})

    def test_the_page_is_sent_with_the_brands_line_escaped(self):
        page = Brand.from_env({"VECTRIXDB_BRAND_COPYRIGHT": "R&D <Labs>"}).render_index(PAGE)
        assert '<p class="copyline" id="copyline">© 2026 R&amp;D &lt;Labs&gt;</p>' in page and COPYLINE not in page

    def test_every_other_setting_leaves_the_line_as_it_was(self, tmp_path):
        assert COPYLINE in Brand.from_env(full_brand(tmp_path)).render_index(PAGE)

    def test_it_goes_out_with_the_branded_page(self, serve, tmp_path):
        reply = serve(VECTRIXDB_BRAND_COPYRIGHT="Acme Bank", **full_brand(tmp_path)).get("/dashboard/")
        assert reply.status_code == 200 and "<title>Acme Bank</title>" in reply.text
        assert '<p class="copyline" id="copyline">© 2026 Acme Bank</p>' in reply.text


class TestTheWordmark:
    def test_on_the_name_is_marked_as_a_wordmark_where_it_is_drawn(self):
        brand = Brand.from_env({"VECTRIXDB_BRAND_NAME": "Acme", "VECTRIXDB_BRAND_WORDMARK": "on"})
        assert brand.wordmark and brand.public()["wordmark"] is True
        assert '<div class="word wordmark"><b id="brand-name">Acme</b></div>' in brand.render_index(PAGE)
        assert brand.banner().startswith("Acme as a wordmark")

    @pytest.mark.parametrize("given", ["", "off", "no", "0", "false"])
    def test_off_or_unset_it_is_the_name_as_ever(self, given):
        brand = Brand.from_env({"VECTRIXDB_BRAND_NAME": "Acme", "VECTRIXDB_BRAND_WORDMARK": given})
        assert not brand.wordmark and 'class="word wordmark"' not in brand.render_index(PAGE)

    def test_anything_else_is_refused(self):
        with pytest.raises(ConfigurationError, match="It is on or off"):
            Brand.from_env({"VECTRIXDB_BRAND_WORDMARK": "bold"})

    def test_the_stylesheet_sets_it_in_the_accent_and_larger(self):
        css = (DASHBOARD / "app.css").read_text(encoding="utf-8")
        rule = re.search(r"\.word\.wordmark b[^{]*\{([^}]*)\}", css)
        assert rule is not None and "var(--acc)" in rule.group(1) and "font-size: 20px" in rule.group(1)


class TestALogoInsideItsSetting:
    """For a host that keeps settings and no files: the image travels in the setting, checked like a file."""

    def data_url(self, content, base64_: bool = True) -> str:
        import base64
        from urllib.parse import quote

        raw = content.encode("utf-8") if isinstance(content, str) else content
        return "data:image/svg+xml;base64," + base64.b64encode(raw).decode() if base64_ else "data:image/svg+xml," + quote(raw)

    @pytest.mark.parametrize("base64_", [True, False])
    def test_an_svg_arrives_either_way_and_is_served_as_itself(self, base64_):
        brand = Brand.from_env({"VECTRIXDB_BRAND_LOGO": self.data_url(PLAIN_SVG, base64_)})
        assert brand.logo.data == PLAIN_SVG.encode("utf-8") and brand.logo.mime == "image/svg+xml"
        assert brand.logo.file == "a data: address" and brand.public()["logo"] == "/brand/logo"

    def test_the_type_it_claims_is_not_believed_the_bytes_are(self):
        import base64

        brand = Brand.from_env({"VECTRIXDB_BRAND_LOGO": "data:image/svg+xml;base64," + base64.b64encode(PNG).decode()})
        assert brand.logo.mime == "image/png"

    def test_an_svg_that_could_run_anything_is_refused_the_same_way(self):
        with pytest.raises(ConfigurationError, match="Brand logo refused: a data: address"):
            Brand.from_env({"VECTRIXDB_BRAND_LOGO": self.data_url(svg("<script>alert(1)</script>"))})

    @pytest.mark.parametrize("given, says", [
        ("data:image/svg+xml;base64", "no comma"),
        ("data:image/svg+xml;base64,@@@", "not base64"),
        ("data:text/plain,hello", "not an SVG, PNG or JPEG"),
    ])
    def test_a_broken_one_is_refused_with_what_is_wrong(self, given, says):
        with pytest.raises(ConfigurationError, match=says):
            Brand.from_env({"VECTRIXDB_BRAND_LOGO": given})

    def test_it_is_held_to_the_same_size(self):
        with pytest.raises(ConfigurationError, match="Keep it under 512 KB"):
            Brand.from_env({"VECTRIXDB_BRAND_LOGO": self.data_url(svg(BOX * 60000))})


class TestThePalette:
    HOUSE = {
        "light": {"page": "#f4f6f4", "sidebar": "#ffffff", "card": "#ffffff", "field": "#eef2ef", "subtle": "#e3ebe5",
                  "text": "#15241c", "text-2": "#33483c", "muted": "#4b6155", "line": "#dde6df", "border": "#cfdad2"},
        "dark": {"page": "#050806", "sidebar": "#141a16", "card": "#141a16", "field": "#1a221d", "subtle": "#212c25",
                 "text": "#eaf3ee", "text-2": "#b7c8bd", "muted": "#8fa598", "line": "#1e2a23", "border": "#2c3a31"},
    }

    def test_the_defaults_it_measures_against_are_the_stylesheets_own(self):
        from vectrixdb.brand import _THEMES, PALETTE

        css = (DASHBOARD / "app.css").read_text(encoding="utf-8")
        dark = css[css.index(":root {"): css.index(":root[data-theme=\"light\"] {")]
        light = css[css.index(":root[data-theme=\"light\"] {"): css.index("}", css.index(":root[data-theme=\"light\"] {"))]
        for theme, block in (("dark", dark), ("light", light)):
            for key, token in PALETTE.items():
                if token == "--field":
                    continue
                found = re.search(re.escape(token) + r": (#[0-9a-f]{6});", block)
                assert found is not None and found.group(1) == _THEMES[theme][key], f"{theme} {key}"

    def test_a_whole_palette_paints_every_token_it_names_and_the_hover_follows_the_text(self):
        brand = Brand.from_env({"VECTRIXDB_BRAND_PALETTE": json.dumps(self.HOUSE)})
        tokens = brand.tokens()
        assert tokens["light"]["--g0"] == "#f4f6f4" and tokens["light"]["--field"] == "#eef2ef" and tokens["light"]["--line2"] == "#cfdad2"
        assert tokens["dark"]["--t1"] == "#eaf3ee" and tokens["dark"]["--hover"] == "rgba(234, 243, 238, 0.045)"
        assert tokens["light"]["--hover"] == "rgba(21, 36, 28, 0.04)"
        css = brand.css()
        assert css.startswith(":root { ") and ':root[data-theme="light"] { --g0: #f4f6f4;' in css
        assert brand.banner() == "VectrixDB, a palette for dark and light"

    def test_a_file_holds_it_as_well_as_the_setting(self, tmp_path):
        where = tmp_path / "palette.json"
        where.write_text(json.dumps(self.HOUSE), encoding="utf-8")
        assert Brand.from_env({"VECTRIXDB_BRAND_PALETTE": str(where)}).palette == self.HOUSE

    def test_a_data_address_holds_it_for_a_host_with_no_files(self):
        import base64

        given = "data:application/json;base64," + base64.b64encode(json.dumps(self.HOUSE).encode()).decode()
        assert Brand.from_env({"VECTRIXDB_BRAND_PALETTE": given}).palette == self.HOUSE

    def test_what_it_leaves_out_stays_vectrixdbs(self):
        brand = Brand.from_env({"VECTRIXDB_BRAND_PALETTE": '{"light": {"page": "#fafafa"}}'})
        assert brand.tokens() == {"light": {"--g0": "#fafafa"}}

    def test_a_state_colour_brings_its_tint(self):
        brand = Brand.from_env({"VECTRIXDB_BRAND_PALETTE": '{"light": {"danger": "#b00020"}}'})
        assert brand.tokens()["light"] == {"--bad": "#b00020", "--bad-bg": "rgba(176, 0, 32, 0.1)"}

    @pytest.mark.parametrize("given, says", [
        ('{"sepia": {}}', "The themes are light and dark"),
        ('{"light": {"hero": "#ffffff"}}', "What it can set is page, sidebar"),
        ('{"light": {"page": "white"}}', "Write it as #rrggbb"),
        ('{"light": ["#ffffff"]}', "an object of colours by name"),
        ("{}", 'an object with "light", "dark" or both'),
        ("{not json", "must be JSON"),
    ])
    def test_a_mistake_is_refused_with_what_is_wrong(self, given, says):
        with pytest.raises(ConfigurationError, match=re.escape(says)):
            Brand.from_env({"VECTRIXDB_BRAND_PALETTE": given})

    def test_a_file_that_cannot_be_read_is_named(self, tmp_path):
        with pytest.raises(ConfigurationError, match="cannot be read"):
            Brand.from_env({"VECTRIXDB_BRAND_PALETTE": str(tmp_path / "missing.json")})

    @pytest.mark.parametrize("theme, colours, pair", [
        ("light", {"muted": "#8fa598"}, "muted #8fa598 on page #f7f5f1"),
        ("light", {"page": "#6f6b61"}, "on page #6f6b61"),
        ("dark", {"text": "#333333"}, "text #333333 on page #120f09"),
        ("light", {"success": "#2fbf71"}, "success #2fbf71 on card #ffffff"),
        ("light", {"field": "#1a170f"}, "text #1a170f on field #1a170f"),
    ])
    def test_an_ink_that_cannot_be_read_on_its_ground_stops_the_start_with_the_pair_named(self, theme, colours, pair):
        with pytest.raises(ConfigurationError, match=re.escape(pair)):
            Brand.from_env({"VECTRIXDB_BRAND_PALETTE": json.dumps({theme: colours})})

    def test_the_accent_is_worked_out_on_the_palettes_grounds(self):
        brand = Brand.from_env({"VECTRIXDB_BRAND_ACCENT": "#1f6f54", "VECTRIXDB_BRAND_PALETTE": json.dumps(self.HOUSE)})
        tokens = brand.tokens()
        for theme in ("light", "dark"):
            shade = rgb(tokens[theme]["--acc"])
            for ground in ("page", "card"):
                assert contrast(shade, rgb(self.HOUSE[theme][ground])) >= 4.5, f"{theme} accent on {ground}"
        assert tokens["light"]["--acc"] == "#1f6f54", "it reads on the page already, so it is used as it is"

    def test_the_page_carries_it_in_the_head_after_the_stylesheet(self):
        brand = Brand.from_env({"VECTRIXDB_BRAND_PALETTE": json.dumps(self.HOUSE)})
        page = brand.render_index(PAGE)
        assert f'<style id="vx-brand">{brand.css()}</style>' in page
        assert page.index('href="app.css"') < page.index('id="vx-brand"')

    def test_inputs_have_a_ground_of_their_own_that_follows_the_sidebar_unless_set(self):
        css = (DASHBOARD / "app.css").read_text(encoding="utf-8")
        assert css.count("--field: var(--g1);") == 2, "in both themes"
        assert re.search(r"input, select, textarea \{[^}]*background: var\(--field\)", css)


class TestTheDashboardPageGoesOutBranded:
    @pytest.mark.parametrize("path", ["/dashboard/", "/dashboard/index.html"])
    def test_it_has_an_etag_of_its_own_and_asking_with_it_is_a_304(self, serve, tmp_path, path):
        client = serve(**full_brand(tmp_path))
        first = client.get(path)
        assert first.status_code == 200 and first.text == client.app.state.brand.render_index(PAGE)
        assert first.headers["cache-control"] == "no-cache" and first.headers["content-type"].startswith("text/html")
        again = client.get(path, headers={"If-None-Match": first.headers["etag"]})
        assert again.status_code == 304 and again.content == b""
        assert again.headers["etag"] == first.headers["etag"] and again.headers["cache-control"] == "no-cache"

    def test_a_browser_holding_the_plain_page_is_sent_the_branded_one(self, serve):
        plain = serve().get("/dashboard/")
        assert plain.status_code == 200 and "<title>VectrixDB</title>" in plain.text
        branded = serve(VECTRIXDB_BRAND_NAME="Acme Bank")
        held = [
            {"If-None-Match": plain.headers["etag"]},
            {"If-Modified-Since": plain.headers["last-modified"]},
            {"If-Modified-Since": "Fri, 01 Jan 2100 00:00:00 GMT"},
            {"If-None-Match": plain.headers["etag"], "If-Modified-Since": plain.headers["last-modified"]},
        ]
        for asked in held:
            reply = branded.get("/dashboard/", headers=asked)
            assert reply.status_code == 200, f"told to keep the plain page: {asked}"
            assert "<title>Acme Bank</title>" in reply.text and reply.headers["etag"] != plain.headers["etag"]

    def test_a_different_brand_is_a_different_page_to_the_browser(self, serve):
        acme = serve(VECTRIXDB_BRAND_NAME="Acme Bank").get("/dashboard/")
        zenith = serve(VECTRIXDB_BRAND_NAME="Zenith Bank").get("/dashboard/", headers={"If-None-Match": acme.headers["etag"]})
        assert zenith.status_code == 200 and "<title>Zenith Bank</title>" in zenith.text

    def test_the_other_dashboard_files_keep_the_usual_answer(self, serve):
        client = serve(VECTRIXDB_BRAND_NAME="Acme Bank")
        first = client.get("/dashboard/app.js")
        assert first.status_code == 200 and first.headers["cache-control"] == "no-cache"
        assert client.get("/dashboard/app.js", headers={"If-None-Match": first.headers["etag"]}).status_code == 304



# --------------------------------------------------- the code an authenticator app scans


class TestTheCodeAnAuthenticatorAppScans:
    """Dark on light on either theme, the dashboard's own mark in the middle, and never too faint for a camera."""

    def test_with_no_brand_it_is_vectrixdbs_ink_paper_and_mark(self):
        from vectrixdb.signin import qr

        assert Brand().code_look() == {"ink": qr.INK, "tile": qr.TILE, "logo": None, "mark_fill": qr.MARK_FILL, "mark_ink": qr.MARK_INK}
        assert (qr.INK, qr.TILE) == ("#1a170f", "#f7f5f1"), "the light theme's text and page, as app.css has them"

    def test_a_palette_draws_it_in_the_light_themes_text_on_its_page(self):
        look = Brand.from_env({"VECTRIXDB_BRAND_PALETTE": json.dumps(TestThePalette.HOUSE)}).code_look()
        assert (look["ink"], look["tile"]) == ("#15241c", "#f4f6f4"), "the light theme's, whichever theme is showing"

    def test_text_and_page_under_7_to_1_get_vectrixdbs_ink_and_paper_since_a_camera_sees_less_than_an_eye(self):
        brand = Brand.from_env({"VECTRIXDB_BRAND_PALETTE": '{"light": {"text": "#666666", "page": "#ffffff"}}'})
        assert 4.5 <= contrast(rgb("#666666"), rgb("#ffffff")) < 7, "readable as text, and still too faint for the code"
        assert (brand.code_look()["ink"], brand.code_look()["tile"]) == ("#1a170f", "#f7f5f1")

    def test_the_logo_goes_in_the_middle(self, tmp_path):
        brand = Brand.from_env({"VECTRIXDB_BRAND_LOGO": logo(tmp_path, "acme.svg", PLAIN_SVG)})
        assert brand.code_look()["logo"] == (PLAIN_SVG.encode("utf-8"), "image/svg+xml")
        assert Brand.from_env({"VECTRIXDB_BRAND_LOGO": logo(tmp_path, "acme.png", PNG)}).code_look()["logo"] == (PNG, "image/png")

    @pytest.mark.parametrize("accent, ink", [("#0b5cad", "#ffffff"), ("#f5c542", "#1a170f")])
    def test_without_a_logo_the_mark_is_the_accent_with_the_ink_the_buttons_have(self, accent, ink):
        brand = Brand.from_env({"VECTRIXDB_BRAND_ACCENT": accent})
        assert (brand.code_look()["mark_fill"], brand.code_look()["mark_ink"]) == (accent, ink)
        assert brand.tokens()["light"]["--acc-ink"] == ink

    def test_the_code_handed_out_at_enrolment_is_drawn_in_the_brand(self, serve, tmp_path):
        pytest.importorskip("cryptography", reason="the signin extra is not installed")
        pytest.importorskip("qrcode", reason="the signin extra is not installed")
        import base64

        from vectrixdb.signin import qr

        config = _config(tmp_path / "db")
        client = serve(signin=config, VECTRIXDB_BRAND_NAME="Harbour Labs", VECTRIXDB_BRAND_LOGO=logo(tmp_path, "harbour.svg", PLAIN_SVG),
                       VECTRIXDB_BRAND_PALETTE=json.dumps(TestThePalette.HOUSE))
        assert client.post("/auth/email/begin", json={"email": "ada@example.com"}).status_code == 200
        begun = client.post("/auth/email/enrol/begin", json={"token": config.sender.token()}).json()["data"]
        assert begun["uri"].startswith("otpauth://totp/Harbour%20Labs%3Aada%40example.com?") and "issuer=Harbour+Labs" in begun["uri"]
        assert begun["qr"] == qr.svg(begun["uri"], **client.app.state.brand.code_look())
        encoded = base64.b64encode(PLAIN_SVG.encode("utf-8")).decode("ascii")
        assert f'<image href="data:image/svg+xml;base64,{encoded}"' in begun["qr"]
        assert '<path fill="#15241c" d="M' in begun["qr"] and 'rx="3" fill="#f4f6f4"' in begun["qr"]

    def test_the_page_is_told_a_code_can_be_drawn(self, serve, tmp_path):
        pytest.importorskip("cryptography", reason="the signin extra is not installed")
        pytest.importorskip("qrcode", reason="the signin extra is not installed")
        client = serve(signin=_config(tmp_path / "db"))
        told = client.get("/auth/me")
        assert told.status_code == 401 and told.json()["data"]["methods"]["email"]["qr"] is True, "somebody not signed in is told"


# ---------------------------------------------------------------- the version


class TestTheVersionIsForPeopleWhoSignedIn:
    def test_with_sign_in_on_somebody_not_signed_in_is_not_told(self, serve, tmp_path):
        client = serve(signin=_config(tmp_path / "db"))
        root = client.get("/")
        assert root.status_code == 200 and "version" not in root.json()
        spec = client.get("/openapi.json")
        assert spec.status_code == 200 and spec.json()["info"]["version"] != __version__
        assert __version__ not in spec.text

    def test_somebody_signed_in_is_told(self, serve, tmp_path):
        pytest.importorskip("cryptography", reason="the signin extra is not installed")
        config = _config(tmp_path / "db")
        client = serve(signin=config)
        enrol(client, config, "ada@example.com")
        assert client.get("/").json()["version"] == __version__

    def test_with_sign_in_off_it_is_said_as_it_always_was(self, serve):
        client = serve()
        assert client.get("/").json()["version"] == __version__
        assert client.get("/openapi.json").json()["info"]["version"] == __version__
