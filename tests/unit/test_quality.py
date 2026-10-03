"""The extraction quality signal, and the measurement behind its threshold.

The roadmap held this back until it could be measured against a labelled set
rather than shipped as a heuristic nobody trusts. So the tests are about the
measurement as much as the code: clean prose scores as usable, prose put
through a simulated bad OCR pass does not, and the separation between them
on a rebuilt labelled set stays above the number the threshold was set on.
"""

from __future__ import annotations

import warnings

import pytest

from vectrixdb.quality import (
    DEFAULT_THRESHOLD,
    calibrate,
    degrade,
    extraction_quality,
)

PROSE = (
    "The facility covenant requires a fixed charge coverage ratio of not less "
    "than 1.15x, tested quarterly against the borrower's accounts and reported "
    "to the agent within thirty days of each quarter end. A breach that is not "
    "cured within the grace period is an event of default under the agreement."
)
GARBAGE = (
    "<a~i1i#c0v n@nt  fj[cl  charg c\\ve�aga^;o riof 1e$sthon�.#�x ,t o|+e  qu@�t]ry  @q#+fho  b"
)


class TestTheSignal:
    def test_clean_prose_is_usable(self):
        assert extraction_quality(PROSE).usable

    def test_a_failed_extraction_is_not(self):
        assert not extraction_quality(GARBAGE).usable

    def test_heavy_ocr_noise_is_not(self):
        assert not extraction_quality(degrade(PROSE, 0.4, seed=1)).usable

    def test_light_noise_a_person_can_read_still_is(self):
        """Five percent is a readable page with a few dropped letters. The
        threshold sits above it on purpose: refusing readable pages would be
        the heuristic nobody trusts."""
        assert extraction_quality(degrade(PROSE, 0.05, seed=1)).usable

    def test_the_signals_say_which_way_it_failed(self):
        signals = extraction_quality(GARBAGE).signals
        assert set(signals) == {
            "plain_characters",
            "whole_words",
            "pronounceable",
            "known_words",
            "word_length",
            "varied",
        }
        assert signals["varied"] == 1.0, "garbage is not a loop, and the sixth signal says so"
        assert signals["whole_words"] < 0.2
        assert signals["known_words"] < 0.2
        assert signals["plain_characters"] < extraction_quality(PROSE).signals["plain_characters"]

    def test_clean_markdown_is_usable(self):
        """ "**Docs:**", "*what*" and "everything." are words with marks at
        their edges; read as tokens whole, this scored 0.75 and was refused."""
        markdown = (
            "# Release notes\n\n"
            "* **Faster search:** queries return sooner, especially on large collections.\n"
            "* **Better errors:** messages now say *what* failed, and *why*.\n"
            "* **Safer deletes:** `forget()` asks before removing everything.\n"
            "* **Docs:** new guides for `memory`, `policy`, and `citations`.\n"
        )
        quality = extraction_quality(markdown)
        assert quality.usable and quality.signals["whole_words"] > 0.9

    def test_a_heading_is_not_judged(self):
        """Two words are not enough to call an extraction failed, and a
        heading is not one."""
        assert extraction_quality("Covenant memo").usable
        assert extraction_quality("").score == 0.0

    def test_the_score_is_a_number_between_zero_and_one(self):
        for text in (PROSE, GARBAGE, "x", "1234 5678 9012 3456 7890"):
            assert 0.0 <= extraction_quality(text).score <= 1.0

    def test_to_dict_round_trips(self):
        payload = extraction_quality(PROSE).to_dict()
        assert payload["usable"] is True and payload["threshold"] == DEFAULT_THRESHOLD


# Clean documents of the shapes the docs prose is not, scored on 2026-10-02
# when raising the threshold was weighed and declined; the numbers are quoted
# beside DEFAULT_THRESHOLD. Each is (name, text, score then, clearly usable):
# the last is True where the shape sits over the line with room, and the test
# holds those there, and holds the threshold under the terse Markdown that
# scores 0.81 to 0.82, which is what a raise to 0.80 or above would refuse.
CLEAN_SHAPES = [
    (
        "terse markdown",
        "# Deploy\n\n## Steps\n- Build the image\n- Push to registry\n- Roll out\n\n## Rollback\n- Revert the tag\n- Redeploy\n",
        0.819,
        True,
    ),
    (
        "bullet checklist",
        "# Release checklist\n\n- Changelog updated\n- Version bumped\n- Tests green\n- Tag pushed\n- Wheel uploaded\n- Docs published\n",
        0.721,
        False,
    ),
    (
        "markdown table",
        "| Region | Revenue | Growth |\n|---|---|---|\n| EMEA | 1200 | 4% |\n| APAC | 980 | 7% |\n| NA | 1500 | 2% |\n\nTotals exclude returns.\n",
        0.780,
        False,
    ),
    (
        "bare csv",
        "Region, Revenue, Growth\nEMEA, 1200, 4%\nAPAC, 980, 7%\nNA, 1500, 2%\nLATAM, 310, 9%\n",
        0.537,
        False,
    ),
    (
        "email",
        "Hi Dana,\n\nThanks for the draft. Two things before Friday: the budget table on page 3 still shows last year's numbers, and legal wants the indemnity clause back in section 7.\n\nCan you send a revised copy by Thursday noon? I'll circulate it to the board after that.\n\nBest,\nMark\n",
        0.967,
        True,
    ),
    (
        "legal prose",
        '1. Definitions. In this Agreement, "Confidential Information" means any information disclosed by either party to the other, whether orally or in writing, that is designated as confidential or that reasonably should be understood to be confidential given the nature of the information and the circumstances of disclosure.\n\n2. Obligations. The receiving party shall hold the Confidential Information in strict confidence and shall not disclose it to any third party without the prior written consent of the disclosing party, except as required by law.\n',
        0.990,
        True,
    ),
    (
        "code readme",
        '# fastcache\n\nIn-memory LRU cache with TTL.\n\n## Install\n\n```\npip install fastcache\n```\n\n## Usage\n\n```python\nfrom fastcache import Cache\n\nc = Cache(maxsize=1000, ttl=60)\nc.set("key", value)\nc.get("key")\n```\n\n## Options\n\n- `maxsize`: int, default 1000\n- `ttl`: seconds, default None\n- `on_evict`: callback(key, value)\n\n## License\n\nMIT\n',
        0.641,
        False,
    ),
    (
        "slide outline",
        "Q3 Review\n\nRevenue up 12%\nChurn down\nTwo new markets\n\nRisks\nHiring pace\nVendor costs\nFX exposure\n\nNext quarter\nLaunch v3\nClose Series B\nHire 20\n",
        0.766,
        False,
    ),
    (
        "french",
        "La réunion s'est tenue mardi matin dans la salle principale. Les participants ont examiné le budget de l'année prochaine et ont décidé de reporter l'achat du nouveau matériel. Le directeur a rappelé que les délais de livraison restaient serrés et qu'il faudrait prévenir les clients avant la fin du mois.\n",
        0.661,
        False,
    ),
    (
        "spanish",
        "El informe anual muestra un crecimiento sostenido en todas las regiones. Las ventas aumentaron un ocho por ciento respecto al año anterior, impulsadas por la demanda en el mercado europeo. La dirección propone invertir en nuevas instalaciones durante el próximo ejercicio.\n",
        0.718,
        False,
    ),
    (
        "german",
        "Die Sitzung begann um neun Uhr. Der Vorstand besprach die Ergebnisse des letzten Quartals und beschloss, die Investitionen in neue Anlagen zu erhöhen. Die Mitarbeiter werden nächste Woche informiert.\n",
        0.741,
        False,
    ),
    (
        "minutes",
        "Minutes, 14 March\n\nPresent: A. Okafor, L. Chen, R. Patel\nApologies: M. Silva\n\n1. Budget approved as circulated.\n2. Vendor contract: L. Chen to negotiate terms.\n3. Next meeting 28 March.\n",
        0.741,
        False,
    ),
    (
        "api reference",
        "## get_user(id)\n\nReturns the user record.\n\n**Parameters**\n\n- `id` (str): the user id\n\n**Returns**\n\ndict with `name`, `email`, `created_at`\n\n**Raises**\n\n- `NotFound` if no user has that id\n",
        0.885,
        True,
    ),
    (
        "changelog",
        "## [2.2.0] - 2026-10-01\n\n### Added\n- Sharded index for collections over RAM\n- Quality check at ingest\n\n### Fixed\n- Tokeniser kept punctuation on word edges\n- Download retries on 5xx\n",
        0.765,
        False,
    ),
    (
        "recipe",
        "Lemon pasta\n\nServes 2\n\n- 200 g spaghetti\n- 1 lemon\n- 50 g parmesan\n- olive oil, salt, pepper\n\nCook the pasta. Zest and juice the lemon. Toss everything together with a splash of the cooking water. Season and serve.\n",
        0.837,
        True,
    ),
    (
        "faq",
        'Q: How do I reset my password?\nA: Click "Forgot password" on the sign-in page and follow the link in the email.\n\nQ: Can I change my username?\nA: No. Usernames are permanent.\n\nQ: Where is my data stored?\nA: In the region you chose when you created the account.\n',
        0.966,
        True,
    ),
    (
        "requirements list",
        "Requirements\n\n- Must run offline\n- Python 3.9+\n- No GPU needed\n- Under 100 MB installed\n- Windows, macOS, Linux\n",
        0.813,
        True,
    ),
    (
        "memo",
        "To: All staff\nFrom: Facilities\nRe: Parking\n\nThe north lot is closed next week for resurfacing. Use the garage on Elm Street. Passes are at reception.\n",
        1.000,
        True,
    ),
    (
        "product page",
        "Trailrunner 3\n\nLightweight. Waterproof. Built for the long run.\n\n- 240 g per shoe\n- 8 mm drop\n- Recycled upper\n\nAvailable in three colours. Free returns within 30 days.\n",
        0.867,
        True,
    ),
    (
        "log notes",
        "2026-09-30 build failed on main, flaky network test, reran green\n2026-10-01 bumped onnxruntime to 1.19, all tests pass\n2026-10-02 released 2.2.0, wheel 98 MB\n",
        0.740,
        False,
    ),
]


class TestTheShapesCleanTextTakes:
    """The threshold was set on docs prose; these are the shapes clean text
    takes that docs prose does not, and what they scored when a raise was
    weighed. A shape can only move by a little without a detector change."""

    @pytest.mark.parametrize(
        "name, text, then, clearly", CLEAN_SHAPES, ids=[c[0] for c in CLEAN_SHAPES]
    )
    def test_a_clean_shape_scores_what_it_did(self, name, text, then, clearly):
        now = extraction_quality(text).score
        assert abs(now - then) < 0.03, (
            f"{name} moved from {then:.3f} to {now:.3f}: re-run scripts/quality_eval.py and re-quote DEFAULT_THRESHOLD"
        )
        if clearly:
            assert now >= DEFAULT_THRESHOLD + 0.02, (
                f"{name} is a clean document and it is at the line"
            )

    def test_the_threshold_stays_under_terse_clean_markdown(self):
        """A raise to 0.80 or above buys precision on the eval set with the
        terse Markdown a person writes: headings, bullets, a requirements
        list. Those sit at 0.81 to 0.82, so the line stays 0.02 under them."""
        terse = [
            text
            for name, text, _then, _clearly in CLEAN_SHAPES
            if name in ("terse markdown", "requirements list")
        ]
        lowest = min(extraction_quality(text).score for text in terse)
        assert DEFAULT_THRESHOLD <= lowest - 0.02
        assert all(extraction_quality(text).usable for text in terse)

    def test_what_sits_under_the_line_is_the_documented_limit(self):
        """Code, a bare table, lines of two to five words, and prose in a
        language the common-word list does not hold: refused today, and said
        to be in the docs. Not a target; a record, so a change is noticed."""
        under = {
            name
            for name, text, _then, _clearly in CLEAN_SHAPES
            if not extraction_quality(text).usable
        }
        assert {"bare csv", "code readme", "french", "spanish"} <= under
        assert not {"email", "legal prose", "memo", "faq", "terse markdown"} & under


class TestTheMeasurement:
    """The threshold was set from a labelled set, and this rebuilds a small
    one to hold the separation it was set on."""

    def _labelled(self):
        base = [
            PROSE,
            "An entitlement policy is declared once against the collection and "
            "enforced on every read, so a request handler that forgets the filter "
            "is refused rather than answered with everything the query matched.",
            "The audit record stores a keyed fingerprint of the query rather than "
            "the query itself, because a bare hash over a small space of likely "
            "questions can be enumerated by anyone who holds the log.",
            "Every mutation of the collection mints a new build id, and every chunk "
            "carries the build that stored it, so a chunk can be walked back to the "
            "ingestion record that put it there whatever build is current now.",
        ]
        labelled = [(text, True) for text in base]
        labelled += [(degrade(text, 0.05, seed=i), True) for i, text in enumerate(base)]
        labelled += [(degrade(text, 0.2, seed=i), False) for i, text in enumerate(base)]
        labelled += [(degrade(text, 0.4, seed=i), False) for i, text in enumerate(base)]
        return labelled

    def test_the_separation_holds_on_a_small_set(self):
        """Four short paragraphs: enough to hold the shape, not the numbers.
        Short texts at 20% noise land near the line, which is why the
        threshold was set on the full set below and not on this one."""
        calibration = calibrate(self._labelled(), threshold=DEFAULT_THRESHOLD)
        assert calibration.auc >= 0.9
        assert calibration.recall >= 0.9, "clean prose is being refused"
        assert calibration.precision >= 0.7, "garbage is being let through"

    def test_the_measurement_the_threshold_was_set_on_still_holds(self):
        """Rebuilds the labelled set scripts/quality_eval.py used, the same
        way, and holds the numbers quoted beside DEFAULT_THRESHOLD. If the
        detector or the docs prose drifts, this is what says so."""
        import importlib.util
        from pathlib import Path

        script = Path(__file__).resolve().parents[2] / "scripts" / "quality_eval.py"
        spec = importlib.util.spec_from_file_location("quality_eval", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        samples = module.labelled_set()
        decisive = [(text, level <= 0.05) for text, _, level in samples if level != 0.10]
        calibration = calibrate(decisive, threshold=DEFAULT_THRESHOLD)

        assert calibration.samples >= 200, "the docs prose the set is built from has shrunk"
        assert calibration.recall >= 0.97, "readable pages are being refused"
        assert calibration.precision >= 0.9, "garbage is being let through"
        assert calibration.balanced_accuracy >= 0.9
        assert calibration.auc >= 0.99

    def test_calibrate_picks_a_threshold_when_not_given_one(self):
        calibration = calibrate(self._labelled())
        assert 0.0 < calibration.threshold < 1.0
        assert calibration.balanced_accuracy >= 0.8

    def test_calibrate_needs_samples(self):
        with pytest.raises(ValueError, match="at least one"):
            calibrate([])

    def test_degrade_is_deterministic_and_proportional(self):
        assert degrade(PROSE, 0.2, seed=4) == degrade(PROSE, 0.2, seed=4)
        assert degrade(PROSE, 0.0, seed=4) == PROSE
        assert extraction_quality(degrade(PROSE, 0.1, seed=4)).score > (
            extraction_quality(degrade(PROSE, 0.4, seed=4)).score
        )


class TestAtTheWrite:
    @pytest.fixture
    def db(self, tmp_path):
        from vectrixdb import Vectrix

        db = Vectrix("scans", path=str(tmp_path / "scans"))
        yield db
        db.close()

    def test_every_chunk_carries_its_score(self, db):
        db.add_document(PROSE * 3, chunk_size=200, overlap=20)
        scores = [m["_vx_quality"] for _, _, m in db._collection._iter_documents_raw()]
        assert scores and all(s >= DEFAULT_THRESHOLD for s in scores)

    def test_a_failed_extraction_warns_by_default(self, db):
        from vectrixdb.exceptions import ExtractionQualityWarning

        with pytest.warns(ExtractionQualityWarning, match="reads as a failed extraction"):
            db.add_document(degrade(PROSE * 3, 0.4, seed=2), chunk_size=200, overlap=20)
        assert db._count_all() > 0

    def test_reject_refuses_it(self, db):
        from vectrixdb.exceptions import ExtractionQualityError

        with pytest.raises(ExtractionQualityError) as info:
            db.add_document(
                degrade(PROSE * 3, 0.4, seed=2), chunk_size=200, overlap=20, on_low_quality="reject"
            )
        assert info.value.score < info.value.threshold
        assert "signals says which signal failed" in str(info.value)
        assert db._count_all() == 0

    def test_allow_is_silent(self, db):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            db.add_document(
                degrade(PROSE * 3, 0.4, seed=2), chunk_size=200, overlap=20, on_low_quality="allow"
            )
        assert db._count_all() > 0

    def test_clean_prose_does_not_warn(self, db):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            db.add_document(PROSE * 3, chunk_size=200, overlap=20)

    def test_the_threshold_can_be_moved(self, db):
        """A deployment that calibrated against its own labelled set passes
        the number it got."""
        from vectrixdb.exceptions import ExtractionQualityError

        with pytest.raises(ExtractionQualityError):
            db.add_document(
                PROSE * 3, chunk_size=200, on_low_quality="reject", quality_threshold=0.99
            )

    def test_an_unknown_mode_is_refused(self, db):
        with pytest.raises(ValueError, match="on_low_quality"):
            db.add_document(PROSE, on_low_quality="maybe")

    def test_the_evidence_pack_counts_the_low_chunks(self, db, tmp_path):
        import json

        db.add_document(PROSE * 3, chunk_size=200, overlap=20)
        db.add_document(
            degrade(PROSE * 3, 0.4, seed=2), chunk_size=200, overlap=20, on_low_quality="allow"
        )
        lineage = json.loads(
            (db.evidence_pack(tmp_path / "pack").directory / "lineage.json").read_text()
        )
        assert lineage["chunks_scored_for_extraction_quality"] == db._count_all()
        assert 0 < lineage["chunks_below_quality_threshold"] < db._count_all()
        assert lineage["quality_threshold"] == DEFAULT_THRESHOLD
