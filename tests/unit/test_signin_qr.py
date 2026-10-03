"""The QR code an authenticator app scans: every module where the standard puts it, drawn as the dashboard's own.

What is being held to: the code is level Q, in the smallest version that
holds the address; every dark module outside the eyes and the middle is a dot
on its own centre, and read back from the markup the drawing is the code, none
added and none lost; the eyes sample as the finder pattern, their corners
softened by half a module; the middle is clear, odd, centred, at most 7.5 per
cent of the code and clear of the eyes, the timing lines and the format and
version information, and a code too small for one has none; the dots are dark
on a light tile with a margin of three modules; no code is drawn less dense
than the one that was read back, 200 pixels for 55 modules; the logo travels
inside as an image and none of its own markup reaches the page; without a logo
the middle is the layers mark in the colours given; a colour or an image that
is not one is refused; the label is escaped; and without the qrcode package
there is no picture.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import base64
import re
import sys

import pytest

from vectrixdb.signin import qr, totp

pytest.importorskip("qrcode", reason="the signin extra is not installed")

SECRET = "K5QW2Y3DPFHK7M4TJBSWY3DPEHPK3PXP"
ADDRESS = totp.provisioning_uri(SECRET, "ama.kyei@company.com")
LONG_ADDRESS = totp.provisioning_uri(
    SECRET, "akosua.boatemaa-mensah@contractors.company.com", issuer="Harbour Labs Wealth"
)
LOGO = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 52 52"><circle cx="26" cy="26" r="26" fill="#1f6f54"/></svg>'
EYES = ((0, 0), (0, -7), (-7, 0))


def read_back(svg: str) -> dict:
    """What the drawing says, from its markup alone: the modules it shows, its eyes, its colours, its middle."""
    view = int(re.search(r'viewBox="0 0 (\d+) \1"', svg).group(1))
    n = view - 2 * qr.QUIET
    tile = re.search(
        rf'<rect width="{view}" height="{view}" rx="{qr.TILE_CORNER}" fill="(#[0-9a-f]{{6}})"/>',
        svg,
    ).group(1)
    ink, path = re.search(r'<path fill="(#[0-9a-f]{6})" d="([^"]*)"/>', svg).groups()
    shown = [[False] * n for _ in range(n)]
    x = y = 0.0
    for move, dx, dy in re.findall(r"([Mm])(-?[\d.]+) (-?[\d.]+)a", path):
        x, y = (float(dx), float(dy)) if move == "M" else (x + float(dx), y + float(dy))
        c, r = x + qr.DOT / 2 - qr.QUIET - 0.5, y - qr.QUIET - 0.5
        assert abs(c - round(c)) < 1e-9 and abs(r - round(r)) < 1e-9, (
            f"a dot off its module's centre at {x}, {y}"
        )
        assert not shown[round(r)][round(c)], "one module drawn twice"
        shown[round(r)][round(c)] = True
    rects = re.findall(
        r'<rect x="(\d+)" y="(\d+)" width="(\d)" height="\3" rx="([\d.]+)" fill="(#[0-9a-f]{6})"/>',
        svg,
    )
    return {
        "n": n,
        "tile": tile,
        "ink": ink,
        "shown": shown,
        "rects": rects,
        "dots": len(re.findall(r"a\.46 \.46 0 1 0 \.92 0", path)),
    }


def finder(r: int, c: int) -> bool:
    """The finder pattern at (r, c) of its 7 by 7 square: a dark ring, a light ring, a dark 3 by 3 in the middle."""
    return r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4)


def middle(n: int) -> range:
    side = qr.mark_side(n)
    low = (n - side) // 2
    return range(low, low + side)


class TestTheModulesAreTheStandards:
    def test_it_is_level_q_in_the_smallest_version_that_holds_the_address(self):
        import qrcode
        from qrcode.constants import ERROR_CORRECT_Q

        code = qrcode.QRCode(error_correction=ERROR_CORRECT_Q, border=0)
        code.add_data(ADDRESS)
        code.make(fit=True)
        assert qr.matrix(ADDRESS) == [[bool(m) for m in row] for row in code.get_matrix()]
        assert len(qr.matrix(ADDRESS)) == 49, "the usual address is version 8"

    def test_read_back_from_its_markup_the_drawing_is_the_code_outside_the_middle(self):
        """Every module a reader samples is where the standard puts it: none added, none lost."""
        modules = qr.matrix(ADDRESS)
        seen = read_back(qr.svg(ADDRESS))
        n, hidden = seen["n"], middle(seen["n"])
        assert n == len(modules)
        for r in range(n):
            for c in range(n):
                if r in hidden and c in hidden:
                    assert not seen["shown"][r][c], "nothing is drawn under the mark"
                    continue
                eye = next(
                    (
                        (r - er % n, c - ec % n)
                        for er, ec in EYES
                        if 0 <= r - er % n < 7 and 0 <= c - ec % n < 7
                    ),
                    None,
                )
                drawn = finder(*eye) if eye else seen["shown"][r][c]
                assert drawn == modules[r][c], f"module {r},{c}"

    def test_the_eyes_are_the_finder_pattern_with_softened_corners(self):
        seen = read_back(qr.svg(ADDRESS))
        n, q = seen["n"], qr.QUIET
        expected = []
        for er, ec in EYES:
            r, c = er % n, ec % n
            expected += [
                (str(c + q), str(r + q), "7", "0.5", seen["ink"]),
                (str(c + q + 1), str(r + q + 1), "5", "0.34", seen["tile"]),
                (str(c + q + 2), str(r + q + 2), "3", "0.23", seen["ink"]),
            ]
        assert seen["rects"] == expected
        assert qr.EYE_CORNERS[0] == 0.5, (
            "half a module: rounder eyes were read by one reader in three"
        )

    def test_the_dots_are_dark_on_a_light_tile_with_a_margin_of_three(self):
        seen = read_back(qr.svg(ADDRESS))
        assert (seen["ink"], seen["tile"]) == (qr.INK, qr.TILE)
        assert qr.QUIET == 3 and f'viewBox="0 0 {seen["n"] + 6} {seen["n"] + 6}"' in qr.svg(ADDRESS)
        assert qr.DOT == 0.92


class TestTheMiddle:
    @pytest.mark.parametrize("version", range(2, 41))
    def test_it_is_odd_and_small_and_clear_of_every_pattern_a_reader_needs(self, version):
        n = 17 + 4 * version
        side = qr.mark_side(n)
        assert side % 2 == 1, "odd, so it sits on the centre module"
        assert side * side <= qr.MARK_SHARE * n * n, "within what level Q leaves room to repair"
        hidden = middle(n)
        # The eyes and their separators, the timing lines, the format and the version information
        # all lie within nine modules of an edge.
        assert hidden.start >= 9 and hidden.stop - 1 <= n - 10
        assert (hidden.start + hidden.stop - 1) / 2 == (n - 1) / 2, "centred"

    def test_a_code_too_small_to_keep_one_clear_has_none(self):
        assert qr.mark_side(21) == 0
        svg = qr.svg("hi")
        assert "<image" not in svg and "<g transform" not in svg
        seen = read_back(svg)
        modules = qr.matrix("hi")
        assert sum(map(sum, seen["shown"])) == sum(
            modules[r][c]
            for r in range(21)
            for c in range(21)
            if not any(0 <= r - er % 21 < 7 and 0 <= c - ec % 21 < 7 for er, ec in EYES)
        ), "every dot is drawn when there is no mark"

    def test_the_logo_goes_inside_as_an_image_and_none_of_its_markup_reaches_the_page(self):
        svg = qr.svg(ADDRESS, logo=(LOGO, "image/svg+xml"))
        image = re.search(
            r'<image href="data:image/svg\+xml;base64,([A-Za-z0-9+/=]+)" x="(\d+)" y="\2" width="(\d+)" height="\3" preserveAspectRatio="xMidYMid meet"/>',
            svg,
        )
        assert image is not None and base64.b64decode(image.group(1)) == LOGO
        assert "<circle" not in svg and "#1f6f54" not in svg, (
            "the logo is an image, never markup in the page"
        )
        n = 49
        assert (
            int(image.group(2)) == middle(n).start + 1 + qr.QUIET
            and int(image.group(3)) == qr.mark_side(n) - 2
        )
        assert "<g transform" not in svg, "the logo takes the mark's place"

    def test_a_png_or_jpeg_logo_goes_in_the_same_way(self):
        for mime in ("image/png", "image/jpeg"):
            assert f'<image href="data:{mime};base64,' in qr.svg(
                ADDRESS, logo=(b"\x89PNG....", mime)
            )

    def test_without_a_logo_it_is_the_layers_mark_in_the_colours_given(self):
        svg = qr.svg(ADDRESS, mark_fill="#0B5CAD", mark_ink="#FFFFFF")
        assert (
            '<rect width="32" height="32" rx="8" fill="#0b5cad"/>' in svg
            and 'stroke="#ffffff"' in svg
        )
        assert svg.count("<path d=") == 3, "the three layers"
        default = qr.svg(ADDRESS)
        assert f'fill="{qr.MARK_FILL}"' in default and f'stroke="{qr.MARK_INK}"' in default


class TestTheSize:
    def test_the_usual_address_is_200_pixels(self):
        assert qr.size(49) == 200 and 'width="200" height="200"' in qr.svg(ADDRESS)

    @pytest.mark.parametrize("n", [49, 53, 57, 61, 65, 77, 97])
    def test_no_code_is_drawn_less_dense_than_the_one_that_was_read(self, n):
        assert qr.size(n) * qr.READ_MODULES >= (n + 2 * qr.QUIET) * qr.READ_PIXELS
        assert qr.size(n) >= qr.MIN_SIZE

    def test_a_long_address_is_drawn_larger(self):
        n = len(qr.matrix(LONG_ADDRESS))
        assert n > 49 and f'width="{qr.size(n)}"' in qr.svg(LONG_ADDRESS) and qr.size(n) > 200


class TestWhatIsRefused:
    @pytest.mark.parametrize("keyword", ["ink", "tile", "mark_fill", "mark_ink"])
    @pytest.mark.parametrize(
        "value", ["red", "#fff", "#12345g", "", "#1a170f; fill:url(x)", "#1a170f\n", '#1a170f"']
    )
    def test_a_colour_that_is_not_one(self, keyword, value):
        with pytest.raises(ValueError, match="Write it as #rrggbb"):
            qr.svg(ADDRESS, **{keyword: value})

    def test_an_image_that_is_not_svg_png_or_jpeg(self):
        with pytest.raises(ValueError, match="image/gif"):
            qr.svg(ADDRESS, logo=(b"GIF89a", "image/gif"))

    def test_colours_are_kept_in_lower_case(self):
        seen = read_back(qr.svg(ADDRESS, ink="#0F1F33", tile="#F5F7FB"))
        assert (seen["ink"], seen["tile"]) == ("#0f1f33", "#f5f7fb")

    def test_the_label_is_escaped(self):
        assert 'aria-label="Ama &quot;K&quot; &lt;b&gt;"' in qr.svg(ADDRESS, label='Ama "K" <b>')


class TestWithoutTheQrcodePackage:
    def test_there_is_no_picture_and_the_page_is_told(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "qrcode", None)
        assert qr.available() is False
        assert (
            qr.matrix(ADDRESS) is None and qr.svg(ADDRESS) is None and totp.qr_svg(ADDRESS) is None
        )

    def test_with_it_the_page_is_told_it_can_show_one(self):
        assert qr.available() is True


class TestTheAddress:
    def test_it_leaves_out_what_every_app_assumes(self):
        assert (
            ADDRESS
            == f"otpauth://totp/VectrixDB%3Aama.kyei%40company.com?secret={SECRET}&issuer=VectrixDB"
        )
        assert "algorithm" not in ADDRESS and "digits" not in ADDRESS and "period" not in ADDRESS
        assert (totp.DIGITS, totp.PERIOD) == (6, 30), "what an app assumes is what the codes are"

    def test_the_old_name_draws_the_same_code(self):
        assert totp.qr_svg(ADDRESS) == qr.svg(ADDRESS)
        assert totp.qr_svg(ADDRESS, ink="#0f1f33") == qr.svg(ADDRESS, ink="#0f1f33")
