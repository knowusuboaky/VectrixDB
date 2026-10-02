"""Generate or verify the model checksum manifest.

    python scripts/model_checksums.py --write dense          # from a known-good copy
    python scripts/model_checksums.py --verify dense
    python scripts/model_checksums.py --list

Checksums are computed over the files in the models directory (the bundled
``vectrixdb/models/data`` or ``VECTRIXDB_MODELS_DIR``), so run ``--write`` on a
machine whose copy you trust, then commit ``vectrixdb/models/checksums.json``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vectrixdb.models.checksums import compute, load_manifest, save_manifest, verify  # noqa: E402
from vectrixdb.models.embedded import get_models_dir  # noqa: E402


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   --write with a model's name, --verify with one, or --list
# OUTPUT  vectrixdb/models/checksums.json written from a trusted copy; a
#         verdict on this copy; or the manifest listed
#
# Checksums are computed over the files in the models directory, the bundled
# vectrixdb/models/data or VECTRIXDB_MODELS_DIR, so --write runs on a machine
# whose copy you trust, and the manifest is committed.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--write", nargs="+", metavar="TYPE", help="record checksums for these model types"
    )
    parser.add_argument("--verify", nargs="+", metavar="TYPE", help="verify these model types")
    parser.add_argument("--list", action="store_true", help="show what the manifest covers")
    args = parser.parse_args(argv)

    models_dir = get_models_dir()
    manifest = load_manifest()

    if args.list:
        for model_type, files in sorted(manifest.items()):
            print(f"{model_type}: {len(files)} files")
        if not manifest:
            print("manifest is empty")

    for model_type in args.write or []:
        model_dir = models_dir / model_type
        if not model_dir.is_dir():
            print(f"{model_type}: no directory at {model_dir}", file=sys.stderr)
            return 1
        manifest[model_type] = compute(model_dir)
        print(f"{model_type}: recorded {len(manifest[model_type])} files")
    if args.write:
        save_manifest(manifest)

    for model_type in args.verify or []:
        verified = verify(model_type, models_dir / model_type, manifest)
        print(f"{model_type}: verified {len(verified)} files")

    return 0


if __name__ == "__main__":
    sys.exit(main())
