"""An in-memory stand-in for the azure-cosmos client the Cosmos DB backend uses.

It implements the subset of ``azure.cosmos.CosmosClient`` (and the database
and container objects it hands back) that ``CosmosDBStorage`` calls: creating
a database or container, fetching an existing one, ``upsert_item``,
``read_item``, ``delete_item`` and ``query_items``. Containers keep their
declared partition key path and route ``read_item``/``delete_item`` through
it, the way the real service does: the wrong partition key for an existing
id behaves exactly like a missing id.

``query_items`` evaluates real SQL text rather than a mock call count, but
only the small fixed set of shapes the backend actually sends: a literal
``c.field = 'value'`` or parameter ``c.field = @name`` equality, an optional
``ORDER BY c.field``, an optional ``OFFSET @o LIMIT @l``, a plain
``SELECT * FROM c``, the id-only projection ``SELECT c.id FROM c ...``, and
the scalar aggregate ``SELECT VALUE COUNT(1) FROM c``.

azure-cosmos is an optional dependency (pyproject.toml's ``azure`` extra)
and is not installed in this environment. ``CosmosDBStorage.create_collection``,
``delete_collection`` and ``ensure_document_tables`` each do a bare
``from azure.cosmos import PartitionKey`` and ``from azure.cosmos.exceptions
import CosmosResourceExistsError`` with no ImportError guard (unlike
``connect()``, which the contract suite never calls for this fake; see
``_cosmos_fake`` in test_storage_contract.py). Importing this module
registers tiny stand-in ``azure.cosmos`` / ``azure.cosmos.exceptions``
modules in ``sys.modules`` so those imports resolve with no network access
and no real package, unless azure-cosmos is genuinely installed, in which
case its own classes are used unchanged and nothing is overridden. Then the
backend catches the SDK's own ``CosmosResourceExistsError``, so that is what
``create_container`` raises: raising the stand-in below failed the document
tests wherever the ``test`` extra had brought azure-cosmos in, CI included.

``CosmosResourceNotFoundError`` does not need that treatment: the backend's
``_is_not_found`` helper (vectrixdb/core/storage.py) matches not-found
exceptions by class name alone, precisely so that optional SDKs are never
imported just to catch an error. Defining a plain, local exception with that
exact name is enough, the same way fake_opensearch.py's own ``NotFoundError``
does not need opensearch-py installed either.
"""

from __future__ import annotations

import itertools
import re
import sys
import types
from typing import Any, Dict, List, Optional


class PartitionKey:
    """Shaped like azure.cosmos.PartitionKey: records the partition path."""

    def __init__(self, path: str = "/id") -> None:
        self.path = path


class CosmosResourceExistsError(Exception):
    """Shaped like azure.cosmos.exceptions.CosmosResourceExistsError."""

    def __init__(self, message: str = "resource exists") -> None:
        super().__init__(message)
        self.status_code = 409


class CosmosResourceNotFoundError(Exception):
    """Shaped like azure.cosmos.exceptions.CosmosResourceNotFoundError.

    Matched by name (see ``_is_not_found`` in vectrixdb/core/storage.py), so
    only the class name below matters, not where it lives.
    """

    def __init__(self, message: str = "resource not found") -> None:
        super().__init__(message)
        self.status_code = 404


def _exists_error(message: str) -> Exception:
    """The error for a container that already exists, of the class the backend
    catches: the SDK's when azure-cosmos is installed, else the stand-in."""
    try:
        from azure.cosmos.exceptions import CosmosResourceExistsError as installed
    except ImportError:
        installed = CosmosResourceExistsError
    if installed is CosmosResourceExistsError:
        return CosmosResourceExistsError(message)
    return installed(status_code=409, message=message)


def _ensure_azure_cosmos_stub() -> None:
    """Make ``from azure.cosmos import ...`` resolve without azure-cosmos installed.

    No-op if the real package is importable, or if the stub is already in
    place (this runs once at module import time, below).
    """
    try:
        import azure.cosmos  # noqa: F401

        return
    except ImportError:
        pass

    if "azure.cosmos" in sys.modules:
        return

    cosmos_module = types.ModuleType("azure.cosmos")
    exceptions_module = types.ModuleType("azure.cosmos.exceptions")

    exceptions_module.CosmosResourceExistsError = CosmosResourceExistsError
    exceptions_module.CosmosResourceNotFoundError = CosmosResourceNotFoundError

    cosmos_module.PartitionKey = PartitionKey
    cosmos_module.CosmosClient = FakeCosmosClient
    cosmos_module.exceptions = exceptions_module

    sys.modules["azure.cosmos"] = cosmos_module
    sys.modules["azure.cosmos.exceptions"] = exceptions_module


# One query shape, covering the seven exact strings CosmosDBStorage sends:
#   SELECT c.id FROM c WHERE c.type = 'collection'
#   SELECT * FROM c ORDER BY c._ts OFFSET @offset LIMIT @limit
#   SELECT VALUE COUNT(1) FROM c
#   SELECT * FROM c WHERE c.id = @doc_id            (also @node_id)
#   SELECT * FROM c
#   SELECT * FROM c WHERE c.doc_id = @doc_id ORDER BY c.position
#   SELECT * FROM c WHERE c.parent_id = @parent_id ORDER BY c.position
_QUERY_RE = re.compile(
    r"^SELECT\s+(?P<select>\*|c\.id|VALUE COUNT\(1\))\s+FROM\s+c"
    r"(?:\s+WHERE\s+c\.(?P<field>\w+)\s*=\s*"
    r"(?:'(?P<literal>(?:[^']|'')*)'|(?P<param>@\w+)))?"
    r"(?:\s+ORDER BY\s+c\.(?P<order>\w+))?"
    r"(?:\s+OFFSET\s+(?P<offset>@\w+)\s+LIMIT\s+(?P<limit>@\w+))?"
    r"\s*$",
    re.IGNORECASE,
)


class FakeCosmosContainer:
    """One container's items, keyed by id, aware of its own partition key path."""

    def __init__(self, container_id: str, partition_key: Optional[PartitionKey] = None) -> None:
        self.id = container_id
        path = getattr(partition_key, "path", None) or "/id"
        self._partition_field = path.lstrip("/")
        self._items: Dict[str, Dict[str, Any]] = {}
        self._clock = itertools.count(1)

    def _pk_value(self, item: Dict[str, Any]) -> Any:
        return item.get(self._partition_field)

    # -- writes ---------------------------------------------------------

    def upsert_item(self, body: Dict[str, Any], **_: Any) -> Dict[str, Any]:
        stored = dict(body)
        stored["_ts"] = next(self._clock)
        self._items[stored["id"]] = stored
        return dict(stored)

    def delete_item(self, item: str, partition_key: Any = None, **_: Any) -> None:
        stored = self._items.get(item)
        if stored is None or (
            partition_key is not None and self._pk_value(stored) != partition_key
        ):
            raise CosmosResourceNotFoundError(f"no item {item!r}")
        del self._items[item]

    # -- reads ------------------------------------------------------------

    def read_item(self, item: str, partition_key: Any = None, **_: Any) -> Dict[str, Any]:
        stored = self._items.get(item)
        if stored is None or (
            partition_key is not None and self._pk_value(stored) != partition_key
        ):
            raise CosmosResourceNotFoundError(f"no item {item!r}")
        return dict(stored)

    def query_items(
        self,
        query: str,
        parameters: Optional[List[Dict[str, Any]]] = None,
        partition_key: Any = None,
        enable_cross_partition_query: Optional[bool] = None,
        **_: Any,
    ) -> List[Any]:
        match = _QUERY_RE.match(str(query).strip())
        if not match:
            raise ValueError(f"fake Cosmos cannot evaluate query {query!r}")
        bound = {p["name"]: p["value"] for p in (parameters or [])}

        rows = list(self._items.values())
        if partition_key is not None:
            rows = [r for r in rows if self._pk_value(r) == partition_key]

        field, literal, param = match.group("field"), match.group("literal"), match.group("param")
        if field:
            wanted = literal.replace("''", "'") if literal is not None else bound[param]
            rows = [r for r in rows if r.get(field) == wanted]

        order = match.group("order")
        if order:
            rows.sort(key=lambda r: r.get(order))

        select = match.group("select")
        if select.upper() == "VALUE COUNT(1)":
            return [len(rows)]

        offset_key, limit_key = match.group("offset"), match.group("limit")
        if offset_key:
            start = bound[offset_key]
            rows = rows[start : start + bound[limit_key]]

        if select == "c.id":
            return [{"id": r["id"]} for r in rows]
        return [dict(r) for r in rows]


class FakeCosmosDatabase:
    """Tracks the containers created under one database, by id."""

    def __init__(self, database_id: str) -> None:
        self.id = database_id
        self._containers: Dict[str, FakeCosmosContainer] = {}

    def create_container_if_not_exists(
        self, id: str, partition_key: Optional[PartitionKey] = None, **_: Any
    ) -> FakeCosmosContainer:
        if id not in self._containers:
            self._containers[id] = FakeCosmosContainer(id, partition_key)
        return self._containers[id]

    def create_container(
        self, id: str, partition_key: Optional[PartitionKey] = None, **_: Any
    ) -> FakeCosmosContainer:
        if id in self._containers:
            raise _exists_error(f"container {id!r} already exists")
        self._containers[id] = FakeCosmosContainer(id, partition_key)
        return self._containers[id]

    def get_container_client(self, id: str) -> FakeCosmosContainer:
        try:
            return self._containers[id]
        except KeyError:
            raise CosmosResourceNotFoundError(f"no container {id!r}") from None

    def delete_container(self, id: str) -> None:
        try:
            del self._containers[id]
        except KeyError:
            raise CosmosResourceNotFoundError(f"no container {id!r}") from None


class FakeCosmosClient:
    """Tracks the databases created under one account, by id.

    Only the idempotent ``create_database_if_not_exists`` is implemented at
    this level: CosmosDBStorage.connect() is monkeypatched rather than run
    for real (see ``_cosmos_fake`` in test_storage_contract.py), so the
    real, non-idempotent ``CosmosClient.create_database`` the backend would
    otherwise call is never exercised and is not faked here.
    """

    def __init__(self, endpoint: Optional[str] = None, key: Optional[str] = None) -> None:
        self.endpoint = endpoint
        self.key = key
        self._databases: Dict[str, FakeCosmosDatabase] = {}

    def create_database_if_not_exists(self, id: str, **_: Any) -> FakeCosmosDatabase:
        if id not in self._databases:
            self._databases[id] = FakeCosmosDatabase(id)
        return self._databases[id]

    def get_database_client(self, id: str) -> FakeCosmosDatabase:
        try:
            return self._databases[id]
        except KeyError:
            raise CosmosResourceNotFoundError(f"no database {id!r}") from None


_ensure_azure_cosmos_stub()
