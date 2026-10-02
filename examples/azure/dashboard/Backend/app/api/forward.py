"""The forwarded routes: everything the pages ask of the retrieval service.

Nine path shapes, named one by one rather than a catch-all, so this router
never stands in front of the built pages and never forwards a path nobody
asked it to. What comes back is streamed: a document download or an upload is
not held in this process's memory on its way through.

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from app.core.headers import downward, from_another_site, upward
from app.core.settings import forwardable
from app.integrations.retrieval_integration import target

router = APIRouter()

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


@router.api_route("/api/{rest:path}", methods=METHODS, include_in_schema=False)
@router.api_route("/auth/{rest:path}", methods=METHODS, include_in_schema=False)
@router.api_route("/health", methods=["GET"], include_in_schema=False)
@router.api_route("/health/{rest:path}", methods=["GET"], include_in_schema=False)
@router.api_route("/docs", methods=["GET"], include_in_schema=False)
@router.api_route("/openapi.json", methods=["GET"], include_in_schema=False)
@router.api_route("/brand.json", methods=["GET"], include_in_schema=False)
@router.api_route("/brand.css", methods=["GET"], include_in_schema=False)
@router.api_route("/brand/{rest:path}", methods=["GET"], include_in_schema=False)
async def forward(request: Request) -> Response:
    """One call, passed to the retrieval service as it arrived, and its answer passed back as it came.

    Annotated as a plain Response on purpose: a union of two response classes
    is something FastAPI tries to build a response model from, and it refuses
    at import with a message about Pydantic fields.
    """
    settings = request.app.state.settings
    client: httpx.AsyncClient = request.app.state.client
    path = request.url.path

    if request.method not in ("GET", "HEAD", "OPTIONS") and from_another_site(request.headers):
        return JSONResponse(
            {"detail": "a call from a page on another site is not forwarded"}, status_code=403
        )
    if not forwardable(path, settings.forwarded):
        return JSONResponse({"detail": f"{path} is not forwarded"}, status_code=404)
    if not settings.ready:
        return JSONResponse(
            {
                "detail": "UPSTREAM is not set, so there is nothing to forward to. Put the retrieval service's address in Backend/.env."
            },
            status_code=503,
        )

    call = client.build_request(
        request.method,
        target(path, request.url.query, settings),
        headers=upward(request.headers.raw, key=settings.key, key_header=settings.key_header),
        content=request.stream(),
    )
    try:
        answer = await client.send(call, stream=True)
    except httpx.HTTPError as exc:
        # The service is asleep, unreachable or slower than its own ceiling.
        # Said plainly, in the shape every refusal takes, and never as a 500
        # from this service, which is up.
        return JSONResponse(
            {"detail": f"the retrieval service did not answer: {type(exc).__name__}"},
            status_code=502,
        )

    if answer.is_stream_consumed:
        # A transport that hands back a body already loaded, the fake one the
        # tests use for instance, has nothing left to stream.
        passed: Response = Response(
            content=answer.content,
            status_code=answer.status_code,
            background=BackgroundTask(answer.aclose),
        )
    else:
        passed = StreamingResponse(
            answer.aiter_raw(),
            status_code=answer.status_code,
            background=BackgroundTask(answer.aclose),
        )
    # Its own headers, whole: the content type, the encoding the body is still
    # in, the cache rules, and every cookie a sign-in set.
    passed.raw_headers = downward(answer.headers.raw)
    return passed
