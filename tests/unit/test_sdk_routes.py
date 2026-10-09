"""The routes every SDK calls are in docs/reference/openapi.json.

The four clients are generated and checked from that file, which
scripts/make_reference.py writes from the code and keeps current. A renamed
route fails here before any client's own test runs.
"""

from __future__ import annotations

import json
from pathlib import Path

FILE = Path(__file__).resolve().parents[2] / "docs" / "reference" / "openapi.json"

ROUTES = [
    ("get", "/health"),
    ("get", "/auth/me"),
    ("get", "/api/v1/collections"),
    ("post", "/api/v2/collections"),
    ("get", "/api/v1/collections/{name}"),
    ("delete", "/api/v1/collections/{name}"),
    ("post", "/api/v1/collections/{name}/documents"),
    ("get", "/api/v1/collections/{name}/documents"),
    ("get", "/api/v1/collections/{name}/documents/{doc_id}"),
    ("delete", "/api/v1/collections/{name}/documents/{doc_id}"),
    ("post", "/api/v1/collections/{name}/text-upsert"),
    ("post", "/api/v1/collections/{name}/text-search"),
    ("post", "/api/v1/collections/{name}/text-hybrid-search"),
    ("get", "/api/v1/collections/{name}/sources"),
    ("post", "/api/v1/collections/{name}/sources"),
    ("post", "/api/v1/collections/{name}/sources/refresh"),
    ("delete", "/api/v1/collections/{name}/sources/{source_id}"),
]


def test_the_routes_the_sdks_use_exist():
    paths = json.loads(FILE.read_text(encoding="utf-8"))["paths"]
    for method, path in ROUTES:
        assert method in paths.get(path, {}), f"{method.upper()} {path} is not in the OpenAPI file"


def test_the_fields_the_sdks_send_exist():
    schemas = json.loads(FILE.read_text(encoding="utf-8"))["components"]["schemas"]
    assert {"query_text", "limit", "filter", "rerank"} <= set(
        schemas["TextSearchRequest"]["properties"]
    )
    assert {"id", "text", "payload"} <= set(schemas["TextUpsertPoint"]["properties"])
    assert {"name", "dimension", "enable_text_index", "metric", "description"} <= set(
        schemas["CreateCollectionRequestV2"]["properties"]
    )
    assert {"address", "kind", "every"} <= set(schemas["AddSourceRequest"]["properties"])
