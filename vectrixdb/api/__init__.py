"""
VectrixDB API - REST API server.

The server is optional. FastAPI, uvicorn and pydantic ship in the ``api`` extra
rather than the core install, so that ``pip install vectrixdb`` gives you vector
search without a web stack::

    pip install vectrixdb[api]

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from ..exceptions import DependencyError

try:
    from .server import create_app
    from .extraction import create_extraction_app, extraction_routes, run_extraction_job
except ImportError as exc:  # pragma: no cover - depends on install shape
    _missing = getattr(exc, "name", None) or "fastapi"
    raise DependencyError(_missing, extra="api") from exc

__all__ = ["create_app", "create_extraction_app", "extraction_routes", "run_extraction_job", "app"]


# ============================================================================
# THE OPTIONAL SERVER
# ============================================================================
#
# INPUT   a name asked of the package
# OUTPUT  the server's objects, imported on first use; or an ImportError
#         naming the api extra
#
# FastAPI, uvicorn and pydantic ship in the api extra rather than the core
# install, so pip install vectrixdb gives vector search without a web stack.


def __getattr__(name: str):
    # The default application is built when first asked for, after the server
    # has set its database path, never as a side effect of importing this.
    if name == "app":
        from . import server

        return server.app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
