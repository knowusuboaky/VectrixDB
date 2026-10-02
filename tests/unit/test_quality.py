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
