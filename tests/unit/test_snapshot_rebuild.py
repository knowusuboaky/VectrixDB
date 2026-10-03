"""Snapshots round-trip a collection; rebuild and read-only opens behave."""

import pytest

from vectrixdb import Vectrix
from vectrixdb.exceptions import ConfigurationError
from vectrixdb.snapshot import read_manifest

TEXTS = ["alpha beta gamma", "delta epsilon", "zeta eta theta"]


class TestSnapshot:
    def test_export_then_import_elsewhere(self, tmp_path):
        db = Vectrix("notes", path=str(tmp_path / "src"))
        db.add(TEXTS, metadata=[{"i": i} for i in range(3)])
        db.remember("the cat is Miso", session="s")
        snapshot = db.export(tmp_path / "notes.zip")
        assert snapshot.exists()

        manifest = read_manifest(snapshot)
        assert manifest["name"] == "notes" and manifest["count"] == 4
        assert manifest["mode"] == "dense"

        restored = Vectrix.import_snapshot(snapshot, path=tmp_path / "dst")
        assert restored.count() == 4
        assert restored.search("alpha", limit=1).top.text == "alpha beta gamma"
        assert restored.search("alpha", limit=1).top.metadata["i"] == 0
        assert "Miso" in restored.recall("cat", session="s").top.text

    def test_import_under_a_new_name(self, tmp_path):
        db = Vectrix("one", path=str(tmp_path / "a"))
        db.add(TEXTS)
        snap = db.export(tmp_path / "one")  # .zip is added
        two = Vectrix.import_snapshot(snap, path=tmp_path / "b", name="two")
        assert two.name == "two" and two.count() == 3
        assert (tmp_path / "b" / "two").is_dir()

    def test_refuses_to_overwrite(self, tmp_path):
        db = Vectrix("x", path=str(tmp_path / "a"))
        db.add(TEXTS)
        snap = db.export(tmp_path / "x.zip")
        with pytest.raises(ConfigurationError, match="already exists"):
            Vectrix.import_snapshot(snap, path=tmp_path / "a")

    def test_graph_mode_snapshot_carries_the_graph(self, tmp_path):
        db = Vectrix("kg", path=str(tmp_path / "a"), tier="graph")
        db.add(["Marie Curie discovered radium in Paris."])
        built = len(db.graph.graph.nodes)
        snap = db.export(tmp_path / "kg.zip")
        restored = Vectrix.import_snapshot(snap, path=tmp_path / "b")
        assert restored.default_mode == "graph"
        assert len(restored.graph.graph.nodes) == built

    def test_members_outside_the_target_are_refused_before_anything_is_written(self, tmp_path):
        import json
        import zipfile

        from vectrixdb.snapshot import import_snapshot

        evil = tmp_path / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"format": 1, "name": "c", "mode": "dense"}))
            zf.writestr("c/c.db", "fine")
            zf.writestr("../escaped.txt", "pwned")
            zf.writestr(str(tmp_path / "absolute.txt"), "pwned")
        with pytest.raises(ConfigurationError, match="outside"):
            import_snapshot(evil, tmp_path / "target")
        assert not (tmp_path / "escaped.txt").exists()
        assert not (tmp_path / "absolute.txt").exists()
        assert not (tmp_path / "target" / "c").exists()

    def test_a_manifest_name_that_is_a_path_is_refused(self, tmp_path):
        import json
        import zipfile

        from vectrixdb.snapshot import import_snapshot

        bad = tmp_path / "bad.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr(
                "manifest.json", json.dumps({"format": 1, "name": "../up", "mode": "dense"})
            )
        with pytest.raises(ConfigurationError, match="cannot be imported"):
            import_snapshot(bad, tmp_path / "target")


class TestRebuild:
    def test_rebuild_keeps_search_working_and_drops_tombstones(self, tmp_path):
        db = Vectrix("rb", path=str(tmp_path))
        db.add([f"document number {i} about topic {i % 3}" for i in range(40)])
        victim = db.search("topic 1", limit=1).top.id
        db.delete(victim)
        assert db.rebuild_index() == 39
        assert db.count() == 39
        hits = db.search("topic 1", limit=5)
        assert hits and victim not in hits.ids

        reopened = Vectrix("rb", path=str(tmp_path))
        assert reopened.count() == 39 and reopened.search("topic 2", limit=1)


class TestReadOnly:
    def test_memory_mapped_open_searches_and_refuses_writes(self, tmp_path):
        Vectrix("ro", path=str(tmp_path)).add(TEXTS)
        db = Vectrix("ro", path=str(tmp_path), readonly=True)
        assert db.count() == 3
        assert db.search("alpha", limit=1).top.text == "alpha beta gamma"
        with pytest.raises(ConfigurationError, match="read-only"):
            db.add("more")
        with pytest.raises(ConfigurationError, match="read-only"):
            db.delete(db.search("alpha", limit=1).top.id)
        db.close()

    def test_read_only_does_not_touch_the_index_file(self, tmp_path):
        Vectrix("ro2", path=str(tmp_path)).add(TEXTS)
        index_file = next((tmp_path / "ro2").glob("*.usearch"))
        before = index_file.stat().st_mtime_ns
        db = Vectrix("ro2", path=str(tmp_path), readonly=True)
        db.search("beta")
        db.close()
        assert index_file.stat().st_mtime_ns == before
