"""Row level security, against a real PostgreSQL.

The half of the entitlement work this repository's own suite cannot prove.
`tests/unit/test_pushdown.py` checks that `SET LOCAL ROLE` is issued, quoted
and scoped to a transaction, against an in-memory stand-in. Whether
PostgreSQL then actually refuses the rows is a question only PostgreSQL can
answer, and the answer is the entire point of the feature: enforcement below
the filter, where an application bug cannot reach it.

So this module is gated. It runs when `VECTRIXDB_LIVE_BACKENDS` names
`postgres_rls` and `VECTRIXDB_RLS_DSN` points at a database where the test
user may create roles and tables, which means a throwaway database, with
pgvector installed: the backend creates the extension when it connects. The
nightly workflow gives it one. Nothing here runs in the default suite, and
nothing here is mocked: a mocked row level security test would assert that
the mock does what it was told to do.

What it proves, in order:

1. a role that the policy does not admit gets no rows, from the database
   rather than from the library
2. a role that it does admit gets exactly its own
3. the connection is handed back as the original user, so the next request
   on a pooled connection does not inherit somebody else's role
4. the role is given back even when the query inside raises

Three and four are the ones worth having a real server for. A stand-in can
be made to forget a role; a real pool will not forgive it.
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

pytestmark = pytest.mark.backend

DSN = os.environ.get("VECTRIXDB_RLS_DSN", "")
ENABLED = "postgres_rls" in {
    name.strip() for name in os.environ.get("VECTRIXDB_LIVE_BACKENDS", "").split(",")
}

if not (ENABLED and DSN):
    pytest.skip(
        "set VECTRIXDB_LIVE_BACKENDS=postgres_rls and VECTRIXDB_RLS_DSN to run this "
        "against a real PostgreSQL",
        allow_module_level=True,
    )

psycopg2 = pytest.importorskip("psycopg2")

TABLE = f"rls_probe_{uuid.uuid4().hex[:8]}"
READER = f"{TABLE}_reader"
OUTSIDER = f"{TABLE}_outsider"


@pytest.fixture(scope="module")
def database():
    """A table with row level security on it, and two roles.

    Written the way a DBA would write it rather than the way the library
    would, because that is the arrangement being tested: the library holds no
    privilege to create any of this, and the point of the feature is that the
    rules live somewhere the application cannot edit.
    """
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE {TABLE} (
                id TEXT PRIMARY KEY,
                metadata JSONB NOT NULL
            )
        """)
        cur.execute(
            f"INSERT INTO {TABLE} (id, metadata) VALUES "
            f"(%s, %s::jsonb), (%s, %s::jsonb), (%s, %s::jsonb)",
            (
                "commercial-1",
                '{"entitlements": {"lob": "commercial_banking"}}',
                "commercial-2",
                '{"entitlements": {"lob": "commercial_banking"}}',
                "wealth-1",
                '{"entitlements": {"lob": "wealth"}}',
            ),
        )
        cur.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
        cur.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
        for role in (READER, OUTSIDER):
            cur.execute(f"CREATE ROLE {role}")
            cur.execute(f"GRANT SELECT ON {TABLE} TO {role}")
            cur.execute(f"GRANT {role} TO CURRENT_USER")
        # The rule: this role sees commercial banking and nothing else. The
        # outsider is named by no policy at all, which is the case that has
        # to come back empty rather than unfiltered.
        cur.execute(f"""
            CREATE POLICY {TABLE}_commercial ON {TABLE}
                FOR SELECT
                TO {READER}
                USING (metadata->'entitlements'->>'lob' = 'commercial_banking')
        """)
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
        for role in (READER, OUTSIDER):
            cur.execute(f"DROP ROLE IF EXISTS {role}")
    conn.close()


@pytest.fixture
def storage(database):
    """An Aurora backend bound to the same database, connected."""
    from vectrixdb.core.storage import AuroraPostgreSQLStorage, StorageBackend, StorageConfig

    # The backend takes its connection as separate aurora_* fields rather
    # than a DSN, so the one address the workflow gives is taken apart here.
    # TLS only when the address asks for it: Aurora requires it, and the
    # service container the nightly job runs does not speak it.
    dsn = urlsplit(DSN)
    sslmode = parse_qs(dsn.query).get("sslmode", ["disable"])[-1]
    backend = AuroraPostgreSQLStorage(
        StorageConfig(
            backend=StorageBackend.AURORA_POSTGRESQL,
            aurora_host=dsn.hostname or "localhost",
            aurora_port=dsn.port or 5432,
            aurora_database=unquote(dsn.path.lstrip("/")) or "postgres",
            aurora_user=unquote(dsn.username) if dsn.username else None,
            aurora_password=unquote(dsn.password) if dsn.password else None,
            aurora_ssl=sslmode not in ("disable", "allow", "prefer"),
        )
    )
    backend.connect()
    yield backend
    close = getattr(backend, "close", None)
    if callable(close):
        close()


def _rows(storage, role):
    with storage.session_role(role):
        with storage._conn.cursor() as cur:
            cur.execute(f"SELECT id FROM {TABLE} ORDER BY id")
            return [row[0] for row in cur.fetchall()]


def test_the_database_refuses_the_rows_the_role_may_not_see(storage):
    """The claim the whole design rests on, made by PostgreSQL.

    Not a filter the library built and not one it could forget to build: the
    role is assumed, the rows are simply not there.
    """
    assert _rows(storage, READER) == ["commercial-1", "commercial-2"]


def test_a_role_no_policy_admits_gets_nothing(storage):
    """Fail closed, in the database. A table with row level security forced
    on and no policy naming this role returns the empty set rather than
    everything, which is the opposite of what an application-side filter does
    when somebody forgets it."""
    assert _rows(storage, OUTSIDER) == []


def test_the_connection_is_handed_back_as_it_was(storage):
    """SET LOCAL, not SET. The transaction ending is what gives the role
    back, and the next request on a pooled connection must not inherit it."""
    with storage._conn.cursor() as cur:
        cur.execute("SELECT current_user")
        before = cur.fetchone()[0]

    _rows(storage, READER)

    with storage._conn.cursor() as cur:
        cur.execute("SELECT current_user")
        assert cur.fetchone()[0] == before


def test_the_role_is_given_back_even_when_the_query_raises(storage):
    """The case that would poison a pool quietly: an exception inside the
    block leaves the connection running as somebody else, and the next
    request in the process inherits it."""
    with storage._conn.cursor() as cur:
        cur.execute("SELECT current_user")
        before = cur.fetchone()[0]

    with pytest.raises(psycopg2.Error):
        with storage.session_role(READER):
            with storage._conn.cursor() as cur:
                cur.execute("SELECT * FROM a_table_that_is_not_there")

    storage._conn.rollback()
    with storage._conn.cursor() as cur:
        cur.execute("SELECT current_user")
        assert cur.fetchone()[0] == before


def test_the_unprivileged_role_cannot_undo_the_policy(storage):
    """Worth stating: if the application role could ALTER TABLE, the control
    would be advisory. It cannot."""
    with pytest.raises(psycopg2.Error):
        with storage.session_role(READER):
            with storage._conn.cursor() as cur:
                cur.execute(f"ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY")
    storage._conn.rollback()
