"""An audit sink that writes where nothing can rewrite.

The PostgreSQL sink makes the audit trail something the audited application
cannot edit: its role holds INSERT and nothing else. This one makes the
stronger claim a regulator asks for, that the record could not have been
altered by anybody, because the object store itself refuses: every object is
written under a retention lock, in compliance mode, and the store will not
let it be overwritten or deleted until the retention date passes, whatever
credentials are presented.

The sink verifies the lock is on at construction and refuses otherwise. A
bucket without Object Lock accepts the same put and keeps the object exactly
as long as anybody with delete permission wants it to, which is a log, and
this must not quietly become one.

Every record is one object by default. Batching is available and means what
it says: a record in a batch that has not been flushed is not in the store
yet, so batching needs a :class:`~vectrixdb.audit.Spool` to hold what the
store has not accepted, and a process that dies with a batch in memory loses
that batch. One object per record costs a request per decision and loses
nothing, which is the right default for a trail kept seven years.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, Mapping, Optional, Protocol, Sequence

from .audit import AuditSink, Spool
from .exceptions import ConfigurationError

__all__ = ["ObjectStore", "ObjectLockSink", "S3ObjectLockStore", "read_locked"]


# ============================================================================
# THE STORE
# ============================================================================
#
# INPUT   an object store
# OUTPUT  what the sink needs from it, and nothing else
#
# A protocol, so a host can bring its own.


class ObjectStore(Protocol):
    """What the sink needs from an object store, and nothing else.

    Implement it against any store that can put an object under a retention
    lock. :class:`S3ObjectLockStore` is the one shipped; the fake in the
    test suite is another.
    """

    def lock_configuration(self) -> Optional[Mapping[str, Any]]:
        """The bucket's lock configuration, or None when locking is off."""

    def put_locked(self, key: str, body: bytes, *, retain_until: datetime, mode: str) -> None:
        """Write ``body`` at ``key`` under a retention lock."""

    def list(self, prefix: str) -> Sequence[str]:
        """Every key under ``prefix``, in order."""

    def get(self, key: str) -> bytes:
        """The body at ``key``."""


def _driver_errors() -> tuple:
    try:
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:  # pragma: no cover - boto3 is optional
        return ()
    return (BotoCoreError, ClientError)


# ============================================================================
# THE SINK, AND READING BACK
# ============================================================================
#
# INPUT   a decision record; a store and a prefix
# OUTPUT  records as immutable objects under a retention lock; every record
#         under the prefix in key order, each object checked against its name
#
# A record whose name does not match its contents is a tampered record, and is
# said.


class ObjectLockSink(AuditSink):
    """Records as immutable objects under a retention lock.

    Args:
        store: an :class:`ObjectStore`.
        query_key, on_failure: as for every sink.
        retain_days: how long the store must refuse to alter or delete each
            object. A trail for a regulated model is kept years, and the
            number is a business decision, so there is no default.
        mode: ``"COMPLIANCE"`` (nobody can shorten the retention, not even
            the account root) or ``"GOVERNANCE"`` (a specifically granted
            role can). Compliance is the default because governance mode
            answers the regulator's question with "nobody except us".
        batch_size: records per object. One by default; see the module
            docstring for what a larger number costs.
        prefix: key prefix in the bucket.
    """

    #: The store's own failures. Widened with the S3 driver's exceptions
    #: when it is installed; a TypeError from a broken store stays a bug.
    TRANSIENT: ClassVar[tuple] = (OSError, ConnectionError, TimeoutError) + _driver_errors()

    def __init__(
        self,
        store: ObjectStore,
        *,
        query_key: bytes,
        on_failure: Any,
        retain_days: int,
        mode: str = "COMPLIANCE",
        batch_size: int = 1,
        prefix: str = "vectrixdb-audit/",
    ) -> None:
        super().__init__(query_key=query_key, on_failure=on_failure)
        if mode not in ("COMPLIANCE", "GOVERNANCE"):
            raise ConfigurationError(f"mode is COMPLIANCE or GOVERNANCE, got {mode!r}")
        if not isinstance(retain_days, int) or retain_days < 1:
            raise ConfigurationError(
                f"retain_days is a whole number of days, at least 1, got {retain_days!r}. "
                f"How long an audit trail is kept is a business decision, so it has no default."
            )
        if batch_size < 1:
            raise ConfigurationError(f"batch_size is at least 1, got {batch_size!r}")
        if batch_size > 1 and not isinstance(on_failure, Spool):
            raise ConfigurationError(
                "batch_size above 1 needs on_failure=Spool(path). A record in a batch the "
                "store has not accepted yet is not in the store, and DENY has nothing to hold "
                "it in. One object per record works with either policy."
            )
        self.store = store
        self.retain_days = retain_days
        self.mode = mode
        self.batch_size = batch_size
        self.prefix = prefix
        self._pending: list[dict[str, Any]] = []
        self._sequence = 0
        self._verify_lock()

    def _verify_lock(self) -> None:
        """Refuse a bucket that would keep the objects only as long as anyone
        with delete permission felt like it."""
        configuration = self.store.lock_configuration()
        if not configuration:
            raise ConfigurationError(
                "the object store has no retention lock enabled, so anything written to it "
                "can be deleted or overwritten by whoever holds the credentials. That is a "
                "log, not an audit trail. Enable Object Lock on the bucket (S3: at creation, "
                "with versioning) or use PostgresAuditSink, whose grants are the control."
            )
        self.lock_configuration = dict(configuration)

    # -- writing -------------------------------------------------------------

    def _emit(self, record: Any) -> None:
        self._emit_raw(record.to_dict())

    def _emit_raw(self, payload: Mapping[str, Any]) -> None:
        self._pending.append(dict(payload))
        if len(self._pending) >= self.batch_size:
            self.flush()

    def flush(self) -> int:
        """Write every pending record as one object. Returns how many.

        On a store failure with a spool configured, the pending records go
        to the spool and the failure is swallowed, since that is what the
        spool is for; ``drain()`` brings them back through here. Without a
        spool the failure propagates, which under DENY refuses the answer.
        """
        if not self._pending:
            return 0
        batch, self._pending = self._pending, []
        body = "\n".join(json.dumps(r, sort_keys=True, default=str) for r in batch) + "\n"
        encoded = body.encode("utf-8")
        key = self._key_for(encoded)
        retain_until = datetime.now(timezone.utc) + timedelta(days=self.retain_days)
        try:
            self.store.put_locked(key, encoded, retain_until=retain_until, mode=self.mode)
        except self.TRANSIENT:
            if isinstance(self.on_failure, Spool):
                for payload in batch:
                    self._spool_raw(payload)
                return 0
            raise
        return len(batch)

    def _spool_raw(self, payload: Mapping[str, Any]) -> None:
        spool = self.on_failure
        assert isinstance(spool, Spool)
        spool.path.parent.mkdir(parents=True, exist_ok=True)
        with open(spool.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
            handle.flush()

    def _key_for(self, body: bytes) -> str:
        """Dated, sequenced, and carrying the body's hash.

        The hash in the name is what makes a substituted object visible: a
        body that does not hash to its own key was not the one written.
        """
        self._sequence += 1
        now = datetime.now(timezone.utc)
        digest = hashlib.sha256(body).hexdigest()[:16]
        return f"{self.prefix}{now:%Y/%m/%d}/{now:%Y%m%dT%H%M%S%f}Z-{self._sequence:06d}-{digest}.jsonl"

    # -- reading -------------------------------------------------------------

    def read_all(self) -> list[dict[str, Any]]:
        """Every record in the store under this prefix, in key order. See :func:`read_locked`."""
        return read_locked(self.store, self.prefix)

    def find(self, record_id: str) -> Optional[Mapping[str, Any]]:
        for payload in self.read_all():
            if record_id in (payload.get("decision_id"), payload.get("ingestion_id")):
                return payload
        return None


def read_locked(store: ObjectStore, prefix: str) -> list[dict[str, Any]]:
    """Every record in ``store`` under ``prefix``, in key order, each object checked against its name.

    A full scan; the store is the system of record, not an index. Each
    object's hash is checked against its key on the way through, and a
    mismatch raises rather than returning a record that was not the one
    written. Needs no sink, so a page can read what a server wrote.
    """
    found: list[dict[str, Any]] = []
    for key in store.list(prefix):
        body = store.get(key)
        digest = hashlib.sha256(body).hexdigest()[:16]
        if not key.endswith(f"-{digest}.jsonl"):
            raise ConfigurationError(
                f"object {key} does not hash to its own name: its body was not the one "
                f"written under that key. The lock should have made this impossible; "
                f"treat the store as compromised until you know why."
            )
        for line in body.decode("utf-8").splitlines():
            if line.strip():
                found.append(json.loads(line))
    return found


# ============================================================================
# S3 WITH OBJECT LOCK
# ============================================================================
#
# INPUT   a bucket with Object Lock, through boto3
# OUTPUT  an ObjectStore over it
#
# The PostgreSQL sink makes the audit trail something the audited application
# cannot edit; this makes it something nobody can, until the retention runs
# out.


class S3ObjectLockStore:
    """An :class:`ObjectStore` over an S3 bucket with Object Lock.

    Takes a boto3 client so the credentials, region and endpoint are the
    host's business; anything with the same four methods works, including a
    MinIO endpoint or a stand-in in a test. Object Lock has to be enabled on
    the bucket when it is created; it cannot be turned on afterwards, and
    this store does not try.
    """

    def __init__(self, bucket: str, client: Any = None) -> None:
        self.bucket = bucket
        if client is None:
            try:
                import boto3
            except ImportError as exc:
                raise ConfigurationError(
                    "S3ObjectLockStore needs boto3: pip install boto3, or pass client="
                ) from exc
            client = boto3.client("s3")
        self.client = client

    def lock_configuration(self) -> Optional[Mapping[str, Any]]:
        try:
            response = self.client.get_object_lock_configuration(Bucket=self.bucket)
        except Exception as exc:
            # The bucket has no lock configuration at all, which S3 reports
            # as an error rather than an empty answer.
            if "ObjectLockConfigurationNotFoundError" in str(exc):
                return None
            raise
        configuration = response.get("ObjectLockConfiguration") or {}
        if configuration.get("ObjectLockEnabled") != "Enabled":
            return None
        return configuration

    def put_locked(self, key: str, body: bytes, *, retain_until: datetime, mode: str) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType="application/x-ndjson",
            ObjectLockMode=mode,
            ObjectLockRetainUntilDate=retain_until,
        )

    def list(self, prefix: str) -> Sequence[str]:
        keys: list[str] = []
        token: Optional[str] = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            response = self.client.list_objects_v2(**kwargs)
            keys.extend(item["Key"] for item in response.get("Contents", []))
            if not response.get("IsTruncated"):
                return sorted(keys)
            token = response.get("NextContinuationToken")

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
