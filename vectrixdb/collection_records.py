"""Each collection's record, in one store every server reads.

A collection's record says where its files arrive and who may search it.
Every server reads the same record, so a rule set on one is the rule on all
of them, however each opened the collection. The record is the choke point:
a collection with no record, or a record with no policy, answers nobody.

Where the records live is the caller's choice, and the stores are the ones
the sign-in store already runs on (``vectrixdb.signin.records``):

    a path, or sqlite:///<path>                                    a file, for one server
    postgresql://<user>@<host>/<database>                          several servers, the postgres extra
    cosmos://<account>.documents.azure.com/<database>/<container>  Azure Cosmos DB, the azure extra
    dynamodb://<table>?region=<region>                             Amazon DynamoDB, the aws extra

``VECTRIXDB_COLLECTION_STORE`` names it to the server, with its key in
``VECTRIXDB_COLLECTION_STORE_KEY`` (or ``_KEY_FILE``) and never in the
address. Left unset, nothing is gated: the server serves collections as it
always has, which is what a server on one machine with nobody to answer to
expects.

One record, as a person writes it and as it is kept::

    {
      "name": "financial",
      "path": "raw/financial/",
      "policy": {"method": "store", "allow": [{"email": "ama@company.com"}, {"domain": "company.com"}]}
    }

``name`` is the collection's, and its search index's, and the file's when the
record is a file. ``path`` is where its files are dropped, when a pipeline
feeds it. ``policy`` is who may search it, the whole rule: see
:mod:`vectrixdb.collection_access`. ``null`` is nobody yet, and an empty
policy is refused, since a policy names a method. Everyone who reaches the
server sees that the collection exists, its name and its size; what is in it
answers only the people the policy names. Identifiers in what people are
shown are always masked, so there is nothing to switch.

Reading is cached for a short while, so a search does not become a round trip
to the store. When the store cannot be read, a copy held from a moment ago
stands in; with none, the read raises ``CollectionStoreUnavailable`` and the
search does not run.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Union

from .collection_access import AccessPolicy
from .exceptions import CollectionStoreUnavailable, ConfigurationError

__all__ = [
    "KIND",
    "STORE_ENV",
    "CollectionRecord",
    "CollectionRecords",
    "describe_collection_store",
    "open_collection_store",
]


# ============================================================================
# SETTINGS: the log, the record's kind, and the store's setting
# ============================================================================
#
# How a record is told from the other kinds in the same store, and which
# variable names the store.

log = logging.getLogger("vectrixdb.collection_records")

#: The kind every collection record is kept under, in the records layer.
KIND = "collection"
#: The setting that names the store to a server.
STORE_ENV = "VECTRIXDB_COLLECTION_STORE"


# ============================================================================
# A RECORD
# ============================================================================
#
# INPUT   a stored item, or a request
# OUTPUT  one collection's record: where its files go and who may search
#         it, with its policy as an object
#
# The record is the rule: a server reads it, and never a copy.


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _is_token(policy: Mapping[str, Any]) -> bool:
    """Whether a policy as written reads as the token method, its earlier spelling included."""
    method = str(policy.get("method") or "").strip().lower()
    return method == "token" or (method == "groups" and str(policy.get("source") or "").strip().lower() != "store")


@dataclass
class CollectionRecord:
    """One collection's record: where its files go, and who may search it."""

    name: str
    #: When this collection was made. A collection deleted and made again is a new generation.
    generation: str
    #: Where its files arrive, when a pipeline feeds it: raw/<name>/.
    path: Optional[str] = None
    #: Who may search it, as ``AccessPolicy.to_dict()`` writes it, or None for nobody yet.
    policy: Optional[Dict[str, Any]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    #: Who made the last change: a person's email, or the script that pushed it.
    changed_by: Optional[str] = None
    _policy: Any = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ConfigurationError("a collection record needs the collection's name")
        if self.path is not None:
            self.path = str(self.path).strip()
            last = self.path.strip("/").split("/")[-1] if self.path.strip("/") else ""
            if last != self.name:
                raise ConfigurationError(f"{self.name}: its path ends in {last or 'nothing'!r}, and the last folder is the collection: raw/{self.name}/")
        if self.policy is not None:
            # An empty policy is not "nobody yet", it is a mistake: None says nobody.
            if not isinstance(self.policy, Mapping) or not self.policy.get("method"):
                raise ConfigurationError(f"{self.name}: a policy names a method, store or token, and who it allows; null is nobody yet")
            # Built once, here, so a record that cannot become a policy is refused
            # where it is written rather than at the first search.
            self._policy = AccessPolicy.from_dict(self.policy)
            self.policy = self._policy.to_dict()

    # ------------------------------------------------------------ reading ---

    def policy_object(self) -> Optional[AccessPolicy]:
        """Who may search it, as an ``AccessPolicy``, or None for nobody yet."""
        return self._policy

    # -------------------------------------------------------------- shapes ---

    def to_data(self) -> Dict[str, Any]:
        """What the store keeps under the record's key."""
        return {
            "generation": self.generation,
            "path": self.path,
            "policy": self.policy,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "changed_by": self.changed_by,
        }

    @classmethod
    def from_data(cls, name: str, data: Mapping[str, Any]) -> "CollectionRecord":
        """A record from what the store kept, or from a file a person wrote.

        A record written before 2.3 is read as it meant: ``folder`` is the
        path; ``visibility``, ``masking`` and ``entitlement`` are read past,
        since the policy is the whole rule now; and a ``policy`` holding
        ``rules`` was an entitlement, which reads as nobody yet. So does a
        token policy with no list of people: a group alone would let everyone
        in it search, which is exactly what the list is there to stop.
        """
        policy = data.get("policy")
        if isinstance(policy, Mapping) and "rules" in policy and "method" not in policy:
            log.warning("%s: its record carries a per-document policy, which records no longer hold; it reads as nobody yet", name)
            policy = None
        if isinstance(policy, Mapping) and _is_token(policy) and not policy.get("people"):
            log.warning("%s: its policy names security groups and no list of people; it reads as nobody yet until people are added", name)
            policy = None
        return cls(
            name=name,
            generation=str(data.get("generation") or data.get("created_at") or _now()),
            path=data.get("path") if data.get("path") is not None else data.get("folder"),
            policy=dict(policy) if isinstance(policy, Mapping) else None,
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            changed_by=data.get("changed_by"),
        )

    def to_json(self) -> Dict[str, Any]:
        """The record as a person reads and writes it."""
        out: Dict[str, Any] = {"name": self.name, "path": self.path, "policy": self.policy}
        return {k: v for k, v in out.items() if v is not None or k == "policy"}

    @classmethod
    def from_json(cls, source: Union[str, Path, Mapping[str, Any]]) -> "CollectionRecord":
        """A record from ``to_json``'s shape: a mapping, JSON text, or a file of it.

        The file's name is the collection's name when the JSON has none, so
        ``financial.json`` holding only the rules is enough.
        """
        name_from_file = None
        if isinstance(source, Path) or (isinstance(source, str) and source.strip().endswith(".json") and Path(source).is_file()):
            path = Path(source)
            name_from_file = path.stem
            source = path.read_text(encoding="utf-8")
        data = json.loads(source) if isinstance(source, str) else dict(source)
        name = str(data.get("name") or data.get("id") or name_from_file or "")
        if name_from_file and name and name != name_from_file:
            raise ConfigurationError(f"{name_from_file}.json says it is the record for {name!r}; one of the two is wrong")
        return cls.from_data(name, data)


# ============================================================================
# THE STORE
# ============================================================================
#
# INPUT   a store every server shares
# OUTPUT  every collection's record, read, written and listed, with what was
#         read earlier standing in when the store cannot be read right now
#
# One record a collection, so every instance and the hosted API read the same
# rule.


class CollectionRecords:
    """Every collection's record, in a store every server shares.

    ``records`` is any store of ``vectrixdb.signin.records``: SQLite,
    PostgreSQL, Cosmos DB or DynamoDB, or your own. ``fresh_for`` is how long a
    record read is used before the store is asked again.
    """

    def __init__(self, records: Any, *, fresh_for: float = 30.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._records = records
        self._fresh_for = float(fresh_for)
        self._clock = clock
        self._lock = threading.Lock()
        self._held: Dict[str, Tuple[float, Optional[CollectionRecord]]] = {}

    @classmethod
    def open(cls, where: Any, *, key: Optional[str] = None, fresh_for: float = 30.0) -> "CollectionRecords":
        """Open the store at ``where``: an address, a path, or a records store already made."""
        from .signin.records import Records, open_records

        records = where if isinstance(where, Records) else open_records(where, key=key, setting=STORE_ENV)
        return cls(records, fresh_for=fresh_for)

    # ------------------------------------------------------------ reading ---

    def get(self, name: str) -> Optional[CollectionRecord]:
        """The record for ``name``, or None when there is none.

        A record read within ``fresh_for`` seconds is used as it is. When the
        store cannot be read, one held from earlier stands in, however old,
        and the failure is logged; with none held, ``CollectionStoreUnavailable``.
        """
        now = self._clock()
        with self._lock:
            held = self._held.get(name)
        if held is not None and now - held[0] < self._fresh_for:
            return held[1]
        try:
            found = self._records.get(KIND, name)
        except Exception as exc:  # the store's own error, whatever database it is
            if held is not None:
                log.warning("collection records at %s could not be read (%s); using %s's record from earlier", self.describe(), type(exc).__name__, name)
                return held[1]
            raise CollectionStoreUnavailable(name, self.describe(), exc) from exc
        record = CollectionRecord.from_data(name, found.data) if found is not None else None
        with self._lock:
            self._held[name] = (now, record)
        return record

    def names(self) -> List[str]:
        """Every collection with a record, in order."""
        return sorted(r.key for r in self._records.query(KIND))

    def all(self) -> List[CollectionRecord]:
        """Every record, in order of name."""
        records = [CollectionRecord.from_data(r.key, r.data) for r in self._records.query(KIND)]
        return sorted(records, key=lambda record: record.name)

    # ------------------------------------------------------------ writing ---

    def put(self, record: CollectionRecord, *, by: Optional[str] = None) -> CollectionRecord:
        """Keep ``record``, replacing what was there. Returns it as kept."""
        from .signin.records import Record

        now = _now()
        kept = replace(
            record,
            created_at=record.created_at or now,
            updated_at=now,
            changed_by=by if by is not None else record.changed_by,
        )
        self._records.put(Record(KIND, kept.name, kept.to_data()))
        with self._lock:
            self._held[kept.name] = (self._clock(), kept)
        return kept

    def delete(self, name: str) -> bool:
        """Remove a collection's record entirely. Whether one went.

        A collection deleted takes its record with it, so one made again
        under the name starts with no policy, which answers nobody until
        somebody gives it one: the safe way to start.
        """
        gone = self._records.delete(KIND, name)
        with self._lock:
            self._held[name] = (self._clock(), None)
        return bool(gone)

    def _change(self, name: str, by: Optional[str], generation: Optional[str], **changes: Any) -> CollectionRecord:
        record = self.get(name) or CollectionRecord(name=name, generation=generation or _now())
        return self.put(replace(record, **changes), by=by)

    def set_policy(self, name: str, policy: Any, *, by: Optional[str] = None, generation: Optional[str] = None) -> CollectionRecord:
        """Say who may search a collection, an ``AccessPolicy`` or its dict, or nobody yet with None."""
        as_dict = policy.to_dict() if hasattr(policy, "to_dict") else policy
        return self._change(name, by, generation, policy=as_dict)

    # -------------------------------------------------------------- about ---

    def describe(self) -> str:
        """Where this is, in words fit for a log: never a key."""
        return str(self._records.describe())

    def close(self) -> None:
        self._records.close()


# ============================================================================
# OPENING AND DESCRIBING
# ============================================================================
#
# INPUT   an address, a store, or None
# OUTPUT  None stays None, a store is used as it is, anything else is opened;
#         where records are kept, for the log, none when nothing is gated
#
# Nothing gated is a state the log says plainly.


def open_collection_store(where: Any, *, key: Optional[str] = None) -> Optional[CollectionRecords]:
    """``where`` as a collection store: None stays None, a store is used as it is, anything else is opened."""
    if where is None or where == "":
        return None
    if isinstance(where, CollectionRecords):
        return where
    return CollectionRecords.open(where, key=key)


def describe_collection_store(store: Optional[CollectionRecords]) -> str:
    """Where a server keeps collection records, for its log: 'none' when nothing is gated."""
    return store.describe() if store is not None else "none, every collection is served as it always was"
