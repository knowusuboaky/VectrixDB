"""A stand-in for a Blob container's append blobs, with the refusals Azure gives.

``FakeAppendContainer`` answers the calls :class:`vectrixdb.append_log.BlobDayLog`
makes of a ``ContainerClient``: its properties, a blob client, and a listing.
It keeps what an immutability policy keeps: a blob made cannot be made again
over the top of itself, and, unless protected append writes are allowed, a
locked container refuses the append that would add to one. A blob takes at
most ``limit`` blocks, as Azure's take 50,000.
"""

from __future__ import annotations

import types


class BlobError(Exception):
    """Shaped as azure-core's errors are: a status code and Azure's error code."""

    def __init__(self, status: int, code: str):
        super().__init__(f"{status} {code}")
        self.status_code = status
        self.error_code = code


class FakeAppendContainer:
    container_name = "audit"

    def __init__(self, *, locked: bool = True, appends: bool = True, limit: int = 50_000):
        self.locked = locked
        self.appends = appends
        self.limit = limit
        self.blobs: dict = {}
        self.blocks: dict = {}
        #: How many appends from now on are told the blob is not there, as a race with its maker would.
        self.not_there_yet = 0
        self.made: list = []

    def get_container_properties(self):
        return {"has_immutability_policy": self.locked, "has_legal_hold": False, "immutable_storage_with_versioning_enabled": False}

    def get_blob_client(self, name):
        return _Blob(self, name)

    def list_blobs(self, name_starts_with=""):
        return [types.SimpleNamespace(name=name) for name in sorted(self.blobs) if name.startswith(name_starts_with)]


class _Blob:
    def __init__(self, container: FakeAppendContainer, name: str):
        self.container = container
        self.name = name

    def create_append_blob(self, **conditions):
        assert conditions, "made without a condition, a blob made a moment ago by another server would be emptied"
        if self.name in self.container.blobs:
            raise BlobError(409, "BlobAlreadyExists")
        self.container.blobs[self.name] = bytearray()
        self.container.blocks[self.name] = 0
        self.container.made.append(self.name)

    def append_block(self, data):
        if self.container.not_there_yet:
            self.container.not_there_yet -= 1
            raise BlobError(404, "BlobNotFound")
        if self.name not in self.container.blobs:
            raise BlobError(404, "BlobNotFound")
        if self.container.locked and not self.container.appends:
            raise BlobError(409, "BlobImmutableDueToPolicy")
        if self.container.blocks[self.name] >= self.container.limit:
            raise BlobError(409, "BlockCountExceedsLimit")
        self.container.blobs[self.name] += bytes(data)
        self.container.blocks[self.name] += 1

    def download_blob(self):
        body = bytes(self.container.blobs[self.name])
        return types.SimpleNamespace(readall=lambda: body)


class FakeBlobService:
    """What evaluation._blob_client makes: one account, its containers by name."""

    def __init__(self, **containers: FakeAppendContainer):
        self.containers = containers
        self.asked: list = []

    def get_container_client(self, name):
        self.asked.append(name)
        return self.containers.setdefault(name, FakeAppendContainer())
