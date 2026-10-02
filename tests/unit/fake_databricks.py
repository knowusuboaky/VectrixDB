"""An in-memory stand-in for the databricks-sql-connector DB-API connection.

It implements the fake connection and cursor that ``DeltaLakeStorage`` needs.
Statements execute for real against an in-memory ``sqlite3`` database, after
translating the handful of Databricks-isms the backend relies on that SQLite
does not understand:

- Three-part backtick-quoted identifiers (```` `catalog`.`schema`.`table` ````)
  collapse to one double-quoted SQLite identifier. Catalog and schema are the
  same for every statement on one fake connection, so only the table name
  needs to survive.
- ``CREATE SCHEMA IF NOT EXISTS ...`` is a no-op: SQLite has no schema
  namespace to create.
- ``CREATE TABLE ... USING DELTA`` loses the ``USING DELTA`` clause, and the
  Databricks column types the backend declares (``STRING``, ``TIMESTAMP``,
  ``ARRAY<DOUBLE>``) become ``TEXT``. A dense embedding therefore round-trips
  as JSON-encoded text instead of a native array; the cursor decodes it back
  to a list on the way out, because that column is the one place the backend
  assumes the driver already handed it an array (see ``_row_to_data`` in
  ``vectrixdb/core/storage.py``).
- ``ARRAY(1.0, 2.0, ...)`` embedding literals are handled by registering a
  Python function named ``ARRAY`` on the connection, so the expression
  evaluates instead of needing text surgery.
- ``DESCRIBE TABLE`` becomes ``PRAGMA table_info``, reshaped so the first
  element of every row is the column name, which is all the backend reads.
- ``MERGE INTO ... WHEN MATCHED THEN UPDATE SET ... WHEN NOT MATCHED THEN
  INSERT ...`` has no SQLite equivalent, so it is split into an ``UPDATE``
  followed by an ``INSERT`` that only runs when that ``UPDATE`` matched no
  rows. Every MERGE this backend sends keys on exactly one column matched
  between ``target`` and ``source``, which is what makes the split
  mechanical rather than a general SQL rewrite.
- ``:name`` parameter binding needs no translation: it is also how the
  stdlib ``sqlite3`` module binds named parameters.
- The change data feed. Every table has a version, bumped once per
  ``INSERT``, ``UPDATE``, ``DELETE`` or ``MERGE`` that names it, and reset by
  ``DROP TABLE``. A table created with ``TBLPROPERTIES
  (delta.enableChangeDataFeed = true)``, or altered to have it, gets a
  ``__cdf_<table>`` log filled by SQLite triggers with the row, its
  ``_change_type`` (``insert``, ``update_preimage``, ``update_postimage``,
  ``delete``) and ``_commit_version``, as Delta records them.
  ``DESCRIBE HISTORY ... LIMIT 1`` answers the latest version, and
  ``table_changes(:table, :start, :end)`` reads the log, refusing a start
  before the feed was on, as Delta does.

The backend never asks the database to rank vectors: ``vector_search`` and
its callers already pull every row out with a plain SELECT and score them in
Python (see ``DeltaLakeStorage.vector_search`` in
``vectrixdb/core/storage.py``, which iterates the whole collection and does
the cosine math itself). There is no SQL-side similarity expression for this
fake to imitate, so none is implemented here.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

_IDENT_CHAIN = re.compile(r"`(?:[^`]|``)*`(?:\s*\.\s*`(?:[^`]|``)*`)*")

#: The one column the backend expects back as a native list rather than the
#: text SQLite actually stores (see the module docstring).
_ARRAY_COLUMN = "dense_embedding"


def _flatten_identifiers(sql: str) -> str:
    """Collapse a chain of backtick-quoted, dot-joined identifiers to one.

    ```` `main`.`t`.`contract` ```` becomes ``"contract"``. Every fake
    connection is scoped to a single (catalog, schema) pair for its whole
    life, so dropping both and keeping only the last segment (the table, or
    the schema in the one two-part case from ``CREATE SCHEMA``, which is
    discarded outright anyway) loses nothing a single SQLite database would
    have used.
    """

    def repl(match: "re.Match[str]") -> str:
        segments = re.findall(r"`((?:[^`]|``)*)`", match.group(0))
        last = segments[-1].replace("``", "`")
        return '"' + last.replace('"', '""') + '"'

    return _IDENT_CHAIN.sub(repl, sql)


_CDF_PROPERTY = re.compile(
    r"\bTBLPROPERTIES\s*\(\s*delta\.enableChangeDataFeed\s*=\s*true\s*\)", re.IGNORECASE
)
_MUTATION = re.compile(
    r'^(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO)\s+"((?:[^"]|"")*)"', re.IGNORECASE
)
_TABLE_CHANGES = re.compile(
    r"table_changes\(\s*:(\w+)\s*,\s*:(\w+)\s*,\s*:(\w+)\s*\)", re.IGNORECASE
)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _translate_tokens(sql: str) -> str:
    """Rewrite the Databricks type names and clauses SQLite does not have."""

    sql = sql.replace("ARRAY<DOUBLE>", "TEXT")
    sql = re.sub(r"\bSTRING\b", "TEXT", sql)
    sql = re.sub(r"\bTIMESTAMP\b", "TEXT", sql)
    sql = re.sub(r"\bUSING\s+DELTA\b", "", sql, flags=re.IGNORECASE)
    return sql


def _array_literal_fn(*values: float) -> str:
    """Backs the ``ARRAY(...)`` literal the backend renders for embeddings.

    Real Delta Lake stores an ``ARRAY<DOUBLE>``; SQLite has no array type, so
    the column is TEXT here and this stores the JSON encoding of the values.
    """

    return json.dumps([float(v) for v in values])


class FakeDatabricksCursor:
    """Enough of a ``databricks.sql`` cursor for ``DeltaLakeStorage``, in memory."""

    def __init__(self, sqlite_conn: sqlite3.Connection) -> None:
        self._raw = sqlite_conn.cursor()
        self.description: Optional[List[Tuple[Any, ...]]] = None
        self.rowcount: int = -1
        self._synthetic_rows: Optional[List[Tuple[Any, ...]]] = None

    # -- statement execution --------------------------------------------

    def execute(self, sql: str, params: Optional[Dict[str, Any]] = None) -> "FakeDatabricksCursor":
        with_feed = bool(_CDF_PROPERTY.search(sql))
        sql = _CDF_PROPERTY.sub("", sql)
        sql = _translate_tokens(sql)
        sql = _flatten_identifiers(sql).strip()
        self._synthetic_rows = None

        upper = sql.upper()
        if upper.startswith("ALTER TABLE") and with_feed:
            table = re.match(r'ALTER\s+TABLE\s+"((?:[^"]|"")*)"', sql, re.IGNORECASE).group(1)
            self._bump(table)
            self._install_feed(table)
            self.description = None
            return self

        if upper.startswith("DESCRIBE HISTORY"):
            table = re.match(r'DESCRIBE\s+HISTORY\s+"((?:[^"]|"")*)"', sql, re.IGNORECASE).group(1)
            self._raw.execute("SELECT version FROM __delta_log WHERE tbl = ?", (table,))
            found = self._raw.fetchone()
            if found is None:
                raise sqlite3.OperationalError(f"no such table: {table}")
            self._synthetic_rows = [(found[0],)]
            self.description = [("version",)]
            return self

        if _TABLE_CHANGES.search(sql):
            sql = self._table_changes(sql, params or {})

        mutated = _MUTATION.match(sql)
        if mutated:
            self._bump(mutated.group(1).replace('""', '"'))

        if upper.startswith("DROP TABLE"):
            table = sql.rstrip().split()[-1].strip('"').replace('""', '"')
            self._raw.execute(f"DROP TABLE IF EXISTS {_quote('__cdf_' + table)}")
            self._raw.execute("DELETE FROM __delta_log WHERE tbl = ?", (table,))

        if upper.startswith("CREATE TABLE"):
            table = re.match(
                r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"((?:[^"]|"")*)"', sql, re.IGNORECASE
            ).group(1)
            self._raw.execute(sql)
            self._raw.execute(
                "INSERT OR IGNORE INTO __delta_log (tbl, version, cdf_from) VALUES (?, 0, ?)",
                (table, 0 if with_feed else None),
            )
            if with_feed:
                self._install_feed(table)
            self.description = None
            self.rowcount = -1
            return self

        if upper.startswith("CREATE SCHEMA"):
            # SQLite has no schema/catalog namespace to create; the fake
            # connection is already scoped to one (catalog, schema) pair.
            self.description = None
            self.rowcount = -1
            return self

        if upper.startswith("DESCRIBE TABLE"):
            self._execute_describe(sql)
            return self

        if upper.startswith("MERGE INTO"):
            self._execute_merge(sql, params or {})
            return self

        if params:
            self._raw.execute(sql, params)
        else:
            self._raw.execute(sql)
        self.description = self._raw.description
        self.rowcount = self._raw.rowcount
        return self

    def _execute_describe(self, sql: str) -> None:
        table_token = sql[len("DESCRIBE TABLE") :].strip()
        self._raw.execute(f"PRAGMA table_info({table_token})")
        columns = self._raw.fetchall()
        # (col_name, data_type, comment): the backend only ever reads row[0],
        # the other two positions exist so this looks like a real DESCRIBE
        # TABLE result. A table that does not exist yields zero rows here,
        # exactly like the try/except in _ensure_collection_table expects.
        self._synthetic_rows = [(col[1], col[2], None) for col in columns]
        self.description = [("col_name",), ("data_type",), ("comment",)]
        self.rowcount = len(self._synthetic_rows)

    def _execute_merge(self, sql: str, params: Dict[str, Any]) -> None:
        """Split one single-key MERGE into an UPDATE and a conditional INSERT.

        Every MERGE ``DeltaLakeStorage`` sends has the same shape: one column
        matched between ``target`` and ``source``, an unconditional
        ``UPDATE ... SET`` list for the matched case, and an ``INSERT`` for
        the not-matched case, both drawing on the same bound parameters. That
        regularity is what makes the split mechanical instead of a real MERGE
        implementation.
        """

        m = re.match(r"MERGE\s+INTO\s+(\S+)\s+AS\s+target\s+USING", sql, re.IGNORECASE)
        if not m:
            raise ValueError(f"fake Databricks cursor cannot parse MERGE: {sql[:200]!r}")
        table = m.group(1)

        key_m = re.search(r"\bON\s+target\.(\w+)\s*=\s*source\.\w+", sql, re.IGNORECASE)
        if not key_m:
            raise ValueError(f"fake Databricks cursor cannot find the MERGE key: {sql[:200]!r}")
        key_col = key_m.group(1)

        set_m = re.search(
            r"WHEN\s+MATCHED\s+THEN\s+UPDATE\s+SET\s+(.*?)\s+WHEN\s+NOT\s+MATCHED",
            sql,
            re.IGNORECASE | re.DOTALL,
        )
        if not set_m:
            raise ValueError(f"fake Databricks cursor cannot find UPDATE SET: {sql[:200]!r}")
        set_clause = set_m.group(1)

        insert_m = re.search(
            r"WHEN\s+NOT\s+MATCHED\s+THEN\s+INSERT\s*\((.*?)\)\s*VALUES\s*\((.*)\)\s*\Z",
            sql,
            re.IGNORECASE | re.DOTALL,
        )
        if not insert_m:
            raise ValueError(f"fake Databricks cursor cannot find INSERT/VALUES: {sql[:200]!r}")
        insert_cols, insert_vals = insert_m.group(1), insert_m.group(2)

        self._raw.execute(f"UPDATE {table} SET {set_clause} WHERE {key_col} = :{key_col}", params)
        if self._raw.rowcount == 0:
            self._raw.execute(f"INSERT INTO {table} ({insert_cols}) VALUES ({insert_vals})", params)
        self.description = None
        self.rowcount = self._raw.rowcount

    # -- change data feed --------------------------------------------------

    def _bump(self, table: str) -> None:
        """One commit on a table: its version goes up by one."""
        self._raw.execute("UPDATE __delta_log SET version = version + 1 WHERE tbl = ?", (table,))

    def _install_feed(self, table: str) -> None:
        """Start recording a table's changes from its current version."""
        self._raw.execute(
            "UPDATE __delta_log SET cdf_from = version WHERE tbl = ? AND cdf_from IS NULL", (table,)
        )
        log = _quote("__cdf_" + table)
        self._raw.execute(f"PRAGMA table_info({_quote(table)})")
        columns = [col[1] for col in self._raw.fetchall()]
        self._raw.execute(
            f"CREATE TABLE IF NOT EXISTS {log} ("
            + ", ".join(_quote(c) for c in columns)
            + ", _change_type TEXT, _commit_version INTEGER)"
        )
        names = ", ".join(_quote(c) for c in columns)
        version = (
            f"(SELECT version FROM __delta_log WHERE tbl = '{table.replace(chr(39), chr(39) * 2)}')"
        )

        def row(prefix: str, kind: str) -> str:
            values = ", ".join(f"{prefix}.{_quote(c)}" for c in columns)
            return f"INSERT INTO {log} ({names}, _change_type, _commit_version) VALUES ({values}, '{kind}', {version});"

        for event, body in (
            ("INSERT", row("NEW", "insert")),
            ("UPDATE", row("OLD", "update_preimage") + " " + row("NEW", "update_postimage")),
            ("DELETE", row("OLD", "delete")),
        ):
            trigger = _quote(f"__cdf_{table}_{event.lower()}")
            self._raw.execute(
                f"CREATE TRIGGER IF NOT EXISTS {trigger} AFTER {event} ON {_quote(table)} BEGIN {body} END"
            )

    def _table_changes(self, sql: str, params: Dict[str, Any]) -> str:
        """Swap ``table_changes(...)`` for a subquery over the table's log."""
        match = _TABLE_CHANGES.search(sql)
        table = _flatten_identifiers(params[match.group(1)]).strip('"').replace('""', '"')
        start, end = int(params[match.group(2)]), int(params[match.group(3)])
        self._raw.execute("SELECT cdf_from FROM __delta_log WHERE tbl = ?", (table,))
        found = self._raw.fetchone()
        if found is None or found[0] is None:
            raise sqlite3.OperationalError(f"change data feed is not enabled on {table}")
        if start < found[0]:
            raise sqlite3.OperationalError(
                f"change data feed of {table} starts at version {found[0]}, not {start}"
            )
        subquery = (
            f"(SELECT * FROM {_quote('__cdf_' + table)} "
            f"WHERE _commit_version BETWEEN {start} AND {end})"
        )
        for key in (match.group(1), match.group(2), match.group(3)):
            params.pop(key, None)
        return sql[: match.start()] + subquery + sql[match.end() :]

    # -- results ----------------------------------------------------------

    def _decode(self, row: Optional[Tuple[Any, ...]]) -> Optional[Tuple[Any, ...]]:
        """Turn the JSON text a ``dense_embedding`` column holds back into a list.

        Real Databricks hands ``ARRAY<DOUBLE>`` columns back as native lists;
        this fake stores them as TEXT (see the module docstring), so fetch is
        where that gets undone, for that column only.
        """

        if row is None or not self.description:
            return row
        names = [d[0] for d in self.description]
        if _ARRAY_COLUMN not in names:
            return row
        idx = names.index(_ARRAY_COLUMN)
        value = row[idx]
        if not isinstance(value, str):
            return row
        out = list(row)
        out[idx] = json.loads(value)
        return tuple(out)

    def fetchone(self) -> Optional[Tuple[Any, ...]]:
        if self._synthetic_rows is not None:
            return self._synthetic_rows.pop(0) if self._synthetic_rows else None
        return self._decode(self._raw.fetchone())

    def fetchall(self) -> List[Tuple[Any, ...]]:
        if self._synthetic_rows is not None:
            rows, self._synthetic_rows = self._synthetic_rows, []
            return rows
        return [self._decode(row) for row in self._raw.fetchall()]

    def close(self) -> None:
        self._raw.close()


class FakeDatabricksConnection:
    """Enough of a ``databricks.sql`` connection for ``DeltaLakeStorage``, in memory.

    Backed by one in-memory ``sqlite3`` connection in autocommit mode, so a
    write is visible to the next statement on the same fake connection
    without an explicit commit; the backend itself never commits.
    """

    def __init__(self) -> None:
        self._sqlite = sqlite3.connect(":memory:", isolation_level=None)
        self._sqlite.create_function("ARRAY", -1, _array_literal_fn)
        self._sqlite.execute(
            "CREATE TABLE __delta_log (tbl TEXT PRIMARY KEY, version INTEGER, cdf_from INTEGER)"
        )

    def cursor(self) -> FakeDatabricksCursor:
        return FakeDatabricksCursor(self._sqlite)

    def close(self) -> None:
        self._sqlite.close()
