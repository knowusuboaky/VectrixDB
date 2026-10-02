"""Build the wheel and refuse it if it is too large to upload.

    python scripts/check_wheel_size.py [--limit-mb 100]

PyPI rejects files over 100 MB unless the project has been granted a larger
limit. The wheel bundles the English models on purpose, so it works offline
straight after ``pip install``; this keeps that decision inside the limit
instead of discovering the problem at release time.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


# ============================================================================
# SETTINGS: where the repository is
# ============================================================================
#
# The checkout the wheel is built from.

ROOT = Path(__file__).resolve().parent.parent


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   --limit-mb, 100 unless told
# OUTPUT  the wheel built, and exit 1 when it is over the limit
#
# PyPI rejects files over 100 MB unless the project has been granted more. The
# wheel bundles the English models on purpose, so it works offline straight
# after pip install; this keeps that decision inside the limit instead of
# discovering the problem at release time.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--limit-mb", type=float, default=100.0)
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", tmp, str(ROOT)],
            check=True,
            capture_output=True,
        )
        wheels = list(Path(tmp).glob("*.whl"))
        if len(wheels) != 1:
            print(f"expected one wheel, found {wheels}", file=sys.stderr)
            return 1
        size_mb = wheels[0].stat().st_size / (1024 * 1024)
        print(f"{wheels[0].name}: {size_mb:.1f} MiB (limit {args.limit_mb:.0f} MiB)")
        if size_mb > args.limit_mb:
            print("the wheel is over the upload limit", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
