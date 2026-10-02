"""Stand-ins for the databases sign-in state can live in, so the record layer is tested without any of them.

* ``FakePostgres``: connections shaped like psycopg2's, taking ``%s`` and
  answering from one SQLite database, so the statements the PostgreSQL store
  sends are actually run. It can drop a connection, as a restarted server does.
* ``FakeContainer``: the calls the Cosmos DB store makes of a container, with an
  ETag that changes on every write and the status codes the SDK raises. An ETag
  sent without a match condition fails the test: the SDK would ignore it.
* ``FakeTable``: the calls the DynamoDB store makes of a table. Its condition
  expressions are checked, a float is refused as DynamoDB refuses one, an index
  cannot be read consistently, and every query comes back in pages of two.
"""

from __future__ import annotations

import copy
import re
import sqlite3
import uuid


class OperationalError(Exception):
    """Named as psycopg2's is: how the store tells a lost connection from a bad statement."""


class ProgrammingError(Exception):
    """What psycopg2 raises for a statement the database refuses, a missing table among them."""


class FakePostgres:
    def __init__(self):
        self.db = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        self.opened: list = []
        self.connections: list = []
        #: How many statements from now on fail as if the server had gone away.
        self.fail_next = 0

    def connect(self, dsn, **options):
        self.opened.append((dsn, options))
        connection = _Connection(self)
        self.connections.append(connection)
        return connection


class _Connection:
    def __init__(self, server: FakePostgres):
        self.server = server
        self.autocommit = False
        self.statements: list = []
        self.closed = False

    def cursor(self):
        return _Cursor(self)

    def close(self):
        self.closed = True


class _Cursor:
    def __init__(self, connection: _Connection):
        self.connection = connection
        self.rowcount = -1
        self._rows: list = []

    def execute(self, sql, params=()):
        assert "?" not in sql, "psycopg2 takes %s, not ?"
        assert self.connection.autocommit, "every statement stands alone: autocommit is on"
        server = self.connection.server
        if self.connection.closed:
            raise OperationalError("connection already closed")
        if server.fail_next:
            server.fail_next -= 1
            raise OperationalError("server closed the connection unexpectedly")
        self.connection.statements.append(sql)
        try:
            cursor = server.db.execute(sql.replace("%s", "?"), params)
        except sqlite3.Error as exc:
            # SQLite calls a missing table an OperationalError, which psycopg2 keeps for a lost connection.
            raise ProgrammingError(str(exc)) from exc
        self._rows = cursor.fetchall() if cursor.description else []
        self.rowcount = cursor.rowcount

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class CosmosError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class FakeContainer:
    def __init__(self):
        self.items: dict = {}

    def _stored(self, body: dict) -> dict:
        assert not re.search(r"[/\\?#]", body["id"]), "an id may not hold / \\ ? or #"
        if body.get("expires") is not None:
            assert isinstance(body.get("ttl"), int) and body["ttl"] >= 1, "an item that expires carries its own time to live"
        else:
            assert "ttl" not in body
        item = copy.deepcopy(body)
        item["_etag"] = uuid.uuid4().hex
        self.items[(body["kind"], body["id"])] = item
        return copy.deepcopy(item)

    @staticmethod
    def _unchanged(found: dict, etag, match_condition) -> None:
        if etag is not None:
            assert match_condition is not None, "the SDK ignores an etag sent without a match condition"
            if found["_etag"] != etag:
                raise CosmosError(412)

    def read_item(self, item, partition_key):
        found = self.items.get((partition_key, item))
        if found is None:
            raise CosmosError(404)
        return copy.deepcopy(found)

    def create_item(self, body):
        if (body["kind"], body["id"]) in self.items:
            raise CosmosError(409)
        return self._stored(body)

    def replace_item(self, item, body, etag=None, match_condition=None):
        assert item == body["id"]
        found = self.items.get((body["kind"], item))
        if found is None:
            raise CosmosError(404)
        self._unchanged(found, etag, match_condition)
        return self._stored(body)

    def upsert_item(self, body):
        return self._stored(body)

    def delete_item(self, item, partition_key, etag=None, match_condition=None):
        found = self.items.get((partition_key, item))
        if found is None:
            raise CosmosError(404)
        self._unchanged(found, etag, match_condition)
        del self.items[(partition_key, item)]

    def query_items(self, query, parameters, partition_key):
        assert re.fullmatch(r"SELECT \* FROM c WHERE c\.kind = @kind( AND c\.ix[12] = @ix[12])*", query), query
        values = {p["name"]: p["value"] for p in parameters}
        wanted = re.findall(r"c\.(\w+) = (@\w+)", query)
        return [
            copy.deepcopy(item)
            for (kind, _), item in self.items.items()
            if kind == partition_key and all(item.get(name) == values[value] for name, value in wanted)
        ]


class DynamoError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": code}}


class FakeTable:
    PAGE = 2

    def __init__(self):
        self.items: dict = {}

    @staticmethod
    def _holds(expression, names, values, old) -> bool:
        if expression == "attribute_not_exists(#k) OR #e <= :now":
            assert names == {"#k": "key", "#e": "expires_at"}
            return old is None or (old.get("expires_at") is not None and old["expires_at"] <= values[":now"])
        if expression == "#v = :v":
            assert names == {"#v": "ver"}
            return old is not None and old.get("ver") == values[":v"]
        raise AssertionError(f"the fake does not know {expression!r}")

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803 - boto3's names
        assert ConsistentRead, "a read straight after a write must see it"
        found = self.items.get((Key["kind"], Key["key"]))
        return {"Item": copy.deepcopy(found)} if found else {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeNames=None, ExpressionAttributeValues=None):  # noqa: N803
        for name, value in Item.items():
            assert not isinstance(value, float), f"DynamoDB refuses a float: {name}"
        old = self.items.get((Item["kind"], Item["key"]))
        if ConditionExpression and not self._holds(ConditionExpression, ExpressionAttributeNames or {}, ExpressionAttributeValues or {}, old):
            raise DynamoError("ConditionalCheckFailedException")
        self.items[(Item["kind"], Item["key"])] = copy.deepcopy(Item)
        return {}

    def delete_item(self, Key, ReturnValues=None, ConditionExpression=None, ExpressionAttributeNames=None, ExpressionAttributeValues=None):  # noqa: N803
        at = (Key["kind"], Key["key"])
        old = self.items.get(at)
        if ConditionExpression and not self._holds(ConditionExpression, ExpressionAttributeNames or {}, ExpressionAttributeValues or {}, old):
            raise DynamoError("ConditionalCheckFailedException")
        self.items.pop(at, None)
        return {"Attributes": copy.deepcopy(old)} if old is not None and ReturnValues == "ALL_OLD" else {}

    def query(self, KeyConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues, IndexName=None,  # noqa: N803
              ConsistentRead=False, ExclusiveStartKey=None):
        name, value = KeyConditionExpression.split(" = ")
        attribute = ExpressionAttributeNames[name]
        if IndexName is None:
            assert attribute == "kind"
        else:
            assert not ConsistentRead, "an index cannot be read consistently"
            assert {"ix1": "g1", "ix2": "g2"}[IndexName] == attribute
        matched = sorted((i for i in self.items.values() if i.get(attribute) == ExpressionAttributeValues[value]), key=lambda i: (i["kind"], i["key"]))
        start = 0
        if ExclusiveStartKey:
            start = 1 + next(n for n, i in enumerate(matched) if (i["kind"], i["key"]) == (ExclusiveStartKey["kind"], ExclusiveStartKey["key"]))
        page = matched[start:start + self.PAGE]
        answer: dict = {"Items": copy.deepcopy(page)}
        if start + self.PAGE < len(matched):
            answer["LastEvaluatedKey"] = {"kind": page[-1]["kind"], "key": page[-1]["key"]}
        return answer
