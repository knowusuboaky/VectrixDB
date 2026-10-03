"""The dashboard at three widths, held without a browser.

A browser sweep found what was wrong: at 375 pixels the Overview did not
stack, column headings shrank to a letter, a pill sat on top of a command and
Settings scrolled sideways. The causes were structural, and a structural
fault can be tested structurally: twenty-five table layouts were inline
percentage grids, which no stylesheet can override, there was one breakpoint
and it only hid the sidebar, and buttons never wrapped.

This does not prove the page looks right. It proves the things that made it
impossible for the page to look right cannot come back unnoticed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DASH = Path(__file__).resolve().parents[2] / "vectrixdb" / "dashboard"
JS = (DASH / "app.js").read_text(encoding="utf-8")
# The Evaluate pages are a script of their own, held to the same rules.
EVAL = (DASH / "evaluate.js").read_text(encoding="utf-8")
# And so is its Chunking tab.
CHUNK = (DASH / "chunking.js").read_text(encoding="utf-8")
TRENDS = (DASH / "trends.js").read_text(encoding="utf-8")
HTML = (DASH / "index.html").read_text(encoding="utf-8")
CSS = (DASH / "app.css").read_text(encoding="utf-8")


def block(max_width: int) -> str:
    """The body of the first ``@media (max-width: Npx)`` block."""
    start = CSS.index(f"@media (max-width: {max_width}px)")
    depth, i = 0, CSS.index("{", start)
    for j in range(i, len(CSS)):
        depth += {"{": 1, "}": -1}.get(CSS[j], 0)
        if depth == 0:
            return CSS[i + 1 : j]
    raise AssertionError("unclosed media block")


def all_blocks(max_width: int) -> str:
    out, at = [], 0
    marker = f"@media (max-width: {max_width}px)"
    while (at := CSS.find(marker, at)) != -1:
        depth, i = 0, CSS.index("{", at)
        for j in range(i, len(CSS)):
            depth += {"{": 1, "}": -1}.get(CSS[j], 0)
            if depth == 0:
                out.append(CSS[i + 1 : j])
                at = j
                break
    return "\n".join(out)


class TestNoLayoutHidesInAnAttribute:
    def test_no_inline_grid_is_left(self):
        for name, source in (
            ("app.js", JS),
            ("evaluate.js", EVAL),
            ("chunking.js", CHUNK),
            ("trends.js", TRENDS),
            ("index.html", HTML),
        ):
            found = re.findall(r'style="[^"]*grid-template-columns[^"]*"', source)
            assert found == [], (
                f"{name} sets a grid inline, where no breakpoint can reach it: {found[:2]}"
            )
        assert (
            "grid-template-columns" not in JS
            and "grid-template-columns" not in EVAL
            and "grid-template-columns" not in CHUNK
        )

    def test_every_table_class_the_page_uses_has_a_rule(self):
        used = set(re.findall(r'class="tr[^"]*\b(t-[a-z]+)', JS + HTML))
        assert len(used) >= 9, used
        missing = [
            name
            for name in sorted(used)
            if not re.search(rf"^\.{name} \{{[^}}]*grid-template-columns", CSS, re.M)
        ]
        assert missing == [], f"table classes with no columns: {missing}"

    def test_a_row_is_never_given_a_class_that_is_not_a_class(self):
        assert "trNone" not in JS and 'class="tr"' not in JS.replace('class="tr ', ""), (
            "a bare .tr has no columns at all"
        )

    def test_the_page_layouts_are_classes_too(self):
        for name in ("lay-overview", "lay-searchbar", "lay-half"):
            assert name in HTML and re.search(rf"^\.{name} \{{", CSS, re.M), name


class TestThreeTiers:
    def test_there_are_three(self):
        widths = sorted({int(w) for w in re.findall(r"@media \(max-width: (\d+)px\)", CSS)})
        assert widths == [700, 900, 1100], widths

    def test_a_tablet_gets_one_column_where_two_do_not_fit(self):
        assert re.search(
            r"\.lay-overview \{ grid-template-columns: minmax\(0, 1fr\); \}", all_blocks(1100)
        )

    def test_a_phone_stacks_rows_with_their_headings(self):
        phone = all_blocks(700)
        assert ".table > .tr.head { display: none; }" in phone
        assert "content: attr(data-label)" in phone, (
            "a stacked value needs its column heading beside it"
        )
        assert re.search(
            r"\.table > \.tr:not\([^{]*\{ grid-template-columns: minmax\(0, 1fr\)", phone
        )
        assert (
            "overflow-wrap: anywhere" in phone
            and ".grid.metrics { grid-template-columns: minmax(0, 1fr);" in phone
        )
        for layout in (".lay-half", ".lay-searchbar"):
            assert layout in phone


class TestNoTileIsLeftAlone:
    """Five tiles in a grid of four left the fifth stretched across a row of
    its own, and four in a grid of three left the fourth alone under them."""

    def test_a_row_has_as_many_columns_as_tiles(self):
        assert (
            ".grid.metrics { grid-template-columns: repeat(var(--tiles, 4), minmax(0, 1fr)); }"
            in CSS
        )
        for n in (2, 3, 5):
            assert f".grid.metrics:has(> :nth-child({n}):last-child) {{ --tiles: {n}; }}" in CSS
        assert not re.search(r"metrics[^{]*\{[^}]*auto-fit", CSS), (
            "a fitted grid strands tiles at in-between widths"
        )
        assert "#ov-metrics > :last-child" not in CSS, "no tile is stretched across a row"

    def test_on_a_phone_the_tiles_are_rows_of_one_card(self):
        phone = all_blocks(700)
        assert 'grid-template-areas: "l v" "s v"' in phone, (
            "the name and its note on the left, the number on the right"
        )
        assert ".grid.metrics > .metric + .metric { border-top:" in phone

    def test_every_row_of_tiles_fits_the_rule(self):
        inline = re.findall(
            r'class="grid metrics"[^>]*>(.*?)</div>\s*(?:<div class="grid|\$\{refusal|<div class="card"|`)',
            JS,
            re.S,
        )
        listed = re.findall(r"\$\('(?:ov|s)-metrics'\)\.innerHTML = \[(.*?)\]\.join", JS, re.S)
        counts = [row.count("metric('") for row in inline + listed]
        assert len(counts) == 7, counts
        assert all(2 <= n <= 5 for n in counts), counts

    def test_what_needs_attention_is_counted_on_its_card_not_in_a_tile(self):
        assert "metric('Needs attention'" not in JS
        assert 'id="ov-attn-count"' in HTML and "$('ov-attn-count').innerHTML" in JS

    def test_a_tile_with_nothing_yet_says_so_in_words(self):
        assert "none ? 'None yet'" in JS and "if (!state.timings.length) return null;" in JS
        assert "metric('Last update', newest ? ago(newest.at) : null" in JS
        assert re.search(r"^\.metric \.v\.none \{", CSS, re.M)
        assert ".grid.metrics:empty { display: none; }" in CSS, (
            "an empty row on a phone would draw as a line"
        )


class TestSmallThings:
    def test_the_collection_filters_are_one_line_that_scrolls(self):
        assert 'class="toolbar"' in HTML and 'class="chips"' in HTML
        assert re.search(r"^\.chips \{[^}]*overflow-x: auto; scrollbar-width: none;", CSS, re.M)
        assert ".toolbar .chips { grid-column: 1 / -1; grid-row: 2; }" in block(
            1100
        ) or ".toolbar .chips { grid-column: 1 / -1; grid-row: 2; }" in all_blocks(1100)

    def test_the_tabs_scroll_without_a_bar(self):
        assert (
            re.search(r"^\.tabs \{[^}]*scrollbar-width: none;", CSS, re.M)
            and ".tabs::-webkit-scrollbar { display: none; }" in CSS
        )

    def test_the_sign_in_box_is_never_wider_than_the_screen(self):
        # The code input is twenty characters wide by default, which pushed the
        # box past the edge of a phone.
        assert re.search(r"^\.gate \{[^}]*grid-template-columns: minmax\(0, 1fr\);", CSS, re.M)
        assert ".gate input, .dialog input { min-width: 0; }" in CSS

    def test_between_phone_and_desktop_a_person_is_two_lines(self):
        tablet = all_blocks(1100)
        assert 'grid-template-areas: "who who who act" "role docs ways ways"' in tablet
        assert (
            ".tr.t-people > :nth-child(3)::before, .tr.t-people > :nth-child(4)::before { content: attr(data-label);"
            in tablet
        )

    def test_cards_drawn_into_one_holder_keep_the_page_spacing(self):
        assert (
            "#access-body, #audit-body { display: flex; flex-direction: column; gap: 18px; }" in CSS
        )

    def test_the_search_way_tags_are_not_sign_in_rows(self):
        # The rows of "How you sign in" were called .way, and so were the tags
        # that say what a collection can search, which grew into tall boxes.
        assert not re.search(r"^\.way[ {.]", CSS, re.M) and ".ways .way {" in CSS

    def test_the_headings_are_put_on_the_cells_by_the_page(self):
        assert "function labelTables(" in JS and "setAttribute('data-label'" in JS
        assert "MutationObserver" in JS and "watchTables();" in JS, (
            "or a table rendered later has no labels"
        )

    def test_nothing_is_forced_onto_one_line_on_a_phone(self):
        phone = all_blocks(700)
        assert ".btn { white-space: normal;" in phone
        assert re.search(r"^\.card > \.head \{[^}]*flex-wrap: wrap", CSS, re.M), (
            "a header with two buttons has to be able to wrap"
        )
        assert re.search(r"^\.pill \{[^}]*max-width: 100%", CSS, re.M)

    def test_fingers_get_forty_pixels(self):
        touch = all_blocks(900)
        rule = re.search(r"([^{}]*)\{ min-height: 40px; \}", touch)
        assert rule, "no touch target rule"
        for selector in (".btn", ".chip", ".tabs button", "select"):
            assert selector in rule.group(1), selector
        assert ".btn.sm { min-height: 40px;" in touch

    def test_the_keyboard_can_see_where_it_is(self):
        assert re.search(r":focus-visible \{ outline: 2px solid var\(--acc\)", CSS)

    def test_motion_is_optional(self):
        assert "prefers-reduced-motion: reduce" in CSS


class TestACollectionsTiles:
    def test_four_tiles_and_none_repeats_a_tag_from_the_header(self):
        panel = JS[JS.index("${metric('Chunks', fmtNum(h.count)") :]
        panel = panel[: panel.index("</div>")]
        labels = re.findall(r"metric\('([^']+)'", panel)
        assert labels == ["Chunks", "Index", "Kept in", "On disk", "Last write"], (
            "four are drawn: where it is kept, or its size on this disk"
        )
        assert "${c.shared_store ? metric('Kept in'" in panel and ": metric('On disk'" in panel

    def test_a_date_is_short_enough_to_stay_on_one_line(self):
        assert "toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })" in JS


class TestSettingsAreInTheAccountMenu:
    """Settings were a page, then a gear in the top bar. They are in the account menu now, under who is signed in."""

    def test_the_account_button_at_the_top_right_opens_it(self):
        assert (
            'id="btn-account"' in HTML
            and 'aria-haspopup="true"' in HTML
            and 'aria-controls="account-menu"' in HTML
        )
        assert 'id="account-menu" hidden' in HTML, "closed until asked for"
        assert 'id="btn-settings"' not in HTML and 'data-icon="settings"' not in HTML, (
            "no gear in the top bar"
        )
        assert (
            "page-settings" not in HTML
            and 'href="#/settings"' not in HTML
            and "go('#/settings')" not in JS
        )

    def test_it_holds_the_theme_the_key_and_the_api_reference(self):
        menu = HTML[HTML.index('id="account-menu"') :]
        menu = menu[: menu.index("</header>")]
        light, dark = '[["setTheme","light"]]', '[["setTheme","dark"]]'
        assert dark in menu and light in menu and 'id="btn-key"' in menu and 'href="/docs"' in menu
        assert menu.index(light) < menu.index(dark), "light first: it is the default"
        for item in ('id="mi-access"', 'id="mi-keys"', 'id="mi-ways"', 'id="mi-signout"'):
            assert item in menu, item

    def test_it_closes_on_escape_a_click_elsewhere_and_a_change_of_page(self):
        boot = JS[JS.index("$('btn-account').onclick") :][:700]
        assert (
            "closest('.menu-wrap')" in boot
            and "e.key === 'Escape'" in boot
            and "'hashchange', () => closeAccount()" in boot
        )
        assert "setAttribute('aria-expanded'" in JS

    def test_it_does_not_repeat_the_overview_or_the_version(self):
        menu = JS[JS.index("async function openAccount()") : JS.index("function closeAccount")]
        for repeated in ("collections_count", "total_vectors", "total_size_bytes", "'Version'"):
            assert repeated not in menu


class TestSettingsHasNoModelsPanel:
    def test_it_is_gone_from_the_page_the_script_and_the_stylesheet(self):
        for source in (HTML, JS, CSS):
            assert (
                "set-models" not in source and "t-models" not in source and "tr-note" not in source
            )
        assert "Models on this machine" not in HTML

    def test_a_missing_model_is_still_flagged_where_it_matters(self):
        assert "loadModels()" in JS and "'Model missing'" in JS


class TestTheSidebarFolds:
    def test_the_fold_is_in_the_sidebars_own_header(self):
        header = next(line for line in HTML.splitlines() if '<div class="brand">' in line)
        assert 'id="side-toggle"' in header and 'aria-controls="side"' in header
        assert (
            header.index('class="mark"')
            < header.index('class="word"')
            < header.index('id="side-toggle"')
        )
        toggle = JS[JS.index("$('side-toggle').onclick") :][:300]
        assert "app.classList.toggle('collapsed')" in toggle

    def test_folded_the_rail_keeps_the_logo_and_the_control_and_nothing_else_up_there(self):
        assert ".app.collapsed .side .brand { flex-direction: column;" in CSS
        assert ".app.collapsed .side .brand .word { display: none; }" in CSS
        assert ".brand > div:last-child" not in CSS, (
            "it matched the header's last element, which stopped being the name when a button was added"
        )

    def test_the_top_bars_button_is_for_a_narrow_screen_only(self):
        assert 'id="hamburger"' in HTML and 'data-icon="menu"' in HTML
        assert ".top .hamburger { display: none; }" in CSS, (
            "more specific than .btn, so no later rule can show it again"
        )
        narrow = all_blocks(900)
        assert (
            ".top .hamburger { display: inline-flex; }" in narrow
            and ".side .brand .side-toggle { display: none; }" in narrow
        )

    def test_it_folds_to_a_rail_on_a_desktop_and_remembers(self):
        assert re.search(
            r"@media \(min-width: 901px\) \{\s*\.app\.collapsed \.side \{ width: var\(--rail\)", CSS
        )
        assert (
            "localStorage.setItem('vectrixdb.side'" in JS
            and "localStorage.getItem('vectrixdb.side')" in JS
        )
        assert "$('side-toggle').setAttribute('aria-expanded'" in JS

    def test_on_a_narrow_screen_the_menu_button_opens_the_drawer(self):
        handler = JS[JS.index("$('hamburger').onclick") :][:200]
        assert "const drawer = window.matchMedia('(max-width: 900px)')" in JS
        assert "$('side').classList.toggle('open')" in handler

    def test_an_open_drawer_can_be_closed(self):
        # it covered the button that opened it, and there was no other way out
        assert 'id="side-close"' in HTML and 'id="scrim"' in HTML
        assert (
            "$('side-close').onclick = closeDrawer" in JS
            and "$('scrim').onclick = closeDrawer" in JS
        )
        assert "e.key === 'Escape' && $('side').classList.contains('open')" in JS
        assert ".side.open ~ .scrim { display: block" in CSS
        drawer, scrim = (
            re.search(r"\.side \{ position: fixed;[^}]*z-index: (\d+)", CSS),
            re.search(r"\.scrim \{[^}]*z-index: (\d+)", CSS),
        )
        assert int(scrim.group(1)) < int(drawer.group(1)), (
            "the scrim sits under the drawer and over the page"
        )

    def test_a_rail_still_says_what_each_icon_is(self):
        links = re.findall(r'<a href="#/[a-z]+" data-page="[a-z]+"[^>]*>', HTML)
        assert len(links) == 9 and all(" title=" in link for link in links)


class TestTheIcons:
    def test_every_icon_asked_for_is_drawn(self):
        table = JS[JS.index("const paths = {") : JS.index("return `<svg")]
        drawn = set(re.findall(r"^\s+'?([a-z-]+)'?: '<", table, re.M))
        asked = set(re.findall(r"icon\('([a-z-]+)'", JS + EVAL + CHUNK)) | set(
            re.findall(r'data-icon="([a-z-]+)"', HTML + JS + EVAL + CHUNK)
        )
        assert asked <= drawn, f"asked for and not drawn: {sorted(asked - drawn)}"

    def test_they_are_one_family(self):
        assert (
            'viewBox="0 0 24 24"' in JS
            and 'stroke-width="1.75"' in JS
            and 'aria-hidden="true"' in JS
        )


class TestThePalette:
    TOKENS = (
        "--g0",
        "--g1",
        "--g2",
        "--g3",
        "--t1",
        "--t2",
        "--t3",
        "--line",
        "--acc",
        "--acc-fill",
        "--acc-ink",
        "--ok",
        "--warn",
        "--bad",
        "--info",
    )

    @pytest.mark.parametrize("opener", [":root {", ':root[data-theme="light"] {'])
    def test_both_themes_define_every_colour(self, opener):
        block = CSS[CSS.index(opener) :]
        block = block[: block.index("}")]
        for token in self.TOKENS:
            assert f"{token}:" in block, f"{token} is missing from {opener}"

    def test_gold_is_a_fill_in_both_and_dark_enough_to_read_as_text_on_paper(self):
        light = CSS[CSS.index(':root[data-theme="light"] {') :]
        light = light[: light.index("}")]
        assert "--acc: #8a6203" in light and "--acc-fill: #e8a81a" in light
        assert re.search(r"\.btn\.primary \{ background: var\(--acc-fill\)", CSS)


class TestABuildIdIsAnId:
    def test_it_is_shown_short_whole_on_hover_and_copied_on_a_click(self):
        chip = JS[JS.index("const idChip") :][:600]
        assert "slice(0, 8)" in chip and "title=" in chip and "['copyText', full]" in chip
        assert "replace(/^[a-z]+_/, '')" in chip, (
            "the build_ prefix says nothing the column heading has not"
        )

    def test_it_is_not_on_the_pages_where_nobody_needs_it(self):
        assert "'build ' +" not in JS, "collection cards, the collection header and search results"
        assert "metric('Build'" not in JS, "a headline tile is for a number somebody acts on"
        assert "<span>Id</span><span>Quality</span><span>Source</span><span></span>" in JS, (
            "where a chunk came from, not which build stored it"
        )

    def test_it_is_where_a_build_is_the_subject(self):
        for place in (
            "idChip(b.build_id)",
            "idChip(h.index_build_id)",
            "idChip(prov.build_id)",
            "idChip(d.build_id)",
            "idChip(r.index_build_id)",
        ):
            assert place in JS, place


class TestTheBrowserIsToldToAsk:
    fastapi = pytest.importorskip("fastapi", reason="the API extra is not installed")

    @pytest.mark.parametrize(
        "path", ["/dashboard/", "/dashboard/index.html", "/dashboard/app.js", "/dashboard/app.css"]
    )
    def test_every_dashboard_file_is_revalidated(self, tmp_path, path):
        from fastapi.testclient import TestClient

        from vectrixdb.api.server import create_app

        with TestClient(create_app(db_path=str(tmp_path / "db"), enable_dashboard=True)) as client:
            first = client.get(path)
            assert first.status_code == 200 and first.headers["cache-control"] == "no-cache"
            again = client.get(path, headers={"If-None-Match": first.headers["etag"]})
            assert again.status_code == 304, "asking is cheap: the unchanged file is not sent again"


class TestTheGuideIsOneFrameAPiece:
    """The Learn page's guide came over from an older page with inline styles
    naming colour variables that no longer existed, so its icons drew as empty
    squares, and each block of code showed its markup's blank lines."""

    def test_every_colour_the_page_names_exists(self):
        defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", CSS)) | {"--tiles"}
        for name, source in (
            ("app.css", CSS),
            ("app.js", JS),
            ("evaluate.js", EVAL),
            ("chunking.js", CHUNK),
            ("index.html", HTML),
        ):
            used = set(re.findall(r"var\((--[a-z0-9-]+)", source))
            assert used <= defined, f"{name} names {sorted(used - defined)}, which nothing defines"

    def test_the_guide_has_no_inline_styles(self):
        guide = HTML[
            HTML.index('id="learn-guide"') : HTML.index(
                "</section>", HTML.index('id="learn-guide"')
            )
        ]
        assert 'style="' not in guide
        assert (
            guide.count('class="guide-sec"') == 9 and '"gotoGuide",{"$":"this"},"code-cli"' in guide
        ), "every section, the command line too, is on the rail"

    def test_only_the_code_keeps_its_line_breaks(self):
        block = re.search(r"^\.code-block \{[^}]*\}", CSS, re.M).group(0)
        assert "white-space" not in block, "the markup's blank lines around the code would show"
        assert re.search(r"^\.code-block pre \{[^}]*white-space: pre;", CSS, re.M)
        assert "const pre = el.querySelector('pre'); copyText((pre || el).innerText);" in JS, (
            "copying takes the code, not the word Copy"
        )


class TestTheTutorialsInTwoColumns:
    """Five tutorials, on a server with collections of its own, left the last alone on half a row at tablet width."""

    def test_an_odd_one_out_takes_the_whole_row_and_an_even_count_is_left_as_it_is(self):
        rule = CSS[CSS.index("@media (max-width: 1100px) and (min-width: 701px) {\n  .tut-grid") :]
        rule = rule[: rule.index("\n}\n")]
        last_odd = (
            ".tut-grid > .tut:nth-last-child(1 of :not([hidden])):nth-child(odd of :not([hidden]))"
        )
        assert f"{last_odd} {{ grid-column: 1 / -1; }}" in rule, (
            "the last card shown, when it is an odd one, spans the row"
        )
        assert f"{last_odd} p {{ max-width: 65ch; }}" in rule, (
            "and its words stay a width that reads"
        )

    def test_it_is_only_where_there_are_two_columns(self):
        assert (
            "@media (max-width: 1100px) { .tut-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }"
            in CSS
        )
        assert (
            ".tut-grid { grid-template-columns: minmax(0, 1fr); }"
            in CSS[CSS.index("@media (max-width: 700px) {\n  .tut-grid") :]
        ), "one column below 701, where nothing is left alone"
