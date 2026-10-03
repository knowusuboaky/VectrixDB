"""The text index computes BM25 as the reference implementations do.

The IDF used to be log((N + 1) / (df + 0.5)), which is not BM25's: it never
reaches zero for a term in every document and overweights rare terms in small
collections. The reference here is the Lucene / rank_bm25 form, written out in
full so the comparison does not depend on an optional package. If rank_bm25 is
installed it is compared against as well.
"""

from collections import Counter
from math import log

import pytest

from vectrixdb.core.collection import TextIndex

DOCS = {
    "d1": "the quick brown fox jumps over the lazy dog",
    "d2": "a quick brown dog outpaces a quick fox",
    "d3": "lazy afternoons are for sleeping dogs",
    "d4": "nothing here matches anything else at all",
}


def reference_bm25(tokenised: dict, query_tokens: list, k1=1.2, b=0.75) -> dict:
    n = len(tokenised)
    avg = sum(len(t) for t in tokenised.values()) / n
    df = Counter()
    for tokens in tokenised.values():
        df.update(set(tokens))
    scores = {}
    for doc_id, tokens in tokenised.items():
        tf = Counter(tokens)
        score = 0.0
        for q in set(query_tokens):
            if tf[q] == 0:
                continue
            idf = log(1.0 + (n - df[q] + 0.5) / (df[q] + 0.5))
            score += idf * tf[q] * (k1 + 1) / (tf[q] + k1 * (1 - b + b * len(tokens) / avg))
        if score:
            scores[doc_id] = score
    return scores


@pytest.fixture
def index() -> TextIndex:
    idx = TextIndex(use_stemming=False)
    for doc_id, text in DOCS.items():
        idx.add(doc_id, text)
    return idx


class TestFormula:
    def test_scores_match_the_reference(self, index):
        tokenised = {d: index._tokenize(t) for d, t in DOCS.items()}
        query = "quick fox"
        expected = reference_bm25(tokenised, index._tokenize(query))
        got = dict(index.search(query, limit=10))
        assert set(got) == set(expected)
        for doc_id, score in expected.items():
            assert got[doc_id] == pytest.approx(score, rel=1e-6), doc_id

    def test_a_term_in_every_document_scores_near_zero(self):
        idx = TextIndex(use_stemming=False)
        for i in range(6):
            idx.add(f"d{i}", f"common word plus unique{i}")
        common = dict(idx.search("common", limit=10))
        rare = dict(idx.search("unique3", limit=10))
        assert max(common.values()) < 0.2
        assert rare["d3"] > 1.0

    def test_against_rank_bm25_when_available(self, index):
        rank_bm25 = pytest.importorskip("rank_bm25")
        corpus = [index._tokenize(t) for t in DOCS.values()]
        ref = rank_bm25.BM25Okapi(corpus, k1=1.2, b=0.75, epsilon=0.0)
        got = dict(index.search("lazy dog", limit=10))
        ref_scores = ref.get_scores(index._tokenize("lazy dog"))
        for (doc_id, _), ref_score in zip(DOCS.items(), ref_scores):
            if ref_score > 0:
                assert got[doc_id] == pytest.approx(ref_score, rel=1e-5)


class TestCJK:
    def test_chinese_is_indexed_as_bigrams(self):
        idx = TextIndex()
        idx.add("zh", "北京是中国的首都")
        idx.add("en", "Beijing is the capital of China")
        assert [d for d, _ in idx.search("首都", limit=5)] == ["zh"]
        assert [d for d, _ in idx.search("北京", limit=5)] == ["zh"]

    def test_japanese_and_korean(self):
        idx = TextIndex()
        idx.add("ja", "東京は日本の首都です")
        idx.add("ko", "서울은 한국의 수도입니다")
        assert [d for d, _ in idx.search("日本", limit=5)] == ["ja"]
        assert [d for d, _ in idx.search("한국", limit=5)] == ["ko"]

    def test_mixed_script_query(self):
        idx = TextIndex()
        idx.add("m", "the 東京 office opens monday")
        idx.add("o", "the office opens tuesday")
        assert idx.search("東京 office", limit=5)[0][0] == "m"

    def test_single_cjk_character_is_kept(self):
        idx = TextIndex()
        assert idx._tokenize("水") == ["水"]


class TestEveryScript:
    """The word pattern was [a-z0-9]+. It kept "fung" out of "Prüfung",
    indexed "prêt" as "pr" and "t", dropped Thai entirely, and stemmed German
    with an English stemmer because German happens to be ASCII. So every
    language with an accent had a sparse index full of fragments, and hybrid
    search on it was dense search with noise added."""

    def test_accented_letters_stay_in_their_words(self):
        idx = TextIndex(language="de")
        idx.add("de", "Kreditvertrag für die Überprüfung")
        assert "überprüfung" in idx._tokenize("Kreditvertrag für die Überprüfung")
        assert [d for d, _ in idx.search("Überprüfung", limit=5)] == ["de"]

    def test_french_is_not_stemmed_with_an_english_stemmer(self):
        english = TextIndex()
        french = TextIndex(language="fr")
        assert "garanti" in english._tokenize("la garantie de prêt")
        assert french._tokenize("la garantie de prêt est révisée") == [
            "la",
            "garantie",
            "de",
            "prêt",
            "est",
            "révisée",
        ]

    def test_thai_is_cut_into_bigrams(self):
        idx = TextIndex(language="th")
        idx.add("th", "สัญญาเงินกู้")
        idx.add("en", "loan agreement")
        assert [d for d, _ in idx.search("เงินกู้", limit=5)] == ["th"]

    def test_casefold_handles_german_and_turkish(self):
        idx = TextIndex(language="de")
        assert idx._tokenize("Straße GROSS") == ["strasse", "gross"]
        assert idx._tokenize("İstanbul")[0] == "i̇stanbul".casefold()

    def test_a_cjk_run_glued_to_latin_letters_splits_at_the_script_boundary(self):
        idx = TextIndex(language="ja")
        assert idx._tokenize("東京office") == ["東京", "office"]

    def test_english_behaviour_is_unchanged(self):
        idx = TextIndex()
        assert idx._tokenize("the covenants were tested") == ["coven", "test"]

    def test_a_non_english_index_keeps_short_and_stop_words(self):
        """ "die" is a German article and "the" in an English list; a
        French index has no business dropping "de"."""
        assert "die" in TextIndex(language="de")._tokenize("für die Bank")
        assert "the" not in TextIndex()._tokenize("the bank")


class TestLanguageTravelsWithTheCollection:
    def test_it_is_persisted_and_reopened(self, tmp_path):
        from vectrixdb import Vectrix

        db = Vectrix("de", path=str(tmp_path), mode="hybrid", text_language="de")
        db.add(["Der Kreditvertrag wurde zur Überprüfung vorgelegt"])
        db.close()

        again = Vectrix("de", path=str(tmp_path), mode="hybrid")
        try:
            assert again._collection.text_language == "de"
            assert again._collection._text_index.language == "de"
            assert again.search("Überprüfung", limit=1, mode="sparse").top.text.startswith("Der")
        finally:
            again.close()

    def test_english_is_the_default(self, tmp_path):
        from vectrixdb import Vectrix

        db = Vectrix("en", path=str(tmp_path), mode="hybrid")
        try:
            assert db._collection._text_index.language == "en"
        finally:
            db.close()


class TestFieldBoosts:
    def test_a_title_match_outranks_a_body_match(self):
        boosted = TextIndex(use_stemming=False, field_boosts={"title": 3.0})
        boosted.add("a", "an essay about many things", fields={"title": "gardening"})
        boosted.add(
            "b", "gardening gardening is mentioned twice in the body", fields={"title": "essay"}
        )
        assert boosted.search("gardening", limit=2)[0][0] == "a"

        plain = TextIndex(use_stemming=False)
        plain.add("a", "an essay about many things", fields={"title": "gardening"})
        plain.add(
            "b", "gardening gardening is mentioned twice in the body", fields={"title": "essay"}
        )
        assert plain.search("gardening", limit=2)[0][0] == "b"

    def test_unlisted_fields_count_once(self):
        idx = TextIndex(use_stemming=False, field_boosts={"title": 2.0})
        idx.add("a", "body", fields={"tag": "orchid"})
        assert idx._docs["a"]["term_freq"]["orchid"] == 1
