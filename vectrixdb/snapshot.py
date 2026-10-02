"""One-file snapshots of a local collection: vectors, index, texts, graph.

``export`` writes a zip holding everything under the collection's path plus a
manifest naming the mode and models it was built with, so ``import_snapshot``
can open it with the same configuration. The format is plain: unzip it and
the files are the ones VectrixDB wrote.
"""

from __future__ import annotations

import json
import zipfile
from datetime import timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterator, Optional

from ._time import utcnow
from .exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: no cover
    from .easy import Vectrix

__all__ = ["FORMAT", "export", "import_snapshot", "read_manifest"]


# ============================================================================
# SETTINGS: the format's version
# ============================================================================
#
# Written into the manifest, so a snapshot from another version is told apart.

FORMAT = 1


# ============================================================================
# EXPORT AND IMPORT
# ============================================================================
#
# INPUT   a collection and a target; a snapshot and a path
# OUTPUT  every file that belongs to the collection, zipped with a manifest
#         naming it; the manifest read; the snapshot unpacked under the path
#         and opened
#
# One file: vectors, index, texts, graph.


def _members(root: Path, name: str) -> Iterator[Path]:
    """Every file that belongs to collection ``name`` under ``root``."""
    for candidate in (root / f"{name}.db", root / f"{name}.db-wal", root / f"{name}.db-shm"):
        if candidate.exists():
            yield candidate
    for folder in (root / name, root / f"{name}_graph"):
        if folder.is_dir():
            for path in sorted(folder.rglob("*")):
                if path.is_file():
                    yield path


def export(db: "Vectrix", target: Path) -> Path:
    """Write ``db`` to a zip at ``target`` and return the path."""
    if not db.path:
        raise ConfigurationError("Only a collection with a local path can be exported")
    root = Path(db.path)
    target = Path(target)
    if target.suffix != ".zip":
        target = target.with_suffix(target.suffix + ".zip")

    # Flush so the zip holds committed state, not a write-ahead log in flight.
    if db._db is None:
        raise ConfigurationError(f"Vectrix({db.name!r}) is closed; open it to export")
    collection = db._collection
    if collection is not None:
        collection.save()
    flush = getattr(db._db, "flush", None)
    if callable(flush):
        flush()
    if db._graph_pipeline is not None:
        db._graph_pipeline.save()

    manifest: Dict[str, Any] = {
        "format": FORMAT,
        "name": db.name,
        "mode": db.default_mode,
        "dense_model": db.dense_model_name,
        "model": db.model_name,
        "dimension": db.dimension,
        "count": db.count(),
        "exported_at": utcnow().astimezone(timezone.utc).isoformat(),
    }
    from . import __version__

    manifest["vectrixdb"] = __version__

    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for path in _members(root, db.name):
            zf.write(path, arcname=path.relative_to(root).as_posix())
    return target


def read_manifest(snapshot: Path) -> Dict[str, Any]:
    with zipfile.ZipFile(snapshot) as zf:
        return json.loads(zf.read("manifest.json"))


def import_snapshot(
    snapshot: Path,
    path: Path,
    name: Optional[str] = None,
    **overrides: Any,
) -> "Vectrix":
    """Unpack ``snapshot`` under ``path`` and open it.

    The collection keeps its exported name unless ``name`` is given. Mode and
    dense model come from the manifest; ``overrides`` are passed to ``Vectrix``
    on top of them. Refuses to overwrite a collection that already exists.
    """
    from .easy import Vectrix

    snapshot = Path(snapshot)
    path = Path(path)
    manifest = read_manifest(snapshot)
    if manifest.get("format") != FORMAT:
        raise ConfigurationError(f"Unsupported snapshot format {manifest.get('format')!r}")

    # Both names become paths under ``path``, so both are held to the rules
    # a collection name is: the manifest's was used as it came.
    from .core.database import validate_collection_name
    from .exceptions import InvalidCollectionName

    source_name: str = manifest.get("name", "")
    name = name or source_name
    try:
        validate_collection_name(source_name)
        validate_collection_name(name)
    except InvalidCollectionName as exc:
        raise ConfigurationError(f"Snapshot cannot be imported: {exc}") from exc
    for existing in (path / f"{name}.db", path / name, path / f"{name}_graph"):
        if existing.exists():
            raise ConfigurationError(f"{existing} already exists; choose another name or path")

    path.mkdir(parents=True, exist_ok=True)
    root = path.resolve()
    with zipfile.ZipFile(snapshot) as zf:
        # Every destination is worked out and checked before anything is
        # written, so a snapshot refused for one member leaves nothing behind.
        planned = []
        for member in zf.namelist():
            if member == "manifest.json" or member.endswith("/"):
                continue
            relative = Path(member)
            parts = list(relative.parts)
            if not parts:
                continue
            # Rename on the way in: the file, the folder and the graph folder
            # all carry the collection name.
            if parts[0] == f"{source_name}.db" or parts[0].startswith(f"{source_name}.db-"):
                parts[0] = parts[0].replace(source_name, name, 1)
            elif parts[0] == source_name:
                parts[0] = name
            elif parts[0] == f"{source_name}_graph":
                parts[0] = f"{name}_graph"
            elif parts[0].startswith(source_name):
                # The index file inside the folder is <name>.usearch
                pass
            inner = Path(*parts)
            if len(parts) > 1 and parts[-1].startswith(source_name + "."):
                inner = inner.with_name(parts[-1].replace(source_name, name, 1))
            destination = path / inner
            # A member named "../x" or "/x" was written wherever it pointed.
            try:
                destination.resolve().relative_to(root)
            except ValueError:
                raise ConfigurationError(
                    f"Snapshot member {member!r} would be written outside {path}; refusing it"
                ) from None
            planned.append((member, destination))

        for member, destination in planned:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(destination, "wb") as dst:
                dst.write(src.read())

    kwargs: Dict[str, Any] = {"mode": manifest.get("mode")}
    if manifest.get("dense_model"):
        kwargs["dense_model"] = manifest["dense_model"]
    kwargs.update(overrides)
    return Vectrix(name, path=str(path), **kwargs)
