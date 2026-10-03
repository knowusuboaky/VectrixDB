# Handle errors

## Catch everything VectrixDB raises

Every exception the library raises on purpose derives from `VectrixError`, so one
`except` insulates your code from it:

```python
import logging

from vectrixdb import (
    StorageBackend,
    StorageConfig,
    Vectrix,
    VectrixError,
    create_storage,
)

log = logging.getLogger(__name__)
db = Vectrix("reports", path="./data")
db.add(["Q3 revenue rose 12 percent"])

storage = create_storage(
    StorageConfig(backend=StorageBackend.SQLITE, sqlite_path="./data")
)
storage.connect()


def fallback_keyword_search(query):
    return []


try:
    results = db.search("quarterly revenue")
except VectrixError as exc:
    log.warning("vector search unavailable: %s", exc)
    results = fallback_keyword_search("quarterly revenue")
```

Programming errors such as `TypeError` are deliberately **not** wrapped. They
propagate unchanged, because they are bugs in the calling code rather than
conditions to handle.

## Absence is a result; failure is an exception

This is the rule the whole storage layer follows.

A document that does not exist is a normal outcome, and you get `None`:

```python
row = storage.get("docs", "no-such-id")  # -> None
```

A backend that cannot be reached is **not** a normal outcome, and it raises:

```python
from vectrixdb import StorageOperationError

try:
    row = storage.get("docs", "id-1")
except StorageOperationError as exc:
    # exc names the backend and the operation; exc.__cause__ is the driver error
    log.error("storage unreachable: %s", exc)
    raise
```

!!! warning "Changed in 2.2.0"
    Earlier versions returned `None`, `[]` or `0` when a backend read failed, so
    an unreachable database looked exactly like an empty collection. If you have
    code that treats an empty result as "no data", check whether it should now
    handle `StorageOperationError` instead.

## Reach for the specific type

```python
from vectrixdb import (
    VectrixError,  # everything below
    ConfigurationError,  # bad or missing configuration
    DependencyError,  # an optional extra is not installed
    StorageError,  # backend problems
    StorageConnectionError,  #   could not connect or authenticate
    StorageOperationError,  #   an operation failed
    CollectionNotFoundError,
    CollectionAlreadyExistsError,
    DocumentNotFoundError,
    DimensionMismatchError,  # vector width does not match the collection
    SearchError,
    QuantizationError,
    IndexBuildError,
    ModelError,  # embedded model problems
    ModelNotFoundError,
    ModelDownloadError,
)
```

Catch the narrowest type that matches what you can actually do about it. Retrying
is sensible for `StorageConnectionError` and pointless for
`DimensionMismatchError`.

## Missing optional dependencies

Extras raise `DependencyError`, which names the extra to install rather than
leaving you to decode an `ImportError`:

```python
try:
    import vectrixdb.api
except DependencyError as exc:
    print(exc)
    # 'fastapi' is required for this feature. Install it with: pip install vectrixdb[api]
```

## Keep the original cause

Wrapped errors are chained with `raise ... from`, so the driver-level exception
is preserved:

```python
try:
    row = storage.get("docs", "id-1")
except StorageOperationError as exc:
    original = exc.__cause__      # e.g. CosmosHttpResponseError
    log.error("backend said: %s", original)
```

Logging `exc_info=True` gives you both halves of the traceback.

Every refusal the server and the extraction service can send, with its status and its words, is on [Errors](../reference/errors.md), read off the code.
