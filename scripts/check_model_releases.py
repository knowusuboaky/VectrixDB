"""Which GitHub release assets the model downloader can actually fetch.

    python scripts/check_model_releases.py            # every downloadable type; exit 1 when any is missing
    python scripts/check_model_releases.py dense_en   # one or more types

``vectrixdb download-models`` tries ``releases/download/<tag>/<asset>.zip``
on GitHub before HuggingFace. A tag nobody has released answers 404 to
everyone, which the downloader reports as a network fault unless it is told
otherwise; this sends a HEAD to each URL and says which exist. Run nightly as
an advisory job, and by hand after publishing with scripts/publish_models.py.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vectrixdb.models.downloader import RELEASE_ASSETS, release_asset_url  # noqa: E402


# ============================================================================
# THE CHECK
# ============================================================================
#
# INPUT   a release asset URL
# OUTPUT  "ok", or what the server answered
#
# GitHub answers a release asset with a 302 to its CDN; urlopen follows it,
# so a 200 at the end means the asset is there. A HEAD costs nothing to
# download.


def head(url: str, timeout: float = 30.0) -> str:
    try:
        with urlopen(
            Request(url, method="HEAD", headers={"User-Agent": "VectrixDB-ReleaseCheck/1.0"}),
            timeout=timeout,
        ) as response:
            return "ok" if response.status == 200 else f"HTTP {response.status}"
    except HTTPError as e:
        return "missing (HTTP 404)" if e.code == 404 else f"HTTP {e.code}"
    except URLError as e:
        return f"unreachable: {e.reason}"


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   model types, or none for all of them
# OUTPUT  a row per type with its URL and whether the asset is there; exit 1
#         when any is not


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "types", nargs="*", help="model types; default is every type with a release tag"
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    types = args.types or [t for t in RELEASE_ASSETS if t != "colbert"]
    missing = []
    for model_type in types:
        url = release_asset_url(model_type)
        if url is None:
            print(f"{model_type:20} no release tag")
            continue
        verdict = head(url, args.timeout)
        print(f"{model_type:20} {verdict:22} {url}")
        if verdict != "ok":
            missing.append(model_type)
    if missing:
        print(
            f"\n{len(missing)} of {len(types)} release assets cannot be fetched: {', '.join(missing)}"
        )
        print(
            "Publish with: python scripts/publish_models.py <type>, then the gh command it prints."
        )
        return 1
    print(f"\nall {len(types)} release assets are there")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
