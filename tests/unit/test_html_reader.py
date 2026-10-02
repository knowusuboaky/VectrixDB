"""A web page is read as its content, not as the site around it.

The first live read of a bank's About page came back as 12,774 characters on
3,189 lines, 2,841 of them blank: two copies of the main menu, a utility bar,
a site index in a hidden dialog, a footer and a cookie notice, with the page's
own text in the middle. These pages are made up, in the shapes real sites use.
"""

from __future__ import annotations

from vectrixdb.ingest import _decode_html, _load_html, chunk, load


def read(markup: str) -> str:
    return _load_html(markup).text


def lines_of(text: str) -> list:
    return text.splitlines()


# A site: a banner with the menu twice, for the desktop and the phone, a cookie
# notice, a site index in a dialog hidden until opened, a footer, and between
# them a <main> that is the page.
SITE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>About the Harbour Bakery</title>
  <script>window.dataLayer = [];</script>
  <style>.menu { color: red }</style>
</head>
<body class="modal-open">
  <a class="skip-link" href="#content">Skip to main content</a>
  <div class="cookie-banner">We use cookies. <button>Accept</button></div>
  <header class="site-header">
    <nav aria-label="main menu">
      <ul>
        <li>
          <a href="/bread">
            Bread
          </a>
        </li>
        <li><a href="/cakes">Cakes</a></li>
        <li><a href="/about">About us</a></li>
      </ul>
    </nav>
    <nav aria-label="phone menu">
      <ul><li><a href="/bread">Bread</a></li><li><a href="/cakes">Cakes</a></li><li><a href="/about">About us</a></li></ul>
    </nav>
  </header>
  <main id="content">
    <h1>About the Harbour Bakery</h1>
    <p>
      We have baked   bread in Halifax
      since 1952.
    </p>
    <h2>What we make</h2>
    <ul>
      <li>
        <div class="card">
          <a href="/sourdough">Sourdough</a>
        </div>
        <div class="card-text">
          Fermented for two days, baked every morning.
        </div>
      </li>
      <li>
        <div class="card"><a href="/rye">Rye</a></div>
        <div class="card-text">Dark, dense and sliced thin.</div>
      </li>
    </ul>
    <img src="spacer.gif" alt="">
    <img src="oven.jpg" alt="The stone oven, lit">
  </main>
  <div class="cmp-dialog hidden cmp-sitemap-modal">
    <h2>Site Index</h2>
    <ul><li><a href="/a">Accounts</a></li><li><a href="/b">Branches</a></li><li><a href="/c">Careers</a></li></ul>
  </div>
  <footer>
    <p>123 Water Street, Halifax</p>
    <ul><li><a href="/privacy">Privacy</a></li><li><a href="/legal">Legal</a></li><li><a href="/careers">Careers</a></li></ul>
  </footer>
</body>
</html>"""


class TestAPageIsReadAsItsContent:
    def test_only_what_is_inside_main_is_read(self):
        text = read(SITE)
        assert text.startswith("# About the Harbour Bakery")
        assert "We have baked bread in Halifax since 1952." in text
        for around in ("Bread\n", "Cakes", "About us", "Skip to main content", "cookies", "Accept", "Site Index", "Accounts", "Water Street", "Privacy"):
            assert around not in text, around

    def test_the_title_is_still_the_pages_title(self):
        assert _load_html(SITE).metadata["title"] == "About the Harbour Bakery"

    def test_no_line_is_only_space_and_no_item_is_empty(self):
        found = lines_of(read(SITE))
        assert all(line == line.rstrip() for line in found)
        assert not [line for line in found if line.strip() in ("-", "")] or "\n\n\n" not in read(SITE)
        assert not [line for line in found if line.strip() == "-"], "a marker with nothing after it"
        assert "\n\n\n" not in read(SITE)

    def test_an_item_keeps_its_title_and_its_words_together(self):
        text = read(SITE)
        assert "- Sourdough\n  Fermented for two days, baked every morning.\n- Rye\n  Dark, dense and sliced thin." in text

    def test_a_picture_the_page_calls_decoration_is_left_out(self):
        text = read(SITE)
        assert "[Figure: The stone oven, lit]" in text
        assert "spacer" not in text

    def test_the_real_page_shape_is_a_fraction_of_the_markup(self):
        """The menus twice, a site index and a footer: the page is what is left."""
        assert len(read(SITE)) < 400


class TestTheLandmarkIsFoundHoweverItIsWritten:
    def test_role_main_is_main(self):
        page = "<div class='bar'><p>Top bar</p></div><div role='main'><h1>Rates</h1><p>Prime is 5.45%.</p></div><div><p>Tail</p></div>"
        text = read(page)
        assert "Prime is 5.45%." in text and "Top bar" not in text and "Tail" not in text

    def test_one_article_is_the_page(self):
        page = "<div><p>Latest posts</p></div><article><h1>Proving dough</h1><p>Warm and slow.</p></article><div><p>More posts</p></div>"
        text = read(page)
        assert "Warm and slow." in text and "Latest posts" not in text and "More posts" not in text

    def test_many_articles_are_a_listing_and_the_whole_page_is_read(self):
        page = "<article><h2>One</h2><p>First post.</p></article><article><h2>Two</h2><p>Second post.</p></article>"
        text = read(page)
        assert "First post." in text and "Second post." in text

    def test_a_main_a_script_fills_in_is_a_shell_and_the_page_is_read(self):
        words = "Our fixed rate is 4.2% for five years, and it is the same for every customer. " * 6
        page = f"<header><p>Brand</p></header><main id='app'></main><div id='content'><h1>Rates</h1><p>{words}</p></div>"
        text = read(page)
        assert "Our fixed rate is 4.2%" in text and "Brand" not in text

    def test_a_custom_element_that_starts_with_main_is_not_main(self):
        page = "<main-nav><p>Menu words</p></main-nav><div><p>The words.</p></div>"
        assert "The words." in read(page)


class TestWhatIsAroundTheContentIsLeftOutEverywhere:
    """No <main>: the whole page is read, and what marks itself as around the content is still left out."""

    PAGE = """<body>
      <header><p>Site banner</p></header>
      <div role="navigation"><p>Role nav</p></div>
      <div role="banner"><p>Role banner</p></div>
      <div class="breadcrumb"><p>Home / About</p></div>
      <div id="gdpr-consent"><p>Consent words</p></div>
      <dialog><p>Dialog words</p></dialog>
      <div hidden><p>Hidden words</p></div>
      <div hidden="until-found"><p>Found by search</p></div>
      <div aria-hidden="true"><p>Aria hidden</p></div>
      <div style="display: none"><p>Display none</p></div>
      <div class="hidden md:block"><p>Shown on a desktop</p></div>
      <div class="d-none d-md-block"><p>Shown on a tablet</p></div>
      <p>Read the report <span class="sr-only">(opens in a new window)</span>today.</p>
      <article>
        <header><h1>The article's own header</h1></header>
        <p>The article's words.</p>
        <aside><p>A note in the article.</p></aside>
      </article>
      <article><p>A second article, so neither is the page.</p></article>
      <aside><p>A sidebar.</p></aside>
      <form><select><option>Canada</option></select><textarea>typed</textarea><button>Send</button></form>
      <svg><title>Close icon</title></svg>
      <footer><p>Site footer</p></footer>
    </body>"""

    def test_each_kind_is_left_out(self):
        text = read(self.PAGE)
        for around in ("Site banner", "Role nav", "Role banner", "Home / About", "Consent words", "Dialog words",
                       "Hidden words", "Aria hidden", "Display none", "opens in a new window", "A sidebar.",
                       "Canada", "typed", "Send", "Close icon", "Site footer"):
            assert around not in text, around

    def test_what_is_only_hidden_at_some_widths_is_kept(self):
        text = read(self.PAGE)
        assert "Shown on a desktop" in text and "Shown on a tablet" in text and "Found by search" in text

    def test_a_header_and_an_aside_inside_an_article_are_the_article(self):
        text = read(self.PAGE)
        assert "# The article's own header" in text and "A note in the article." in text

    def test_words_either_side_of_left_out_text_still_meet(self):
        assert "Read the report today." in read(self.PAGE)

    def test_a_page_wrapped_in_one_form_is_still_read(self):
        """ASP.NET Web Forms put the whole body in a <form>."""
        page = "<body><form id='aspnetForm'><div><h1>Rates</h1><p>Prime is 5.45%.</p><input type='hidden' value='x'><button>Go</button></div></form></body>"
        text = read(page)
        assert "Prime is 5.45%." in text and "Go" not in text

    def test_the_body_is_never_left_out_by_its_class(self):
        assert "Kept." in read("<body class='modal-open cookie-consent-shown'><p>Kept.</p></body>")


class TestAMenuIsAMenuWhateverItsMarkup:
    MENU = "<ul class='x1'><li><a href='/a'>Accounts</a></li><li><a href='/b'>Branches</a></li><li><a href='/c'>Careers</a></li></ul>"

    def test_a_list_of_only_links_outside_the_content_is_left_out(self):
        text = read(f"<div>{self.MENU}</div><h1>Rates</h1><p>Prime is 5.45%.</p>")
        assert "Accounts" not in text and "Prime is 5.45%." in text

    def test_a_list_with_words_around_its_links_is_content(self):
        page = (
            "<ul><li>Read the <a href='/r'>annual report</a> before you invest.</li>"
            "<li>See the <a href='/f'>fee schedule</a> for every account.</li>"
            "<li>Ask a <a href='/b'>branch</a> about a mortgage.</li></ul>"
        )
        assert "- Read the annual report before you invest." in read(page)

    def test_a_list_of_links_inside_the_content_is_kept(self):
        assert "- Accounts" in read(f"<main><h1>Where to go</h1>{self.MENU}</main>")
        assert "- Accounts" in read(f"<article><h1>Where to go</h1>{self.MENU}</article>")

    def test_two_links_are_not_a_menu(self):
        page = "<ul><li><a href='/a'>Accounts</a></li><li><a href='/b'>Branches</a></li></ul>"
        assert "- Accounts" in read(page)


class TestTheShapeOfTheText:
    def test_nested_lists_are_indented_under_their_item(self):
        page = "<ul><li>Bread<ul><li>Rye</li><li>Spelt</li></ul></li><li>Cakes</li></ul>"
        assert "- Bread\n  - Rye\n  - Spelt\n- Cakes" in read(page)

    def test_a_line_break_is_a_line_not_a_paragraph(self):
        assert "66 Wellington Street West\nToronto, ON" in read("<p>66 Wellington Street West<br>Toronto, ON</p>")

    def test_code_keeps_its_indentation_in_a_fence(self):
        page = "<p>Call it so:</p><pre><code>def rate(years):\n    return 4.2 if years == 5 else 4.9\n</code></pre><p>Done.</p>"
        text = read(page)
        assert "```\ndef rate(years):\n    return 4.2 if years == 5 else 4.9\n```" in text
        assert text.index("Call it so:") < text.index("```") < text.index("Done.")

    def test_code_that_has_a_fence_in_it_gets_a_longer_one(self):
        assert "````\nuse ``` to fence\n````" in read("<pre>use ``` to fence</pre>")

    def test_a_heading_that_begins_an_item_leaves_its_words_as_paragraphs(self):
        page = "<ul><li><h3>Working together</h3><div><p>Our approach to ESG.</p></div></li></ul>"
        text = read(page)
        assert "### Working together\n\nOur approach to ESG." in text
        assert "  Our approach" not in text

    def test_a_picture_that_is_only_a_counter_or_inline_bytes_is_left_out(self):
        page = (
            "<img src='pixel.gif' width='1' height='1'>"
            "<img src='data:image/png;base64,AAAABBBB'>"
            "<img src='chart.png' alt='Sales by month'>"
        )
        text = read(page)
        assert "[Figure: Sales by month]" in text and "pixel" not in text and "base64" not in text and "AAAA" not in text

    def test_markup_a_browser_would_forgive_does_not_lose_words(self):
        text = read("<div><p>One</div></span><p>Two<li>Three<ul><li>Four</div><p>Five")
        for word in ("One", "Two", "Three", "Four", "Five"):
            assert word in text, word

    def test_a_list_left_open_is_closed_with_its_parent(self):
        text = read("<div><ul><li>First<li>Second</div><p>After the list.</p>")
        assert "- First\n- Second" in text and "After the list." in text

    def test_an_old_page_of_unclosed_paragraphs_and_cells_reads_whole(self):
        """<p>one<p>two, <li>a<li>b, <td>1<td>2: closed as a browser closes them, so the stack stays shallow."""
        from vectrixdb.ingest import _HTMLText

        parser = _HTMLText()
        parser.feed("<body>" + "<p>Paragraph" * 5000 + "<ul>" + "<li>Item" * 500 + "</ul>")
        parser.close()
        assert len(parser._open) <= 3, "each paragraph closes the one before, as it does in a browser"
        text = "".join(parser.parts)
        assert text.count("Paragraph") == 5000 and text.count("- Item") == 500

    def test_an_unclosed_cell_keeps_its_words(self):
        text = read("<table><tr><th>Term<th>Rate<tr><td>1 year<td>4.9%<tr><td>5 years<td>4.2%</table>")
        assert "Term: 1 year; Rate: 4.9%" in text and "Term: 5 years; Rate: 4.2%" in text

    def test_the_sections_still_chunk_by_their_headings(self):
        doc = _load_html(SITE)
        found = [c.heading for c in chunk(doc, "markdown", size=500)]
        assert list(dict.fromkeys(found)) == ["About the Harbour Bakery", "What we make"], "the oven's figure is a chunk of its own, under its section" 


class TestAPageIsReadInItsOwnEncoding:
    def test_the_charset_the_page_names(self):
        page = "<meta charset='windows-1252'><p>Café crème ’</p>".encode("cp1252")
        assert "Café crème ’" in _decode_html(page)

    def test_latin_1_is_read_as_windows_1252_as_browsers_do(self):
        page = b"<meta http-equiv='Content-Type' content='text/html; charset=ISO-8859-1'><p>It\x92s</p>"
        assert "It’s" in _decode_html(page)

    def test_a_page_that_names_nothing_is_utf_8_and_else_windows_1252(self):
        assert "Café" in _decode_html("<p>Café</p>".encode("utf-8"))
        assert "Café" in _decode_html("<p>Café</p>".encode("cp1252"))

    def test_a_byte_order_mark_wins(self):
        assert _decode_html(b"\xef\xbb\xbf<p>x</p>") == "<p>x</p>"
        assert "Café" in _decode_html("<p>Café</p>".encode("utf-16"))

    def test_utf_16_in_a_meta_is_not_utf_16(self):
        page = "<meta charset='utf-16'><p>Café</p>".encode("utf-8")
        assert "Café" in _decode_html(page)

    def test_a_charset_nobody_knows_is_ignored(self):
        assert "Café" in _decode_html("<meta charset='x-made-up'><p>Café</p>".encode("utf-8"))

    def test_a_file_on_disk_is_read_the_same_way(self, tmp_path):
        (tmp_path / "page.html").write_bytes("<meta charset='windows-1252'><h1>Menu</h1><p>Crème brûlée</p>".encode("cp1252"))
        assert "Crème brûlée" in load(tmp_path / "page.html").text
