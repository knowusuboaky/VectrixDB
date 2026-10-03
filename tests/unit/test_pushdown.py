"""Where a filter runs, and a backend that has to say so.

A policy compiles to a filter, and where that filter runs decides what it is
worth. Run in the engine it is enforcement. Run after candidates come back it
returns the right documents, but the work done stays proportional to how
selective the entitlements were, and that is measurable from outside.

Only a backend that can run every field a policy names pushes it down;
the local index and the rest do not. The point of these tests is that
the declaration says so, and cannot quietly stop matching the behaviour.
"""

from __future__ import annotations

import pytest

from vectrixdb.core.storage import BaseStorage
from vectrixdb.core.types import FilterPushdown
from vectrixdb.exceptions import PushdownUnavailable
from vectrixdb.policy import AtMost, Overlap, Policy

RULES = [Overlap("e.roles", "roles"), AtMost("e.rank", "clearance")]
ANALYST = {"roles": ["a"], "clearance": 3}


def meta(rank=1, roles=("a",)):
    return {"e": {"roles": list(roles), "rank": rank}}


def _collection(tmp_path, policy=None, name="c"):
    from vectrixdb import Vectrix

    db = Vectrix(name, path=str(tmp_path), policy=policy)
    return db


class TestTheDeclaration:
    def test_every_backend_declares_where_its_filter_runs(self):
        """A subclass that forgets inherits POST, which is the safe answer:
        claiming ENGINE is the direction that would mislead."""
        assert BaseStorage.FILTER_PUSHDOWN is FilterPushdown.POST

        for subclass in BaseStorage.__subclasses__():
            declared = getattr(subclass, "FILTER_PUSHDOWN", None)
            assert isinstance(declared, FilterPushdown), subclass.__name__

    def test_none_of_them_claim_engine_yet(self):
        """The honest state of the library. When one of them starts pushing
        a policy filter into its query it changes its declaration, and the
        test below is what stops it changing the declaration alone."""
        claiming = [
            s.__name__
            for s in BaseStorage.__subclasses__()
            if getattr(s, "FILTER_PUSHDOWN", FilterPushdown.POST) is FilterPushdown.ENGINE
        ]
        assert claiming == [], (
            f"{claiming} say they filter in the engine. The collection calls vector_search "
            f"with a collection, a vector and a limit and no filter, so unless that call "
            f"site changed too the declaration is wrong."
        )

    def test_the_collection_reports_post(self, tmp_path):
        db = _collection(tmp_path)
        try:
            db.add(["one covenant"])
            assert db.pushdown_mode is FilterPushdown.POST
        finally:
            db.close()

    def test_no_filter_actually_reaches_a_backend(self, tmp_path):
        """The evidence behind the declaration rather than a comment saying
        so. A backend handed the policy could enforce it; none is handed it.

        The backend path has to be forced, because a populated collection is
        served by the local index. Without forcing it this test would pass
        by never calling the backend at all, which is the failure mode of
        every assertion written inside a loop over an empty list.
        """
        from vectrixdb import Vectrix

        seen = []
        db = Vectrix("watched", path=str(tmp_path), policy=Policy(RULES))
        try:
            db.add(["one covenant"], metadata=[meta()])
            collection = db._collection
            backend = collection._storage_backend
            original = backend.vector_search

            def record(*args, **kwargs):
                seen.append((args, kwargs))
                return original(*args, **kwargs)

            backend.vector_search = record
            collection.search(
                query=db._embed(["covenant"])[0],
                limit=5,
                principal=ANALYST,
                use_backend=True,
            )
        finally:
            db.close()

        assert seen, "the backend was never called, so this proved nothing"
        for args, kwargs in seen:
            assert "filter" not in kwargs and "filter_sql" not in kwargs, (
                "a filter reached the backend, so it could have been pushed down and the "
                "POST declaration is now wrong"
            )


class TestRequiringIt:
    def test_a_policy_that_requires_it_is_refused(self, tmp_path):
        """Everywhere, today. That is the honest answer and the reason the
        option exists: a deployment that needs engine-side enforcement should
        be told it does not have it rather than assume it."""
        db = _collection(tmp_path)
        db.add(["one covenant"], metadata=[meta()])
        db.close()

        with pytest.raises(PushdownUnavailable, match="every field the policy names"):
            _collection(tmp_path, Policy(RULES, require_pushdown=True))

    def test_it_refuses_at_open_rather_than_at_the_first_search(self, tmp_path):
        """Found at the first policied query means found in production, under
        a failure policy, at the worst possible moment."""
        with pytest.raises(PushdownUnavailable):
            _collection(tmp_path, Policy(RULES, require_pushdown=True))

    def test_the_refusal_names_the_path_that_decided(self, tmp_path):
        """Saying "SQLiteStorage" when the vectors never went near it sends
        whoever reads it to the wrong place."""
        db = _collection(tmp_path)
        db.add(["one covenant"], metadata=[meta()])
        db.close()

        with pytest.raises(PushdownUnavailable, match="the local index"):
            _collection(tmp_path, Policy(RULES, require_pushdown=True))

    def test_not_requiring_it_opens_normally(self, tmp_path):
        db = _collection(tmp_path, Policy(RULES))
        try:
            db.add(["one covenant"], metadata=[meta()])
            assert len(db.as_principal(ANALYST).search("covenant", limit=5)) == 1
        finally:
            db.close()

    def test_the_requirement_is_the_deployment_s_not_the_collection_s(self, tmp_path):
        """A collection written where pushdown was demanded is not a
        different collection from one where it was not, so it stays out of
        the fingerprint and out of what the collection remembers."""
        strict = Policy(RULES, require_pushdown=True)
        relaxed = Policy(RULES)
        assert strict.fingerprint == relaxed.fingerprint
        assert "require_pushdown" not in strict.to_dict()

        db = _collection(tmp_path, relaxed)
        db.add(["one covenant"], metadata=[meta()])
        db.close()

        # Reopening without the requirement is fine even though the stored
        # rules are identical to the strict policy's.
        again = _collection(tmp_path, relaxed)
        try:
            assert again.policy.fingerprint == relaxed.fingerprint
        finally:
            again.close()


class TestTheRecordSaysSo:
    def test_the_decision_record_carries_the_mode(self, tmp_path):
        """It was always None. A validator asking whether the filter was
        enforced or applied afterwards now has an answer on every row."""
        from vectrixdb import Vectrix
        from vectrixdb.audit import DENY, MemorySink

        sink = MemorySink(query_key=b"k", on_failure=DENY)
        db = Vectrix("rec", path=str(tmp_path), policy=Policy(RULES), on_retrieval=sink)
        try:
            db.add(["one covenant"], metadata=[meta()])
            db.as_principal(ANALYST).search("covenant", limit=5)
            assert sink.records[-1].pushdown_mode == "post"
        finally:
            db.close()


class TestFilterSqlIsGone:
    """`filter_sql` took a raw WHERE fragment and interpolated it:

        where_clause = f"AND {filter_sql}"

    Nothing in this library ever built one, so it was never exploited. It was
    still a public parameter that accepted arbitrary SQL, and it sat exactly
    where somebody would reach to push an entitlement filter down, which would
    have meant building that fragment out of a principal's values.

    It was deprecated with the removal set for 2.4, then removed in 2.2
    instead. Holding an injection surface open for two releases to honour a
    notice period is the wrong trade when nothing in the library calls it and
    the replacement, `filter=`, has always been there.
    """

    def _backend(self):
        import sys
        from pathlib import Path

        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        from fake_postgres import FakeConnection

        from vectrixdb.core.storage import LakebaseStorage

        storage = object.__new__(LakebaseStorage)
        storage._conn = FakeConnection()
        storage._schema = "public"
        return storage

    @pytest.mark.parametrize(
        "call",
        [
            pytest.param(
                lambda s: s.vector_search("c", [0.1, 0.2], 5, filter_sql="1=1"), id="vector"
            ),
            pytest.param(
                lambda s: s.hybrid_search("c", [0.1, 0.2], {1: 1.0}, 5, filter_sql="1=1"),
                id="hybrid",
            ),
        ],
    )
    def test_passing_one_is_a_type_error(self, call):
        """Loudly, rather than being accepted and ignored. A parameter that
        silently does nothing is how the Aurora backend used to return
        unfiltered rows to a caller who had asked for a filter."""
        storage = self._backend()

        with pytest.raises(TypeError, match="filter_sql"):
            call(storage)

    def test_the_searches_still_run_without_it(self):
        """The parameter went, the methods did not."""
        storage = self._backend()

        try:
            storage.vector_search("c", [0.1, 0.2], 5)
        except TypeError as exc:  # pragma: no cover - the failure this guards
            pytest.fail(f"vector_search lost more than the parameter: {exc}")
        except Exception:
            # The query fails against a table that is not there, which is not
            # what this test is about.
            pass

    def test_nothing_in_the_package_takes_one_any_more(self):
        """The guard that used to say nothing *builds* one. It can say
        something stronger now: nothing takes one either.

        A mention in prose is allowed, because the docstring on Aurora's
        hybrid search says what it used to accept and why it does not.
        """
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        uses = []
        for path in sorted((root / "vectrixdb").rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"\bfilter_sql\s*[=:]", line):
                    uses.append(f"{path.relative_to(root).as_posix()}:{number}: {line.strip()}")

        assert not uses, (
            "filter_sql is back. It interpolates a caller's SQL into the statement, "
            "which is the injection path its removal closed:\n  " + "\n  ".join(uses)
        )


class TestSessionRole:
    """Everything else decides in Python. This is the one piece that lets the
    database decide instead.

    What can be checked here is that the right statements are issued, in a
    transaction, in the right order, and that the role is given back. Whether
    PostgreSQL then refuses the rows is PostgreSQL's job and needs a live one
    with policies a DBA wrote, which the mocks-only rule keeps out of this
    suite.
    """

    def _pg(self, autocommit: bool):
        import sys
        from pathlib import Path

        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        from fake_postgres import FakeConnection

        from vectrixdb.core.storage import AuroraPostgreSQLStorage

        storage = object.__new__(AuroraPostgreSQLStorage)
        storage._conn = FakeConnection()
        storage._conn.autocommit = autocommit
        storage._schema = "public"
        return storage

    def test_the_role_is_set_for_the_transaction_and_given_back(self):
        """SET LOCAL rather than SET. A role that outlived the request would
        be worse than none: the next caller on this pooled connection would
        run as somebody else."""
        storage = self._pg(autocommit=False)
        with storage.session_role("analyst_u10442"):
            pass

        assert storage._conn.statements == ['SET LOCAL ROLE "analyst_u10442"']

    def test_autocommit_gets_an_explicit_transaction(self):
        """The Aurora backend runs in autocommit, where psycopg2 manages no
        transaction and SET LOCAL would apply to nothing, silently. One is
        opened rather than assumed."""
        storage = self._pg(autocommit=True)
        with storage.session_role("analyst_u10442"):
            pass

        assert storage._conn.statements == [
            "BEGIN",
            'SET LOCAL ROLE "analyst_u10442"',
            "ROLLBACK",
        ]

    def test_the_role_is_given_back_even_when_the_read_raises(self):
        storage = self._pg(autocommit=True)
        with pytest.raises(RuntimeError, match="boom"):
            with storage.session_role("analyst_u10442"):
                raise RuntimeError("boom")

        assert storage._conn.statements[-1] == "ROLLBACK"

    def test_no_role_means_no_statements(self):
        """A policy that names no role leaves the connection alone."""
        storage = self._pg(autocommit=True)
        with storage.session_role(None):
            pass
        assert storage._conn.statements == []

    @pytest.mark.parametrize(
        "hostile",
        [
            'analyst"; DROP TABLE points; --',
            "analyst; SET ROLE postgres",
            "analyst role",
            "",
            "1abc",
            "a" * 64,
        ],
    )
    def test_a_role_name_that_is_not_an_identifier_is_refused(self, hostile):
        """It goes into the statement as an identifier, which cannot be a
        bind parameter, so it is validated rather than escaped. Anything that
        is not a plain identifier is not a role name anybody meant."""
        storage = self._pg(autocommit=False)
        with pytest.raises(ValueError, match="not a usable PostgreSQL role name"):
            with storage.session_role(hostile):
                pass
        assert storage._conn.statements == []

    def test_a_backend_that_cannot_do_it_says_so(self):
        """Silently not assuming the role would leave the caller believing
        the database is enforcing when nothing is."""
        from vectrixdb.core.storage import InMemoryStorage, StorageConfig

        storage = InMemoryStorage(StorageConfig())
        assert storage.SUPPORTS_SESSION_ROLE is False

        with storage.session_role(None):
            pass  # no role asked for, nothing to do

        with pytest.raises(ValueError, match="cannot run a read as a database role"):
            with storage.session_role("analyst_u10442"):
                pass

    def test_the_policy_says_which_principal_key_holds_the_role(self):
        policy = Policy(RULES, db_role_key="db_role")
        assert policy.db_role({**ANALYST, "db_role": "analyst_u10442"}) == "analyst_u10442"

    def test_no_db_role_key_means_no_role(self):
        assert Policy(RULES).db_role(ANALYST) is None

    def test_a_missing_role_is_a_resolver_fault_not_a_denial(self):
        """Running as the connection's own role instead would mean the read
        happens with whatever privileges the application holds, which is the
        opposite of the point."""
        from vectrixdb.exceptions import PrincipalIncomplete

        policy = Policy(RULES, db_role_key="db_role")
        with pytest.raises(PrincipalIncomplete, match="db_role"):
            policy.db_role(ANALYST)
        with pytest.raises(PrincipalIncomplete, match="resolved to empty"):
            policy.db_role({**ANALYST, "db_role": ""})

    def test_the_setup_sql_is_the_dba_s_and_ships_as_text(self):
        """VectrixDB never issues it: a connection that can create a policy
        can drop one."""
        from vectrixdb.core.storage import AuroraPostgreSQLStorage

        sql = AuroraPostgreSQLStorage.RLS_SETUP.format(table="points", role="app")
        assert "ENABLE ROW LEVEL SECURITY" in sql
        assert "FORCE ROW LEVEL SECURITY" in sql, "without FORCE the table owner bypasses it"
        assert "CREATE POLICY" in sql
