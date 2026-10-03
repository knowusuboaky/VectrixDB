"""Where sign-in state is kept: a small record layer, and the databases it runs on.

The sign-in store asks six things of wherever it keeps its state: read a
record, make one that must not exist yet, write one back only if nobody changed
it since it was read, write one whatever is there, delete one, and list the
records of a kind, all of them or those with a given value in one of two
indexed fields. Any record can carry a time after which it no longer exists.
That is small enough for any database to do well, and it is what lets the state
live somewhere that outlasts the machine a server runs on:

* SQLite, a file beside the collections. The default, and right for one server.
  ``VECTRIXDB_SIGNIN_STORE`` unset, or ``sqlite:///path/to/signin.db``.
* PostgreSQL, for several servers sharing one sign-in: ``postgresql://user@host/db``,
  with the ``postgres`` extra. The table is ``vectrixdb_signin`` unless the
  address ends ``?table=<name>``.
* Azure Cosmos DB: ``cosmos://<account>.documents.azure.com/<database>/<container>``,
  with the ``azure`` extra. The container is partitioned by ``/kind``, with time
  to live on, and every item that expires says when.
* Amazon DynamoDB: ``dynamodb://<table>?region=<region>``, with the ``aws``
  extra. Keys ``kind`` and ``key``, two sparse indexes, time to live on
  ``expires_at``.

A password or account key never goes in the address, where it would be printed
and logged with it: it is ``VECTRIXDB_SIGNIN_STORE_KEY`` (or ``_KEY_FILE``).
Cosmos DB without one signs in as the machine (``DefaultAzureCredential``: a
managed identity, or ``az login`` on a laptop); DynamoDB always uses the
standard AWS chain, a role or a profile. A container or table that is not there
yet is made, where the identity may make it, and otherwise the error says
exactly what to make.

What a record holds is ``store.py``'s business; nothing here reads it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from ..exceptions import ConfigurationError, DependencyError

__all__ = [
    "CosmosRecords",
    "DynamoRecords",
    "Record",
    "Records",
    "SqlRecords",
    "describe_where",
    "open_records",
]


# ============================================================================
# SETTINGS: the file's name, and the setting
# ============================================================================
#
# The SQLite file beside the collections, and the setting that points
# elsewhere.

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
#: The setting an address came from, when nobody says otherwise: the one this layer was made for.
SIGNIN_SETTING = "VECTRIXDB_SIGNIN_STORE"


# ============================================================================
# A RECORD, AND WHAT A STORE MUST DO
# ============================================================================
#
# INPUT   a kind, a key and what it holds
# OUTPUT  one record, with a time after which it no longer exists; the six
#         things the sign-in store asks of any database
#
# Small enough for any database to do well, which is what lets the state
# outlast the machine.


def _where(setting: str) -> str:
    return (
        f"{setting} is a path to a SQLite file, sqlite:///<path>, postgresql://<user>@<host>/<database>, "
        "cosmos://<account>.documents.azure.com/<database>/<container> or dynamodb://<table>?region=<region>"
    )


@dataclass
class Record:
    """One thing the store keeps: a kind, a key unique within it, and what it holds."""

    kind: str
    key: str
    data: dict = field(default_factory=dict)
    #: When it stops existing, in seconds since the epoch. None: never.
    expires: Optional[float] = None
    #: Values it can also be found by.
    ix1: Optional[str] = None
    ix2: Optional[str] = None
    #: Set by the database it came from. A replace succeeds only while the stored record still has it.
    version: Any = None

    def live(self, now: Optional[float] = None) -> bool:
        return self.expires is None or self.expires > (time.time() if now is None else now)


class Records:
    """What the sign-in store needs of a database. Every method may be called from several threads at once."""

    def get(self, kind: str, key: str) -> Optional[Record]:
        raise NotImplementedError

    def create(self, record: Record) -> bool:
        """Keep a new record. False, and nothing written, when a live one already has its key."""
        raise NotImplementedError

    def replace(self, record: Record) -> bool:
        """Write back a record read earlier, only if it is unchanged since. False when somebody got there first, or it is gone."""
        raise NotImplementedError

    def put(self, record: Record) -> None:
        """Write a record whatever is there."""
        raise NotImplementedError

    def delete(self, kind: str, key: str, version: Any = None) -> bool:
        """Remove a record; with ``version``, only if it is unchanged since it was read. Whether one went."""
        raise NotImplementedError

    def query(
        self, kind: str, *, ix1: Optional[str] = None, ix2: Optional[str] = None
    ) -> list[Record]:
        """The live records of a kind: all of them, or those with these index values."""
        raise NotImplementedError

    def purge(self) -> None:
        """Drop records past their time. Where the database drops them itself, nothing to do."""

    def describe(self) -> str:
        """Where this is, in words fit for a log: never a password."""
        raise NotImplementedError

    def close(self) -> None:
        pass


def _dump(data: dict) -> str:
    return json.dumps(data, separators=(",", ":"), sort_keys=True)


# ============================================================================
# SQL: SQLite or PostgreSQL
# ============================================================================
#
# INPUT   a file, or a connection
# OUTPUT  one table and the same statements for both; a write only if
#         unchanged, retried
#
# The default, and right for one server.


class SqlRecords(Records):
    """SQLite or PostgreSQL: one table, and the same statements for both."""

    def __init__(
        self,
        connection: Any,
        *,
        placeholder: str = "?",
        table: str = "records",
        where: str = "SQLite",
        reconnect: Optional[Callable[[], Any]] = None,
        path: Optional[Path] = None,
    ) -> None:
        if not _NAME.match(table):
            raise ConfigurationError(
                f"{table!r} cannot be the sign-in table's name: letters, digits and _, starting with a letter"
            )
        self._conn = connection
        self._placeholder = placeholder
        self._table = self.table = table
        self._where = where
        self._reconnect = reconnect
        self._lock = threading.RLock()
        self._purged = 0.0
        #: The file, when this is a SQLite file.
        self.path = path
        self._ensure_table()

    @staticmethod
    def statements(table: str) -> list[str]:
        """What makes the table and its indexes, for a database administrator to run where the server may not."""
        t = table
        return [
            f"CREATE TABLE IF NOT EXISTS {t} (kind TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL, version BIGINT NOT NULL, "
            "ix1 TEXT, ix2 TEXT, expires DOUBLE PRECISION, PRIMARY KEY (kind, key))",
            *(
                f"CREATE INDEX IF NOT EXISTS {t}_{name} ON {t} ({columns})"
                for name, columns in (
                    ("ix1", "kind, ix1"),
                    ("ix2", "kind, ix2"),
                    ("expires", "expires"),
                )
            ),
        ]

    def _ensure_table(self) -> None:
        try:
            # A table that is there is used as it is: a user who may read and write it need not be allowed to make tables.
            self._run(f"SELECT 1 FROM {self._table} WHERE 1 = 0", rows=True)
            return
        except Exception:  # noqa: BLE001 - not there, or not readable: making it says which
            pass
        try:
            for statement in self.statements(self._table):
                self._run(statement)
        except Exception as exc:
            if self.sqlite_connection is not None:
                raise
            raise ConfigurationError(
                f"The table {self._table} is not there, and this server could not make it ({exc}). "
                f"Make it once, as a user who may: {'; '.join(self.statements(self._table))}"
            ) from exc

    @classmethod
    def sqlite(cls, path: Any) -> "SqlRecords":
        if str(path) == ":memory:":
            return cls(
                sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None),
                where="SQLite in memory",
            )
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(path), check_same_thread=False, timeout=10, isolation_level=None
        )
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover - a file system without modes
            pass
        return cls(connection, where=f"SQLite file {path}", path=path)

    @classmethod
    def postgres(
        cls,
        url: str,
        *,
        password: Optional[str] = None,
        connect: Optional[Callable[..., Any]] = None,
    ) -> "SqlRecords":
        if connect is None:
            try:
                import psycopg2
            except ImportError as exc:
                raise DependencyError("psycopg2", "postgres") from exc
            connect = psycopg2.connect
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        table = (query.pop("table", None) or ["vectrixdb_signin"])[0]
        dsn = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), ""))
        opener = connect

        def fresh() -> Any:
            connection = opener(dsn, **({"password": password} if password else {}))
            connection.autocommit = True
            return connection

        return cls(
            fresh(), placeholder="%s", table=table, where=describe_where(url), reconnect=fresh
        )

    @property
    def sqlite_connection(self) -> Optional[sqlite3.Connection]:
        """The SQLite connection itself: how a file from before records is brought over. None for PostgreSQL."""
        return self._conn if isinstance(self._conn, sqlite3.Connection) else None

    def _run(self, sql: str, params: Iterable[Any] = (), *, rows: bool = False) -> Any:
        if self._placeholder != "?":
            sql = sql.replace("?", self._placeholder)
        with self._lock:
            for attempt in (1, 2):
                try:
                    cursor = self._conn.cursor()
                    try:
                        cursor.execute(sql, tuple(params))
                        return cursor.fetchall() if rows else cursor.rowcount
                    finally:
                        cursor.close()
                except Exception as exc:
                    # A database restarted or a connection dropped by the network: once more, on a new one.
                    if (
                        attempt == 1
                        and self._reconnect is not None
                        and type(exc).__name__ in ("OperationalError", "InterfaceError")
                    ):
                        try:
                            self._conn.close()
                        except Exception:  # noqa: BLE001 - it is already broken
                            pass
                        self._conn = self._reconnect()
                        continue
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    def get(self, kind: str, key: str) -> Optional[Record]:
        found = self._run(
            f"SELECT data, version, ix1, ix2, expires FROM {self._table} WHERE kind = ? AND key = ?",
            (kind, key),
            rows=True,
        )
        if not found:
            return None
        data, version, ix1, ix2, expires = found[0]
        record = Record(kind, key, json.loads(data), expires, ix1, ix2, version)
        return record if record.live() else None

    def _values(self, record: Record) -> tuple:
        return (record.kind, record.key, _dump(record.data), record.ix1, record.ix2, record.expires)

    def create(self, record: Record) -> bool:
        t = self._table
        # A key held by a record past its time is free: it is taken over, not refused.
        got = self._run(
            f"INSERT INTO {t} (kind, key, data, version, ix1, ix2, expires) VALUES (?, ?, ?, 1, ?, ?, ?) "
            f"ON CONFLICT (kind, key) DO UPDATE SET data = excluded.data, version = {t}.version + 1, ix1 = excluded.ix1, "
            f"ix2 = excluded.ix2, expires = excluded.expires WHERE {t}.expires IS NOT NULL AND {t}.expires <= ? RETURNING version",
            (*self._values(record), time.time()),
            rows=True,
        )
        if not got:
            return False
        record.version = got[0][0]
        return True

    def replace(self, record: Record) -> bool:
        got = self._run(
            f"UPDATE {self._table} SET data = ?, version = version + 1, ix1 = ?, ix2 = ?, expires = ? "
            "WHERE kind = ? AND key = ? AND version = ? RETURNING version",
            (
                _dump(record.data),
                record.ix1,
                record.ix2,
                record.expires,
                record.kind,
                record.key,
                record.version,
            ),
            rows=True,
        )
        if not got:
            return False
        record.version = got[0][0]
        return True

    def put(self, record: Record) -> None:
        t = self._table
        got = self._run(
            f"INSERT INTO {t} (kind, key, data, version, ix1, ix2, expires) VALUES (?, ?, ?, 1, ?, ?, ?) "
            f"ON CONFLICT (kind, key) DO UPDATE SET data = excluded.data, version = {t}.version + 1, ix1 = excluded.ix1, "
            "ix2 = excluded.ix2, expires = excluded.expires RETURNING version",
            self._values(record),
            rows=True,
        )
        record.version = got[0][0]

    def delete(self, kind: str, key: str, version: Any = None) -> bool:
        if version is None:
            gone = self._run(f"DELETE FROM {self._table} WHERE kind = ? AND key = ?", (kind, key))
        else:
            gone = self._run(
                f"DELETE FROM {self._table} WHERE kind = ? AND key = ? AND version = ?",
                (kind, key, version),
            )
        return bool(gone and gone > 0)

    def query(
        self, kind: str, *, ix1: Optional[str] = None, ix2: Optional[str] = None
    ) -> list[Record]:
        sql = f"SELECT key, data, version, ix1, ix2, expires FROM {self._table} WHERE kind = ? AND (expires IS NULL OR expires > ?)"
        params: list = [kind, time.time()]
        if ix1 is not None:
            sql += " AND ix1 = ?"
            params.append(ix1)
        if ix2 is not None:
            sql += " AND ix2 = ?"
            params.append(ix2)
        return [
            Record(kind, k, json.loads(d), e, a, b, v)
            for k, d, v, a, b, e in self._run(sql, params, rows=True)
        ]

    def purge(self) -> None:
        now = time.time()
        if now - self._purged < 60:
            return
        self._purged = now
        self._run(f"DELETE FROM {self._table} WHERE expires IS NOT NULL AND expires <= ?", (now,))

    def load(self, records: Sequence[Record], *, drop: Sequence[str] = ()) -> None:
        """Write many records and drop old tables, all of it or none of it. SQLite only: a file's move to records."""
        connection = self.sqlite_connection
        assert connection is not None, "only a SQLite file has tables from before records"
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.executemany(
                    f"INSERT OR REPLACE INTO {self._table} (kind, key, data, version, ix1, ix2, expires) VALUES (?, ?, ?, 1, ?, ?, ?)",
                    [self._values(r) for r in records],
                )
                for table in drop:
                    if not _NAME.match(table):
                        raise ConfigurationError(f"{table!r} is not a table name")
                    connection.execute(f'DROP TABLE IF EXISTS "{table}"')
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def describe(self) -> str:
        return self._where

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ============================================================================
# AZURE COSMOS DB
# ============================================================================
#
# INPUT   an account and a container
# OUTPUT  one container partitioned by kind; an item that expires carries its
#         own time to live; moments as ISO text and back
#
# For a server on Azure that should keep nothing on its own disk.


def _status(exc: BaseException) -> Optional[int]:
    return getattr(exc, "status_code", None)


def _if_unchanged() -> Any:
    try:
        from azure.core import MatchConditions
    except ImportError:  # only where the container is not the SDK's
        return "IfNotModified"
    return MatchConditions.IfNotModified


#: What a Cosmos item id may not hold, plus "%" itself, so the escaping is
#: undone exactly and two keys can never come out as one id.
_ID_UNSAFE = {"%": "%25", "/": "%2F", "\\": "%5C", "?": "%3F", "#": "%23"}
#: The longest id Cosmos takes.
_ID_LIMIT = 255


def _iso(seconds: float) -> str:
    """A moment as a person reads it: 2026-09-23T14:02:11.204000Z."""
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(seconds, tz=timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _seconds(value: Any) -> Optional[float]:
    """A moment back as seconds since the epoch, from a number or from ``_iso``'s text."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    from datetime import datetime

    text = str(value).strip()
    return datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text).timestamp()


class CosmosRecords(Records):
    """Azure Cosmos DB for NoSQL: one container partitioned by kind; an item that expires carries its own time to live.

    An item reads in the portal as what it is. Its id is the kind, a dot and
    the key, ``person.ama@bank.example`` or ``visibility.financial``, with only
    the characters Cosmos refuses escaped. Its times are written as dates,
    ``expires`` and ``updated_at``, with ``expires_at`` beside them as the exact
    number the code compares and ``ttl`` as the seconds Cosmos counts down.
    """

    def __init__(self, container: Any, *, where: str = "Cosmos DB") -> None:
        self._container = container
        self._where = where

    @classmethod
    def open(
        cls, url: str, *, key: Optional[str] = None, setting: str = SIGNIN_SETTING
    ) -> "CosmosRecords":
        try:
            from azure.cosmos import CosmosClient, PartitionKey
        except ImportError as exc:
            raise DependencyError("azure-cosmos", "azure") from exc
        parts = urlsplit(url)
        names = [p for p in parts.path.split("/") if p]
        if not parts.hostname or len(names) != 2:
            raise ConfigurationError(
                f"For Cosmos DB, {setting} reads cosmos://<account>.documents.azure.com/<database>/<container>"
            )
        credential: Any = key
        if not credential:
            try:
                from azure.identity import DefaultAzureCredential
            except ImportError as exc:
                raise DependencyError("azure-identity", "azure") from exc
            credential = DefaultAzureCredential()
        client = CosmosClient(
            f"https://{parts.hostname}:{parts.port or 443}/", credential=credential
        )
        database_name, container_name = names
        container = client.get_database_client(database_name).get_container_client(container_name)
        try:
            container.read()
        except Exception as exc:
            if _status(exc) != 404:
                raise
            try:
                database = client.create_database_if_not_exists(database_name)
                container = database.create_container_if_not_exists(
                    id=container_name, partition_key=PartitionKey(path="/kind"), default_ttl=-1
                )
            except Exception as refused:
                raise ConfigurationError(
                    f"The Cosmos DB container {database_name}/{container_name} is not there, and this server may not make it. "
                    "Make it with the partition key /kind and time to live on with no default (-1), then start again"
                ) from refused
        return cls(container, where=describe_where(url))

    @staticmethod
    def _id(kind: str, key: str) -> str:
        """``<kind>.<key>``, with only what Cosmos refuses in an id escaped.

        An id may not hold / \\ ? or #, and an address or a collection name
        might. Past 255 characters the key becomes its hash, after a ``%`` no
        escaped key can start with, so the two forms never name one item.
        """
        readable = f"{kind}." + "".join(_ID_UNSAFE.get(ch, ch) for ch in key)
        if len(readable) <= _ID_LIMIT:
            return readable
        return f"{kind}.%sha256-" + hashlib.sha256(key.encode()).hexdigest()

    def _body(self, record: Record) -> dict:
        now = time.time()
        body = {
            "id": self._id(record.kind, record.key),
            "kind": record.kind,
            "key": record.key,
            "data": record.data,
            "ix1": record.ix1,
            "ix2": record.ix2,
            "expires": _iso(record.expires) if record.expires is not None else None,
            "expires_at": record.expires,
            "updated_at": _iso(now),
        }
        if record.expires is not None:
            body["ttl"] = max(1, math.ceil(record.expires - now))
        return body

    @staticmethod
    def _record(item: dict) -> Record:
        # expires_at is exact; an item written before it existed has only the number in expires.
        expires = (
            item.get("expires_at")
            if item.get("expires_at") is not None
            else _seconds(item.get("expires"))
        )
        return Record(
            item["kind"],
            item["key"],
            item.get("data") or {},
            expires,
            item.get("ix1"),
            item.get("ix2"),
            item.get("_etag"),
        )

    def _read(self, kind: str, key: str) -> Optional[Record]:
        try:
            return self._record(
                self._container.read_item(item=self._id(kind, key), partition_key=kind)
            )
        except Exception as exc:
            if _status(exc) == 404:
                return None
            raise

    def get(self, kind: str, key: str) -> Optional[Record]:
        record = self._read(kind, key)
        return record if record is not None and record.live() else None

    def create(self, record: Record) -> bool:
        try:
            item = self._container.create_item(body=self._body(record))
        except Exception as exc:
            if _status(exc) != 409:
                raise
            held = self._read(record.kind, record.key)
            if held is None or held.live():
                return False
            record.version = held.version
            return self.replace(record)
        record.version = item.get("_etag")
        return True

    def replace(self, record: Record) -> bool:
        try:
            item = self._container.replace_item(
                item=self._id(record.kind, record.key),
                body=self._body(record),
                etag=record.version,
                match_condition=_if_unchanged(),
            )
        except Exception as exc:
            if _status(exc) in (404, 412):
                return False
            raise
        record.version = item.get("_etag")
        return True

    def put(self, record: Record) -> None:
        record.version = self._container.upsert_item(body=self._body(record)).get("_etag")

    def delete(self, kind: str, key: str, version: Any = None) -> bool:
        condition = (
            {"etag": version, "match_condition": _if_unchanged()} if version is not None else {}
        )
        try:
            self._container.delete_item(item=self._id(kind, key), partition_key=kind, **condition)
        except Exception as exc:
            if _status(exc) in (404, 412):
                return False
            raise
        return True

    def query(
        self, kind: str, *, ix1: Optional[str] = None, ix2: Optional[str] = None
    ) -> list[Record]:
        clauses, parameters = ["c.kind = @kind"], [{"name": "@kind", "value": kind}]
        for name, value in (("ix1", ix1), ("ix2", ix2)):
            if value is not None:
                clauses.append(f"c.{name} = @{name}")
                parameters.append({"name": f"@{name}", "value": value})
        items = self._container.query_items(
            query="SELECT * FROM c WHERE " + " AND ".join(clauses),
            parameters=parameters,
            partition_key=kind,
        )
        now = time.time()
        return [r for r in (self._record(i) for i in items) if r.live(now)]

    def describe(self) -> str:
        return self._where


# ============================================================================
# AMAZON DYNAMODB
# ============================================================================
#
# INPUT   a table
# OUTPUT  kind and key as the table's keys, two sparse indexes, time to live
#         on expires_at; a version token no earlier write had
#
# For a server on AWS, the same.


def _code(exc: BaseException) -> Optional[str]:
    response = getattr(exc, "response", None) or {}
    return (response.get("Error") or {}).get("Code")


def _token() -> int:
    """A new version: any value no earlier write had, so a compare-and-swap can tell them apart."""
    return secrets.randbits(62)


class DynamoRecords(Records):
    """Amazon DynamoDB: kind and key as the table's keys, two sparse indexes, time to live on ``expires_at``."""

    def __init__(self, table: Any, *, where: str = "DynamoDB") -> None:
        self._table = table
        self._where = where

    @classmethod
    def open(cls, url: str, *, setting: str = SIGNIN_SETTING) -> "DynamoRecords":
        try:
            import boto3
        except ImportError as exc:
            raise DependencyError("boto3", "aws") from exc
        parts = urlsplit(url)
        name = parts.netloc or parts.path.strip("/")
        options = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if not name:
            raise ConfigurationError(
                f"For DynamoDB, {setting} reads dynamodb://<table>?region=<region>"
            )
        resource = boto3.resource(
            "dynamodb", region_name=options.get("region"), endpoint_url=options.get("endpoint")
        )
        table = resource.Table(name)
        try:
            table.load()
        except Exception as exc:
            if _code(exc) != "ResourceNotFoundException":
                raise
            table = cls._make(resource, name)
        return cls(table, where=describe_where(url))

    @staticmethod
    def _make(resource: Any, name: str) -> Any:
        try:
            table = resource.create_table(
                TableName=name,
                BillingMode="PAY_PER_REQUEST",
                KeySchema=[
                    {"AttributeName": "kind", "KeyType": "HASH"},
                    {"AttributeName": "key", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": a, "AttributeType": "S"} for a in ("kind", "key", "g1", "g2")
                ],
                GlobalSecondaryIndexes=[
                    {
                        "IndexName": index,
                        "KeySchema": [{"AttributeName": attribute, "KeyType": "HASH"}],
                        "Projection": {"ProjectionType": "ALL"},
                    }
                    for index, attribute in (("ix1", "g1"), ("ix2", "g2"))
                ],
            )
            table.wait_until_exists()
            resource.meta.client.update_time_to_live(
                TableName=name,
                TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"},
            )
            return table
        except Exception as refused:
            raise ConfigurationError(
                f"The DynamoDB table {name} is not there, and this server may not make it. Make it with the partition key kind and the "
                "sort key key (both strings), two indexes named ix1 and ix2 keyed on the strings g1 and g2 with every attribute "
                "projected, and time to live on expires_at, then start again"
            ) from refused

    @staticmethod
    def _item(record: Record, version: int) -> dict:
        # The data as JSON text: DynamoDB turns numbers into Decimals and refuses floats.
        item: dict = {
            "kind": record.kind,
            "key": record.key,
            "data": _dump(record.data),
            "ver": version,
        }
        if record.ix1 is not None:
            item.update(ix1=record.ix1, g1=f"{record.kind}|{record.ix1}")
        if record.ix2 is not None:
            item.update(ix2=record.ix2, g2=f"{record.kind}|{record.ix2}")
        if record.expires is not None:
            item["expires_at"] = math.ceil(record.expires)
        return item

    @staticmethod
    def _record(item: dict) -> Record:
        expires = item.get("expires_at")
        return Record(
            item["kind"],
            item["key"],
            json.loads(item["data"]),
            float(expires) if expires is not None else None,
            item.get("ix1"),
            item.get("ix2"),
            item.get("ver"),
        )

    def get(self, kind: str, key: str) -> Optional[Record]:
        item = self._table.get_item(Key={"kind": kind, "key": key}, ConsistentRead=True).get("Item")
        record = self._record(item) if item else None
        return record if record is not None and record.live() else None

    def _write(self, record: Record, **condition: Any) -> bool:
        version = _token()
        try:
            self._table.put_item(Item=self._item(record, version), **condition)
        except Exception as exc:
            if _code(exc) == "ConditionalCheckFailedException":
                return False
            raise
        record.version = version
        return True

    def create(self, record: Record) -> bool:
        return self._write(
            record,
            ConditionExpression="attribute_not_exists(#k) OR #e <= :now",
            ExpressionAttributeNames={"#k": "key", "#e": "expires_at"},
            ExpressionAttributeValues={":now": int(time.time())},
        )

    def replace(self, record: Record) -> bool:
        return self._write(
            record,
            ConditionExpression="#v = :v",
            ExpressionAttributeNames={"#v": "ver"},
            ExpressionAttributeValues={":v": record.version},
        )

    def put(self, record: Record) -> None:
        self._write(record)

    def delete(self, kind: str, key: str, version: Any = None) -> bool:
        condition: dict = {}
        if version is not None:
            condition = {
                "ConditionExpression": "#v = :v",
                "ExpressionAttributeNames": {"#v": "ver"},
                "ExpressionAttributeValues": {":v": version},
            }
        try:
            gone = self._table.delete_item(
                Key={"kind": kind, "key": key}, ReturnValues="ALL_OLD", **condition
            )
        except Exception as exc:
            if _code(exc) == "ConditionalCheckFailedException":
                return False
            raise
        return bool(gone.get("Attributes"))

    def query(
        self, kind: str, *, ix1: Optional[str] = None, ix2: Optional[str] = None
    ) -> list[Record]:
        if ix1 is not None:
            args: dict = {
                "IndexName": "ix1",
                "KeyConditionExpression": "#g = :g",
                "ExpressionAttributeNames": {"#g": "g1"},
                "ExpressionAttributeValues": {":g": f"{kind}|{ix1}"},
            }
        elif ix2 is not None:
            args = {
                "IndexName": "ix2",
                "KeyConditionExpression": "#g = :g",
                "ExpressionAttributeNames": {"#g": "g2"},
                "ExpressionAttributeValues": {":g": f"{kind}|{ix2}"},
            }
        else:
            args = {
                "KeyConditionExpression": "#p = :p",
                "ExpressionAttributeNames": {"#p": "kind"},
                "ExpressionAttributeValues": {":p": kind},
                "ConsistentRead": True,
            }
        items: list = []
        while True:
            page = self._table.query(**args)
            items.extend(page.get("Items", []))
            if not page.get("LastEvaluatedKey"):
                break
            args["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        now = time.time()
        found = (self._record(i) for i in items)
        return [
            r
            for r in found
            if r.live(now) and (ix1 is None or r.ix1 == ix1) and (ix2 is None or r.ix2 == ix2)
        ]

    def describe(self) -> str:
        return self._where


# ============================================================================
# CHOOSING
# ============================================================================
#
# INPUT   a path, an address, or records already open
# OUTPUT  where sign-in state is kept, in words fit for a banner, with nothing
#         opened and no password shown; the records at that place
#
# The setting decides; the code does not guess.


def _sqlite_path(url: str) -> str:
    if not url.lower().startswith("sqlite:///"):
        raise ConfigurationError(
            "A SQLite address reads sqlite:///relative/path.db or sqlite:////absolute/path.db"
        )
    return url[len("sqlite:///") :]


def describe_where(where: Any) -> str:
    """Where sign-in state is kept, in words fit for a banner or a log. Nothing is opened, and no password shown."""
    if isinstance(where, Records):
        return where.describe()
    text = str(where)
    if text == ":memory:":
        return "SQLite in memory"
    if "://" not in text:
        return f"SQLite file {text}"
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme == "sqlite":
        return f"SQLite file {_sqlite_path(text)}"
    host = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
    if scheme in ("postgres", "postgresql"):
        return f"PostgreSQL {host}{parts.path}"
    if scheme == "cosmos":
        return f"Cosmos DB {host}{parts.path}"
    if scheme == "dynamodb":
        region = parse_qs(parts.query).get("region", [""])[0]
        return f"DynamoDB table {parts.netloc or parts.path.strip('/')}" + (
            f" in {region}" if region else ""
        )
    return f"{scheme}:// (not a store this knows)"


def open_records(
    where: Any, *, key: Optional[str] = None, setting: str = SIGNIN_SETTING
) -> Records:
    """The records at ``where``: a path to a SQLite file, an address, or records already open.

    ``setting`` is the name the address was given under, for what an address
    that cannot be used is told: the sign-in store's unless said.
    """
    if isinstance(where, Records):
        return where
    text = str(where)
    if text == ":memory:":
        return SqlRecords.sqlite(":memory:")
    if "://" not in text:
        return SqlRecords.sqlite(Path(where))
    scheme = urlsplit(text).scheme.lower()
    if scheme == "sqlite":
        return SqlRecords.sqlite(Path(_sqlite_path(text)))
    if scheme in ("postgres", "postgresql"):
        return SqlRecords.postgres(text, password=key)
    if scheme == "cosmos":
        return CosmosRecords.open(text, key=key, setting=setting)
    if scheme == "dynamodb":
        return DynamoRecords.open(text, setting=setting)
    raise ConfigurationError(f"{_where(setting)}, not {scheme}://")
