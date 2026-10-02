"""Word 97-2003 .doc, read in pure Python.

A .doc cannot be written without Word, so these build the two streams one
holds, byte for byte to the format: a File Information Block that says where
the piece table is and how long the body is, and a table stream holding that
piece table. That exercises the real parser rather than a stand-in for it.
"""

from __future__ import annotations

import struct

import pytest

from vectrixdb.exceptions import DependencyError, ExtractionError
from vectrixdb.ingest import _clean_doc, _doc_text, load, load_bytes

TEXT_AT = 0x400


def fib(ccp_text: int, fc_clx: int, lcb_clx: int, flags: int = 0x0200) -> bytearray:
    """A Word 97 File Information Block, to the offsets the format fixes."""
    word = bytearray(TEXT_AT)
    struct.pack_into("<H", word, 0x00, 0xA5EC)  # wIdent
    struct.pack_into("<H", word, 0x0A, flags)  # fWhichTblStm is 0x0200, fEncrypted 0x0100
    struct.pack_into("<H", word, 0x20, 14)  # csw
    struct.pack_into("<H", word, 0x3E, 22)  # cslw
    struct.pack_into("<i", word, 0x4C, ccp_text)  # ccpText
    struct.pack_into("<H", word, 0x98, 93)  # cbRgFcLcb
    struct.pack_into("<II", word, 0x1A2, fc_clx, lcb_clx)  # fcClx, lcbClx
    return word


def document(pieces, *, ccp_text=None, flags=0x0200, formatting=b""):
    """The WordDocument and table streams for a list of (text, compressed) pieces."""
    text_bytes = bytearray()
    cps, pcds, cp = [0], [], 0
    for text, compressed in pieces:
        at = TEXT_AT + len(text_bytes)
        if compressed:
            text_bytes += text.encode("cp1252")
            fc = (at * 2) | 0x40000000
        else:
            text_bytes += text.encode("utf-16-le")
            fc = at
        cp += len(text)
        cps.append(cp)
        pcds.append(struct.pack("<HIH", 0, fc, 0))
    plc = struct.pack(f"<{len(cps)}i", *cps) + b"".join(pcds)
    clx = formatting + b"\x02" + struct.pack("<I", len(plc)) + plc
    word = fib(cp if ccp_text is None else ccp_text, 0, len(clx), flags) + text_bytes
    return bytes(word), clx


class TestThePieceTable:
    def test_a_unicode_piece(self):
        word, table = document([("Late fees are charged monthly.\r", False)])
        assert _doc_text(word, table) == "Late fees are charged monthly."

    def test_an_eight_bit_piece_is_code_page_1252(self):
        """Most .doc text is stored this way, and 0x80 is the euro sign there, not in Latin-1."""
        word, table = document([("Caf\xe9 costs €4.\r", True)])
        assert _doc_text(word, table) == "Café costs €4."

    def test_pieces_are_joined_in_the_order_the_table_gives(self):
        word, table = document([("Revenue grew ", True), ("in every region.\r", False)])
        assert _doc_text(word, table) == "Revenue grew in every region."

    def test_formatting_runs_before_the_table_are_skipped(self):
        """A table stream can open with property runs; the pieces come after them."""
        runs = b"\x01" + struct.pack("<h", 3) + b"xyz" + b"\x01" + struct.pack("<h", 1) + b"q"
        word, table = document([("Still read.\r", False)], formatting=runs)
        assert _doc_text(word, table) == "Still read."

    def test_only_the_body_is_kept(self):
        """Headers and footers follow the body in the same stream; a running header is noise."""
        word, table = document([("The body.\r", False), ("Page header\r", False)], ccp_text=len("The body.\r"))
        assert _doc_text(word, table) == "The body."


class TestWhatWordsControlCharactersBecome:
    def test_paragraphs_line_breaks_and_page_breaks_are_newlines(self):
        assert _clean_doc("one\rtwo\x0bthree\x0cfour") == "one\ntwo\nthree\nfour"

    def test_a_field_shows_its_result_and_hides_its_instruction(self):
        text = 'See \x13 HYPERLINK "https://example.test" \x14the rates page\x15 for more.'
        assert _clean_doc(text) == "See the rates page for more."

    def test_fields_nest(self):
        text = "a \x13 IF \x13 PAGE \x145\x15 > 1 \x14many\x15 b"
        assert _clean_doc(text) == "a many b"

    def test_a_field_with_no_result_leaves_nothing(self):
        assert _clean_doc("before \x13 TOC \\o \x15after") == "before after"

    def test_table_cells_are_tab_separated(self):
        assert _clean_doc("Region\x07Revenue\x07\x07") == "Region\tRevenue"

    def test_hyphens_and_spaces_are_what_a_reader_sees(self):
        assert _clean_doc("co\x1eop re\x1ftire a\xa0b") == "co-op retire a b"

    def test_placeholders_for_pictures_and_notes_are_dropped(self):
        assert _clean_doc("a\x01b\x08c\x02d") == "abcd"

    def test_blank_lines_do_not_pile_up(self):
        assert _clean_doc("one\r\r\r\r\rtwo") == "one\n\ntwo"


class TestWhatItRefuses:
    def test_something_that_is_not_a_word_document(self):
        with pytest.raises(ExtractionError, match="not a Word 97-2003 document"):
            _doc_text(b"%PDF-1.7" + bytes(64), b"")

    def test_a_password_protected_document_says_so(self):
        word, table = document([("secret\r", False)], flags=0x0300)
        with pytest.raises(ExtractionError, match="password-protected"):
            _doc_text(word, table)

    def test_a_header_with_no_piece_table_where_it_points(self):
        word, _ = document([("text\r", False)])
        with pytest.raises(ExtractionError, match="no piece table"):
            _doc_text(word, b"\x00" * 16)

    def test_the_package_it_needs_is_named(self, tmp_path, monkeypatch):
        """Not an ImportError from three frames down."""
        import builtins

        real = builtins.__import__

        def refuse(name, *args, **kwargs):
            if name == "olefile":
                raise ImportError("no olefile here")
            return real(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", refuse)
        path = tmp_path / "old.doc"
        path.write_bytes(b"x")
        with pytest.raises(DependencyError, match="olefile"):
            load(path)


class TestItIsReadLikeEveryOtherFormat:
    """Through load() and load_bytes(), chosen by its extension, like .docx is."""

    @pytest.fixture
    def streams(self, monkeypatch):
        word, table = document([("Quarterly summary\r", False), ("Revenue 1200\r", True)])
        monkeypatch.setattr("vectrixdb.ingest._doc_streams", lambda path: (word, table))

    def test_load_reads_a_doc_by_its_extension(self, tmp_path, streams):
        path = tmp_path / "q3.doc"
        path.write_bytes(b"not read: the streams above stand in for the file")
        doc = load(path)
        assert doc.text == "Quarterly summary\nRevenue 1200"
        assert doc.metadata["kind"] == "doc" and doc.metadata["filename"] == "q3.doc"

    def test_load_bytes_takes_one_from_a_bucket(self, streams):
        doc = load_bytes(b"bytes from a blob", "reports/q3.doc", source="https://a.blob/q3.doc")
        assert doc.text == "Quarterly summary\nRevenue 1200"
        assert doc.metadata["source"] == "https://a.blob/q3.doc"

    def test_the_api_lists_it_among_what_it_reads(self):
        from pathlib import Path

        import vectrixdb

        source = (Path(vectrixdb.__file__).parent / "api" / "documents.py").read_text(encoding="utf-8")
        built_in = source.split("built_in = [")[1].split("]")[0]
        assert '".doc"' in built_in
