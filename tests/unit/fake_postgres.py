"""An in-memory stand-in for the psycopg2 connection Lakebase and Aurora use.

Both LakebaseStorage and AuroraPostgreSQLStorage speak plain PostgreSQL plus
pgvector, so one fake serves both. LakebaseStorage opens its real connection
with cursor_factory=RealDictCursor and reads rows by column name
(row["col"]); AuroraPostgreSQLStorage never sets a cursor_factory and reads
rows positionally, zipping cur.description against the row tuple. Row below
answers to both: it supports row["col"] and row[i] alike, and iterates in
column order so dict(zip(cols, row)) also works, the way
AuroraPostgreSQLStorage.get/scan/iterate build their return value.

Neither backend has a client=/conn=/connection= constructor seam (see
__init__ on both classes in vectrixdb/core/storage.py): connect() always
does its own bare "import psycopg2; psycopg2.connect(...)", and psycopg2 is
not installed in this environment either. So connect() is monkeypatched on
the instance rather than exercised for real (see _lakebase_fake and
_aurora_fake in test_storage_contract.py); the replacement runs the same
bootstrap statements the real connect() runs, copied by hand because a
monkeypatched connect() cannot call the original it replaces. Every
statement after that, create_collection/insert/get/update/delete/scan/
vector_search/..., is the backend's real, unmodified SQL, executed here by
FakeCursor.

Statements execute for real against an in-memory sqlite3 connection, after
translating the PostgreSQL-isms the backends actually send:

- "%s" placeholders become sqlite3's "?".
- "col = ANY(%s)" becomes "col IN (?, ?, ...)", expanded to match however
  many values are bound to that one parameter (get_batch, delete_batch, the
  candidate lookup in ultimate_search).
- "NOW()" becomes CURRENT_TIMESTAMP.
- "::vector", "::jsonb" and other casts are dropped; sqlite3 has none.
- "JSONB" and "vector(N)" column types become TEXT for sqlite3's benefit,
  after being recorded in a Catalog (below) so fetches and inserts can still
  tell a JSONB column from a vector column from a plain one.
- Schema-qualified identifiers lose the schema: both the quoted form
  LakebaseStorage builds ('"schema"."table"' and '"schema".table) and the
  bare form AuroraPostgreSQLStorage._table_name builds (schema.table).
  sqlite3 has no schema namespace, and every statement either backend sends
  names at most one schema, so dropping it loses nothing.
- "DROP TABLE ... CASCADE" loses the CASCADE keyword sqlite3 does not have.
- A trailing semicolon on CREATE TABLE is tolerated. Neither storage
  backend sends one, but PostgresAuditSink ships its DDL as a script for a
  DBA to run, and its test executes that script verbatim rather than a
  doctored copy of it.
- "CREATE EXTENSION ..." and "CREATE SCHEMA ..." become no-ops.
- "SET ..." and "RESET ..." become no-ops and are appended to
  FakeConnection.statements. sqlite3 has no roles or session settings, so
  what a test of SET LOCAL ROLE can check is that the statement was issued,
  inside a transaction, and in the right order relative to the query; the
  role actually being enforced is PostgreSQL's job and needs a live one.
- A "CREATE INDEX ... USING hnsw (...)" or "... USING gin (...)" also
  becomes a no-op. On a real, correctly provisioned pgvector database these
  succeed; sqlite3 cannot run them at all. LakebaseStorage already wraps its
  own equivalent calls in try/except for exactly this kind of failure, so a
  no-op here reproduces that tolerance. AuroraPostgreSQLStorage does not
  wrap its calls the same way, so without this no-op every
  AuroraPostgreSQLStorage.create_collection would raise here regardless of
  whether the collection's data was ever touched; that asymmetry with
  Lakebase's defensive handling is worth a note, but it is not a
  contract-breaking bug the way the ones in the xfail tests are, so it is
  not one of them.
- information_schema.columns, which LakebaseStorage queries to discover
  which optional columns a collection table already has, is answered from
  the same Catalog used for JSONB decoding, rather than attempted against
  sqlite3 (which has no such view).

Nothing else is translated; this is a translator for the statements these
two backends actually send, not a general PostgreSQL-on-sqlite3 emulator.

pgvector's "<=>" cosine-distance operator has no sqlite3 equivalent. A query
that uses it (LakebaseStorage.vector_search, the dense phase of
LakebaseStorage.hybrid_search, and AuroraPostgreSQLStorage.vector_search) is
intercepted before any of the translation above: the candidate rows are
pulled out with a plain SELECT (run back through this same translator),
cosine distance is computed in Python against the query vector, and the
rows are ranked and limited here before being handed back shaped like the
original query's own SELECT list. That is the "rank in Python" this module
promises, not a sqlite3 extension function.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


class Row:
    """One result row, addressable by position or by column name.

    Real psycopg2 gives LakebaseStorage dict-shaped rows (RealDictCursor)
    and AuroraPostgreSQLStorage plain tuples read via cur.description. One
    row type answers to both, and iterates in column order so
    dict(zip(cols, row)) also works the way AuroraPostgreSQLStorage uses it.
    """

    __slots__ = ("_columns", "_values")

    def __init__(self, columns: Sequence[str], values: Sequence[Any]) -> None:
        self._columns = tuple(columns)
        self._values = tuple(values)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            try:
                return self._values[self._columns.index(key)]
            except ValueError:
                raise KeyError(key) from None
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"Row({dict(zip(self._columns, self._values))!r})"


class Catalog:
    """Per-table column kind ("jsonb", "vector", or None), by bare table name.

    Populated from the CREATE TABLE / ALTER TABLE ADD COLUMN statements the
    backends send. Used two ways: a JSONB column is decoded from stored text
    back to a Python object on fetch, the way psycopg2's default JSONB
    typecaster does for both backends regardless of cursor_factory; a vector
    column's insert parameter is formatted from a raw list into pgvector's
    "[v1,v2,...]" text if it did not arrive already formatted that way (see
    _normalize_insert_params).
    """

    def __init__(self) -> None:
        self._columns: Dict[str, Dict[str, Optional[str]]] = {}

    def note_columns(self, table: str, columns: Iterable[Tuple[str, Optional[str]]]) -> None:
        self._columns.setdefault(table, {}).update(dict(columns))

    def note_column(self, table: str, name: str, kind: Optional[str]) -> None:
        self._columns.setdefault(table, {})[name] = kind

    def columns_of(self, table: str) -> List[str]:
        return list(self._columns.get(table, {}))

    def kind_of(self, table: str, column: str) -> Optional[str]:
        return self._columns.get(table, {}).get(column)


# -- parsing CREATE TABLE / ALTER TABLE, to fill in the Catalog -------------


def _split_top_level(text: str) -> List[str]:
    """Split on commas that are not inside parentheses."""

    parts: List[str] = []
    depth = 0
    current: List[str] = []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def _bare_name(qualified: str) -> str:
    """Drop schema qualification and quoting, keeping just the table name."""

    tail = qualified.strip().split(".")[-1]
    return tail.strip('"')


def _column_kind(type_text: str) -> Optional[str]:
    low = type_text.lower()
    if "jsonb" in low or "json" in low:
        return "jsonb"
    if "vector" in low:
        return "vector"
    return None


_CREATE_TABLE_RE = re.compile(
    r"create\s+table\s*(?:if\s+not\s+exists\s*)?([\w.\"]+)\s*\((.*)\)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_ALTER_ADD_COLUMN_RE = re.compile(
    r"alter\s+table\s+([\w.\"]+)\s+add\s+column\s+(\w+)\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)


def _note_ddl(catalog: Catalog, sql: str) -> None:
    """Record a CREATE TABLE's or ALTER TABLE ADD COLUMN's columns and kinds.

    Must run before the JSONB/vector -> TEXT rewrite in _translate, so it
    still sees the real type names. A no-op for every other statement.
    """

    stripped = sql.strip()
    m = _CREATE_TABLE_RE.match(stripped)
    if m:
        table = _bare_name(m.group(1))
        cols = []
        for part in _split_top_level(m.group(2)):
            part = part.strip()
            if not part:
                continue
            tokens = part.split(None, 1)
            name = tokens[0].strip('"')
            rest = tokens[1] if len(tokens) > 1 else ""
            cols.append((name, _column_kind(rest)))
        catalog.note_columns(table, cols)
        return
    m = _ALTER_ADD_COLUMN_RE.match(stripped)
    if m:
        table = _bare_name(m.group(1))
        catalog.note_column(table, m.group(2), _column_kind(m.group(3)))


# -- statement translation ---------------------------------------------------

_QUOTED_SCHEMA_RE = re.compile(r'"[A-Za-z_]\w*"\.')
_UNQUOTED_SCHEMA_REF_RE = re.compile(
    r"(?i)\b(from|into|update|table)\b(\s+if(?:\s+not)?\s+exists)?\s+(\w+)\.(\w+)\b"
)
_CAST_RE = re.compile(r"::\w+")
_NOW_RE = re.compile(r"(?i)now\s*\(\s*\)")
_CASCADE_RE = re.compile(r"(?i)\bcascade\b")
_JSONB_TYPE_RE = re.compile(r"(?i)\bjsonb\b")
_VECTOR_TYPE_RE = re.compile(r"(?i)\bvector\s*\(\s*\d+\s*\)")


def _strip_schema(sql: str) -> str:
    out = _QUOTED_SCHEMA_RE.sub("", sql)

    def repl(m: "re.Match[str]") -> str:
        keyword, if_clause, _schema, table = m.groups()
        return f"{keyword}{if_clause or ''} {table}"

    return _UNQUOTED_SCHEMA_REF_RE.sub(repl, out)


def _translate(sql: str) -> str:
    out = _strip_schema(sql)
    out = _NOW_RE.sub("CURRENT_TIMESTAMP", out)
    out = _CAST_RE.sub("", out)
    out = _CASCADE_RE.sub("", out)
    out = _JSONB_TYPE_RE.sub("TEXT", out)
    out = _VECTOR_TYPE_RE.sub("TEXT", out)
    out = out.replace("%s", "?")
    return out


_ANY_RE = re.compile(r"=\s*ANY\(%s\)")


def _expand_any(sql: str, params: Tuple[Any, ...]) -> Tuple[str, List[Any]]:
    """Rewrite one "... = ANY(%s)" into "... IN (?, ?, ...)".

    Both backends use ANY(%s) exactly this way (get_batch, delete_batch, the
    candidate lookup in ultimate_search): one array parameter, matched
    positionally like every other placeholder. The "=" has to go together
    with "ANY(%s)": sqlite3 has no ANY(), and leaving the "=" in place turns
    "col = (?, ?, ?)" into a row-value comparison, which sqlite3 rejects
    with "row value misused" instead of matching anything.
    """

    m = _ANY_RE.search(sql)
    if not m:
        return sql, list(params)
    idx = sql.count("%s", 0, m.start())
    values = list(params[idx])
    placeholders = ", ".join(["?"] * len(values)) if values else "NULL"
    new_sql = sql[: m.start()] + f"IN ({placeholders})" + sql[m.end() :]
    new_params = list(params[:idx]) + values + list(params[idx + 1 :])
    return new_sql, new_params


_INSERT_RE = re.compile(
    r"insert\s+into\s+([\w.\"]+)\s*\((.*?)\)\s*values\s*\((.*?)\)",
    re.IGNORECASE | re.DOTALL,
)
_INSERT_INTO_RE = re.compile(r"(?i)insert\s+into\s+([\w.\"]+)")


def _format_vector(values: Sequence[Any]) -> str:
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def _parse_vector(value: Any) -> List[float]:
    if isinstance(value, (list, tuple)):
        return [float(v) for v in value]
    text = str(value).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [float(v) for v in text.split(",") if v.strip()]


def _normalize_insert_params(
    sql: str, params: Sequence[Any], catalog: Catalog, table: str
) -> List[Any]:
    """Format a raw vector parameter the way pgvector's adapter would.

    LakebaseStorage always pre-formats a dense embedding into pgvector's
    "[v1,v2,...]" text itself before binding it (see LakebaseStorage.insert
    in vectrixdb/core/storage.py), so this is a no-op there. Aurora binds
    the raw Python list directly (AuroraPostgreSQLStorage.insert), relying
    on psycopg2/pgvector to adapt it; this fake has neither, so the column
    catalog built from the table's own CREATE TABLE is used to spot a
    vector-typed column bound to a list and format it the same way.
    """

    m = _INSERT_RE.search(sql)
    if not m:
        return list(params)
    columns = [c.strip().strip('"') for c in m.group(2).split(",")]
    placeholder_count = m.group(3).count("%s")
    out = list(params)
    for i, col in enumerate(columns[:placeholder_count]):
        if i >= len(out):
            break
        if catalog.kind_of(table, col) == "vector" and isinstance(out[i], (list, tuple)):
            out[i] = _format_vector(out[i])
    return out


def _cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a)) or 1.0
    norm_b = math.sqrt(sum(x * x for x in b)) or 1.0
    return 1.0 - dot / (norm_a * norm_b)


_VECTOR_QUERY_RE = re.compile(
    r"select\s+(?P<cols>.+?)\s+from\s+(?P<table>[\w.\"]+)\s*"
    r"(?:where\s+(?P<where>.+?)\s+)?"
    r"order\s+by\s+\w+\s+limit\s+%s\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _split_select_columns(cols_raw: str) -> List[Tuple[str, bool]]:
    """[(output_name, is_distance_expression), ...] in SELECT-list order."""

    out = []
    for part in _split_top_level(cols_raw):
        part = part.strip()
        if "<=>" in part:
            m = re.search(r"(?i)\bas\s+(\w+)\s*$", part)
            out.append((m.group(1) if m else "distance", True))
        else:
            out.append((part.strip('"'), False))
    return out


_FROM_RE = re.compile(r"(?i)\bfrom\s+([\w.\"]+)")


def _first_table(sql: str) -> Optional[str]:
    m = _FROM_RE.search(sql)
    return _bare_name(m.group(1)) if m else None


class FakeCursor:
    """Enough of a psycopg2 cursor for Lakebase/AuroraPostgreSQLStorage.

    Both use "with self._conn.cursor() as cur:" throughout, so this supports
    the context manager protocol the way a real psycopg2 cursor does.
    """

    def __init__(self, connection: "FakeConnection") -> None:
        self._conn = connection
        self._real = connection.sqlite.cursor()
        self._rows: List[Row] = []
        self._index = 0
        self.description: Optional[List[Tuple[str, ...]]] = None
        self.rowcount: int = -1

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def close(self) -> None:
        self._real.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> "FakeCursor":
        params = tuple(params) if params is not None else ()
        stripped = sql.strip()
        upper = stripped.upper()

        if "INFORMATION_SCHEMA.COLUMNS" in upper:
            _schema, table = params
            names = self._conn.catalog.columns_of(_bare_name(table))
            self._rows = [Row(["column_name"], [name]) for name in names]
            self.description = [("column_name",)]
            self.rowcount = len(self._rows)
            self._index = 0
            return self

        if upper.startswith(("SET ", "RESET ", "BEGIN", "ROLLBACK", "COMMIT")):
            # SET LOCAL ROLE and friends. sqlite3 has no roles and no session
            # settings, so these are recorded and skipped: what the tests
            # need is that the statement was issued, in a transaction, and in
            # the right order, which self.statements preserves.
            self._conn.statements.append(stripped)
            self._rows = []
            self.description = None
            self.rowcount = -1
            return self

        if upper.startswith("CREATE EXTENSION") or upper.startswith("CREATE SCHEMA"):
            self._rows = []
            self.description = None
            self.rowcount = -1
            return self

        if re.search(r"(?i)using\s+(hnsw|gin)\b", stripped):
            # Both backends' HNSW/GIN index creation. See the module
            # docstring for why this is a no-op rather than a translation.
            self._rows = []
            self.description = None
            self.rowcount = -1
            return self

        if "<=>" in stripped:
            return self._execute_vector_query(stripped, params)

        sql2, params2 = _expand_any(stripped, params)
        _note_ddl(self._conn.catalog, sql2)

        if upper.startswith("INSERT INTO"):
            table = _bare_name(_INSERT_INTO_RE.match(sql2).group(1))
            params2 = _normalize_insert_params(sql2, params2, self._conn.catalog, table)

        translated = _translate(sql2)
        self._real.execute(translated, params2)

        if self._real.description:
            names = [d[0] for d in self._real.description]
            table = _first_table(translated)
            rows = []
            for raw in self._real.fetchall():
                values = list(raw)
                if table:
                    for i, name in enumerate(names):
                        if self._conn.catalog.kind_of(table, name) == "jsonb" and isinstance(
                            values[i], str
                        ):
                            values[i] = json.loads(values[i])
                rows.append(Row(names, values))
            self._rows = rows
            self.description = [(n,) for n in names]
        else:
            self._rows = []
            self.description = None
        self._index = 0
        self.rowcount = self._real.rowcount
        return self

    def _execute_vector_query(self, sql: str, params: Tuple[Any, ...]) -> "FakeCursor":
        """Answer a "... <=> %s::vector AS distance ..." query.

        Pulls the candidate rows out with a plain SELECT (run back through
        execute(), so JSONB columns come back decoded exactly as they would
        for any other query), ranks them in Python by cosine distance
        against the query vector, and reshapes the winners into the same
        column order the original SELECT list asked for.
        """

        m = _VECTOR_QUERY_RE.match(sql)
        if not m:
            raise ValueError(f"fake postgres cannot evaluate vector query: {sql!r}")
        table = _bare_name(m.group("table"))
        columns = _split_select_columns(m.group("cols"))
        where_sql = m.group("where")
        query_vector = [float(x) for x in params[0]]
        limit = int(params[-1])

        plain_cols = [name for name, is_distance in columns if not is_distance]
        inner_sql = f"SELECT {', '.join(plain_cols + ['dense_embedding'])} FROM {table}"
        if where_sql:
            inner_sql += f" WHERE {where_sql}"

        self.execute(inner_sql)
        candidates = self.fetchall()

        ranked = []
        for row in candidates:
            vector_text = row["dense_embedding"]
            if vector_text is None:
                continue
            distance = _cosine_distance(query_vector, _parse_vector(vector_text))
            ranked.append((distance, row))
        ranked.sort(key=lambda pair: pair[0])
        ranked = ranked[:limit]

        rows = []
        for distance, row in ranked:
            row_values = [distance if is_distance else row[name] for name, is_distance in columns]
            rows.append(Row([name for name, _ in columns], row_values))

        self._rows = rows
        self._index = 0
        self.description = [(name,) for name, _ in columns]
        self.rowcount = len(rows)
        return self

    def fetchone(self) -> Optional[Row]:
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchall(self) -> List[Row]:
        rows = self._rows[self._index :]
        self._index = len(self._rows)
        return rows

    def __iter__(self):
        return iter(self._rows[self._index :])


class FakeConnection:
    """Stands in for what psycopg2.connect(...) returns.

    One in-memory sqlite3 connection per fake, matching one real PostgreSQL
    connection per backend instance. autocommit is a plain attribute:
    LakebaseStorage sets it False and calls commit() itself after every
    statement; AuroraPostgreSQLStorage sets it True and its flush() checks
    the attribute to decide whether it still needs to call commit(). Either
    way there is nothing to buffer here, since every statement already runs
    against sqlite3 immediately.
    """

    def __init__(self) -> None:
        self.sqlite = sqlite3.connect(":memory:")
        self.catalog = Catalog()
        self.autocommit = False
        self.closed = False
        #: Statements sqlite3 cannot run, kept in order so a test can see
        #: that SET LOCAL ROLE was issued, inside a transaction, before the
        #: query. Whether the role is then enforced is PostgreSQL's job.
        self.statements: List[str] = []

    def cursor(self, *_args: Any, **_kwargs: Any) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.sqlite.commit()

    def rollback(self) -> None:
        self.sqlite.rollback()

    def close(self) -> None:
        self.sqlite.close()
        self.closed = True
