"""create_app() must produce a complete application every time it is called.

Route decorators were applied to the module-level singleton, so a second
create_app() came back with middleware and the dashboard mount but no
endpoints. Routes now register on an APIRouter that create_app() includes.
"""

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from vectrixdb.api import server  # noqa: E402


def _paths(routes) -> set:
    """Every path reachable from a route table.

    Recent FastAPI keeps an included router as one nested entry instead of
    flattening it, so a flat scan of ``app.routes`` sees the docs routes and
    nothing else. Recurse into anything that has routes of its own.
    """
    found = set()
    for route in routes:
        if hasattr(route, "original_router"):
            found |= _paths(route.original_router.routes)
        elif hasattr(route, "routes"):
            found |= _paths(route.routes)
        elif getattr(route, "path", None):
            found.add(route.path)
    return found


API_PATHS = {r.path for r in server.router.routes}


def test_the_router_is_not_empty():
    assert "/api/collections" in API_PATHS and len(API_PATHS) > 40


def test_a_second_app_has_every_route():
    first = server.create_app(enable_dashboard=False)
    second = server.create_app(enable_dashboard=False)
    assert _paths(first.routes) == _paths(second.routes)
    assert API_PATHS <= _paths(second.routes)


def test_the_module_app_matches_a_fresh_one():
    fresh = server.create_app(enable_dashboard=False)
    assert API_PATHS <= _paths(server.app.routes)
    assert API_PATHS <= _paths(fresh.routes)


def test_a_fresh_app_serves_requests():
    with TestClient(server.create_app(enable_dashboard=False)) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.json()["name"] == "VectrixDB"


def test_the_websocket_route_survived_the_move():
    assert "/ws" in _paths(server.create_app(enable_dashboard=False).routes)


def test_a_fresh_app_serves_an_api_route():
    with TestClient(server.create_app(enable_dashboard=False)) as client:
        assert client.get("/api/collections").status_code == 200
