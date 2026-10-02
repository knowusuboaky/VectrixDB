"""Zip a model directory as the release asset ``vectrixdb download-models`` fetches, and print the upload command.

    python scripts/publish_models.py                  # the types this can publish, and which are here
    python scripts/publish_models.py dense_en         # dist/models/dense_en.zip, and the gh command
    python scripts/publish_models.py dense_en rebel   # several
    python scripts/publish_models.py --all            # every type whose model is here

The downloader asks GitHub for ``releases/download/<tag>/<asset>.zip`` under
the tag in ``vectrixdb.models.embedded.MODEL_CONFIG``, falling back to
HuggingFace when that answers 404. No release has been made for any tag yet,
so the fallback has never worked, and ``dense_en`` (e5-small-v2, the English
default before 2.2) is not in the wheel either: a collection written with it
cannot fetch its model on a host without HuggingFace access until the release
exists. This makes the zip, with the files at its root the way the downloader
expects them, and prints the ``gh release`` command. It never runs ``gh``:
publishing is the maintainer's act, and needs their credentials.

The model directory is read from the models directory, the bundled
``vectrixdb/models/data`` or ``VECTRIXDB_MODELS_DIR``, so fetch or export the
model first (``vectrixdb download-models --type <type>``), check it is INT8
(``python scripts/quantize_models.py``), and record its checksums
(``python scripts/model_checksums.py --write <type>``) before publishing:
a download is verified against the manifest the wheel carries.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vectrixdb.models import checksums  # noqa: E402
from vectrixdb.models.downloader import RELEASE_ASSETS, publish_commands  # noqa: E402
from vectrixdb.models.embedded import MODEL_CONFIG, get_models_dir  # noqa: E402


# ============================================================================
# SETTINGS: where the zips go
# ============================================================================
#
# dist/models under the repository, next to where the wheel is built.

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "models"


# ============================================================================
# THE ZIP
# ============================================================================
#
# INPUT   a model type, and where the zip goes
# OUTPUT  <out>/<asset>.zip holding the model directory's files at its root
#
# Flat on purpose: the downloader flattens one nested folder but verifies
# checksums against the file names, and a flat zip is the shape it was written
# against. Backups a quantization left behind are not part of a model.


def zip_model(model_type: str, out_dir: Path = DIST, models_dir: Path | None = None) -> Path:
    """Zip the model's directory into ``out_dir/<asset>.zip`` and return the path."""
    if model_type not in RELEASE_ASSETS:
        raise SystemExit(
            f"{model_type}: not a type the downloader fetches from a release; one of {', '.join(sorted(RELEASE_ASSETS))}"
        )
    asset, folder = RELEASE_ASSETS[model_type]
    source = (models_dir or get_models_dir()) / folder
    if not source.is_dir():
        raise SystemExit(
            f"{model_type}: no model at {source}; fetch or export it first (vectrixdb download-models --type {model_type})"
        )
    files = sorted(p for p in source.rglob("*") if p.is_file() and not p.name.endswith(".backup"))
    if not files:
        raise SystemExit(f"{model_type}: {source} is empty")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{asset}.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, path.relative_to(source).as_posix())
    return target


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   model types, or --all, or nothing
# OUTPUT  the zips under dist/models and the gh command for each; or, with no
#         types, the list of what can be published and what is here
#
# Exit 1 when a type's checksums are not on record, since the download would
# be refused as unverified anyway.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "types",
        nargs="*",
        help="model types to zip, as vectrixdb download-models --type names them",
    )
    parser.add_argument(
        "--all", action="store_true", help="every type whose model directory is here"
    )
    parser.add_argument(
        "--out", type=Path, default=DIST, help="where the zips go (default: dist/models)"
    )
    args = parser.parse_args(argv)

    models_dir = get_models_dir()
    manifest = checksums.load_manifest()
    # The alias "colbert" names the same asset as late_interaction_en.
    publishable = [t for t in RELEASE_ASSETS if t != "colbert"]

    if args.all:
        args.types = [t for t in publishable if (models_dir / RELEASE_ASSETS[t][1]).is_dir()]
    if not args.types:
        print(f"{'type':20} {'release tag':18} {'asset':22} {'here':5} checksums")
        for t in publishable:
            asset, folder = RELEASE_ASSETS[t]
            tag = MODEL_CONFIG[t]["github_release"]
            here = "yes" if (models_dir / folder).is_dir() else "no"
            recorded = "yes" if manifest.get(asset) else "no"
            print(f"{t:20} {tag:18} {asset + '.zip':22} {here:5} {recorded}")
        print(f"\nmodels directory: {models_dir}")
        return 0

    status = 0
    for model_type in args.types:
        target = zip_model(model_type, args.out, models_dir)
        size_mb = target.stat().st_size / (1024 * 1024)
        print(f"{model_type}: {target} ({size_mb:.1f} MB)")
        asset, _folder = RELEASE_ASSETS[model_type]
        shown = str(target.relative_to(ROOT) if target.is_relative_to(ROOT) else target)
        if not manifest.get(asset):
            print(
                f"  no checksums on record: run python scripts/model_checksums.py --write {asset} and commit the manifest first"
            )
            status = 1
        print("  then, as the maintainer:")
        for command in publish_commands(model_type):
            if not command.startswith("python "):
                print(f"    {command.replace(f'dist/models/{asset}.zip', shown)}")
        print(
            f"  (a release that already exists takes: gh release upload {MODEL_CONFIG[model_type]['github_release']} {shown} --clobber)"
        )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
