"""The object-lock sink against a real bucket.

What the fake cannot prove: that S3 itself refuses to delete or overwrite an
object written under a compliance lock. Gated, because it needs a bucket
created with Object Lock enabled and credentials that can write to it, which
is a cloud account. Set VECTRIXDB_LIVE_BACKENDS=s3_object_lock and
VECTRIXDB_OBJECT_LOCK_BUCKET; the usual AWS environment supplies the rest.

The objects it writes stay for the retention it sets, one day, because that
is the point; use a bucket you are content to fill with test records for a
day at a time.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.backend

BUCKET = os.environ.get("VECTRIXDB_OBJECT_LOCK_BUCKET", "")
ENABLED = "s3_object_lock" in {
    n.strip() for n in os.environ.get("VECTRIXDB_LIVE_BACKENDS", "").split(",")
}
if not (ENABLED and BUCKET):
    pytest.skip(
        "set VECTRIXDB_LIVE_BACKENDS=s3_object_lock and VECTRIXDB_OBJECT_LOCK_BUCKET to run "
        "this against a real bucket",
        allow_module_level=True,
    )

boto3 = pytest.importorskip("boto3")
botocore = pytest.importorskip("botocore")


@pytest.fixture(scope="module")
def sink():
    from vectrixdb.audit import DENY
    from vectrixdb.objectlock import ObjectLockSink, S3ObjectLockStore

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return ObjectLockSink(
        S3ObjectLockStore(BUCKET),
        query_key=b"live",
        on_failure=DENY,
        retain_days=1,
        prefix=f"vectrixdb-live/{stamp}/",
    )


def test_the_bucket_really_has_the_lock(sink):
    assert sink.lock_configuration.get("ObjectLockEnabled") == "Enabled"


def test_s3_refuses_to_delete_what_was_written(sink):
    """The claim the sink exists to make, made by S3 rather than the fake."""
    from vectrixdb.audit import RetrievalRecord

    sink.write(
        RetrievalRecord(
            decision_id=RetrievalRecord.new_id(),
            decided_at=datetime.now(timezone.utc),
            collection="live",
        )
    )
    key = sink.store.list(sink.prefix)[0]
    client = sink.store.client

    with pytest.raises(botocore.exceptions.ClientError):
        client.delete_object(Bucket=BUCKET, Key=key, BypassGovernanceRetention=True)
    with pytest.raises(botocore.exceptions.ClientError):
        client.put_object(Bucket=BUCKET, Key=key, Body=b"overwritten")

    assert sink.read_all()[0]["collection"] == "live"
