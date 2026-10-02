"""Two servers changing the same sign-in record at the same moment.

Every change to a record is read, change, write back only if nobody wrote it
in between, and again if somebody did. The contract tests prove the happy
path on every backend; nothing made the write lose. Here a record layer loses
on purpose, as it does when another server got there first, so the retry, and
what happens when the retries run out, are held to.
"""

from __future__ import annotations

import pytest

pytest.importorskip("cryptography", reason="the signin extra is not installed")

from vectrixdb.exceptions import VectrixError  # noqa: E402
from vectrixdb.signin import SignInStore  # noqa: E402
from vectrixdb.signin import store as store_module  # noqa: E402

SECRET = "k" * 48
ADA = "ada@example.com"


class Losing:
    """The real records, with a number of writes that lose to another server first."""

    def __init__(self, real, lose=0, kinds=None, meanwhile=None):
        self._real, self.lose, self.kinds, self.meanwhile = real, lose, kinds, meanwhile
        self.lost = 0

    def __getattr__(self, name):
        return getattr(self._real, name)

    def _loses(self, record):
        if self.lose > 0 and (self.kinds is None or record.kind in self.kinds):
            self.lose -= 1
            self.lost += 1
            if self.meanwhile is not None:
                self.meanwhile(record)
            return True
        return False

    def replace(self, record):
        return False if self._loses(record) else self._real.replace(record)


@pytest.fixture
def store(tmp_path):
    made = SignInStore(tmp_path / "signin.db", (SECRET,))
    made.put_person(ADA, "operator")
    yield made
    made.close()


def lose(store, times, kinds=None, meanwhile=None):
    store._records = Losing(store._records, times, kinds, meanwhile)
    return store._records


class TestAChangeThatLosesIsTriedAgain:
    def test_a_role_change_lands_after_another_server_wrote_first(self, store):
        records = lose(store, 3, {store_module.PERSON})
        store.put_person(ADA, "admin")
        assert records.lost == 3 and store.person(ADA).role == "admin"

    def test_the_change_is_made_to_what_the_other_server_wrote_not_over_it(self, store):
        """The retry reads again, so the other server's write is kept and not undone."""
        real = store._records

        def other_server(record):
            fresh = real.get(record.kind, record.key)
            fresh.data["name"] = "Ada Lovelace"
            assert real.replace(fresh)

        lose(store, 1, {store_module.PERSON}, other_server)
        store.put_person(ADA, "admin")
        kept = real.get(store_module.PERSON, ADA).data
        assert kept["role"] == "admin" and kept["name"] == "Ada Lovelace"

    def test_when_every_try_loses_it_says_so_and_changes_nothing(self, store):
        lose(store, store_module._TRIES, {store_module.PERSON})
        with pytest.raises(VectrixError, match="kept changing the same sign-in person record"):
            store.put_person(ADA, "admin")
        assert store.person(ADA).role == "operator"

    def test_a_change_to_somebody_who_is_not_there_is_none(self, store):
        assert store._change(store_module.PERSON, "nobody@example.com", lambda data: None) is None

    def test_a_change_that_declines_writes_nothing(self, store):
        records = lose(store, 0)
        assert store._change(store_module.PERSON, ADA, lambda data: False) is False
        assert records.lost == 0 and store.person(ADA).role == "operator"


class TestTheRateLimitUnderContention:
    def test_a_lost_count_is_counted_on_the_next_try(self, store):
        assert store.within_rate("guest:x", 5)[0] is True  # makes the minute's record
        records = lose(store, 2, {store_module.RATE})
        assert store.within_rate("guest:x", 5)[0] is True and records.lost == 2
        counted = list(store._records.query(store_module.RATE))[0].data["n"]
        assert counted == 2, "two events, counted once each however many writes were lost"

    def test_when_every_write_loses_it_reads_as_over_the_allowance(self, store):
        """Only a burst from one caller makes that many servers write one record at once."""
        assert store.within_rate("guest:x", 50)[0] is True
        lose(store, store_module._TRIES, {store_module.RATE})
        allowed, wait = store.within_rate("guest:x", 50)
        assert allowed is False and 1 <= wait <= 60


class TestOtherPlacesThatRetry:
    def test_a_passkey_handle_is_one_handle_even_when_the_first_write_loses(self, store):
        records = lose(store, 2, {store_module.PERSON})
        first = store.user_handle(ADA)
        assert records.lost == 2 and len(first) == 16
        assert store.user_handle(ADA) == first, "the handle a passkey carries never changes"

    def test_a_handle_for_nobody_is_random_and_kept_nowhere(self, store):
        one, two = store.user_handle("nobody@example.com"), store.user_handle("nobody@example.com")
        assert len(one) == 16 and one != two

    def test_somebody_another_server_added_at_the_same_moment_is_changed_not_added_twice(self, store):
        """create() loses because the other server's add landed between the read and the write."""
        real = store._records

        class AddedMeanwhile(Losing):
            def create(self, record):
                if record.kind == store_module.PERSON and not self.lost:
                    self.lost += 1
                    other = store_module.Record(record.kind, record.key, dict(record.data, role="viewer"))
                    assert real.create(other)
                    return False
                return real.create(record)

        store._records = AddedMeanwhile(real)
        assert store.put_person("new@example.com", "operator").role == "operator"
        assert len([r for r in real.query(store_module.PERSON) if r.key == "new@example.com"]) == 1

    def test_a_wrong_code_is_counted_even_when_the_count_loses_first(self, store):
        records = lose(store, 2, {store_module.ATTEMPT})
        store.failed("email:" + ADA)  # first failure makes the record
        store.failed("email:" + ADA)
        assert records.lost == 2
        counts = [r.data for r in store._records.query(store_module.ATTEMPT)]
        assert counts and max(int(c.get("count", c.get("n", 0))) for c in counts) == 2

    def test_a_key_last_used_stamp_that_loses_does_not_refuse_the_key(self, store):
        made, key = store.create_key("app", "reader", None)
        lose(store, 5, {store_module.APIKEY})
        assert store.api_key(key).name == "app", "a stamp is not worth a refusal"
