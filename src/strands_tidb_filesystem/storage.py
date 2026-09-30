"""Strands ``Storage`` backend on TiDB Cloud Filesystem, a drop-in for ``S3Storage``."""

from __future__ import annotations

import asyncio
import builtins
import logging
import posixpath
from typing import TYPE_CHECKING

from strands.storage.storage import (
    _NAMESPACED,
    StorageSearchResult,
    _NamespacedStorage,
    _normalize_key,
    _normalize_prefix,
)
from strands.types.exceptions import StorageError

from .client import TiFSClient
from .errors import TiFSError

if TYPE_CHECKING:
    from strands.storage.search.types import SearchStrategy

logger = logging.getLogger(__name__)


class TiDBFilesystemStorage:
    """Persist bytes as files in TiDB Cloud Filesystem.

    Mirrors :class:`strands.storage.S3Storage`: the file system plays the role of
    the bucket and ``prefix`` namespaces keys inside it. Each key is stored as the
    file ``/<prefix>/<key>``.

    Example:
        ```python
        from strands import Agent
        from strands.session import SnapshotSessionManager
        from strands_tidb_filesystem import TiDBFilesystemStorage

        storage = TiDBFilesystemStorage("<file-system-id>", prefix="agents/")

        # Session snapshots, FileMemoryStore and the context offloader all use it:
        agent = Agent(storage=storage, session_manager=SnapshotSessionManager("user-42"))
        ```

    Args:
        file_system_id: File system to use (the counterpart of an S3 bucket), with the
            token saved in the ``ti`` profile. Defaults to ``TI_FS_FILE_SYSTEM_ID``, or
            the file system encoded in ``fs_token``.
        prefix: Path prefix prepended to every key.
        region_name: Region code such as ``aws-us-east-1``. Defaults to ``TI_REGION_CODE``.
        fs_token: File system token. Defaults to ``TI_FS_TOKEN``.
        client: Pre-configured :class:`TiFSClient`. Cannot be combined with
            ``file_system_id``, ``region_name`` or ``fs_token``.
        search_strategy: Optional search strategy. When set, ``write()`` indexes entries
            and ``search()`` delegates to it. Otherwise ``search()`` uses TiDB Cloud
            Filesystem's full-text and semantic index.

    Raises:
        StorageError: If ``client`` is combined with connection arguments.
    """

    # Set to Strands' sentinel on views returned by namespace(), so they are not re-scoped.
    _namespaced: object | None = None

    def __init__(
        self,
        file_system_id: str | None = None,
        *,
        prefix: str = "",
        region_name: str | None = None,
        fs_token: str | None = None,
        client: TiFSClient | None = None,
        search_strategy: SearchStrategy[TiDBFilesystemStorage] | None = None,
    ) -> None:
        """Initialize the storage backend."""
        if client is not None and (file_system_id or region_name or fs_token):
            raise StorageError("Cannot specify client together with file_system_id, region_name or fs_token")
        normalized = _normalize_prefix(prefix).strip("/")
        self._root = f"/{normalized}" if normalized else ""
        self._client = client or TiFSClient(file_system_id, fs_token=fs_token, region_name=region_name)
        self._search_strategy = search_strategy

    @property
    def client(self) -> TiFSClient:
        """The client used to run ``ti``."""
        return self._client

    def _path(self, key: str) -> str:
        return f"{self._root}/{_normalize_key(key)}"

    def _dir(self, rel: str) -> str:
        path = f"{self._root}/{rel}" if rel else self._root
        return path or "/"

    async def write(self, key: str, data: bytes) -> None:
        """Store data under key, overwriting any existing value.

        Raises:
            StorageError: If the write or indexing fails.
        """
        path = self._path(key)
        try:
            await self._client.write(path, data)
        except TiFSError as error:
            raise StorageError(f"Failed to write '{key}' to TiDB Cloud Filesystem") from error
        if self._search_strategy is not None:
            try:
                await self._search_strategy.index(self, _normalize_key(key), data)
            except Exception as error:
                raise StorageError(f"Wrote '{key}' but indexing failed") from error

    async def read(self, key: str) -> bytes | None:
        """Return the bytes stored under key, or None if the key does not exist.

        Raises:
            StorageError: If the read fails for a reason other than a missing key.
        """
        path = self._path(key)
        try:
            return await self._client.read(path)
        except TiFSError as error:
            raise StorageError(f"Failed to read '{key}' from TiDB Cloud Filesystem") from error

    async def delete(self, key: str) -> None:
        """Delete key. A no-op if it does not exist.

        Raises:
            StorageError: If the delete fails.
        """
        path = self._path(key)
        try:
            await self._client.delete(path)
        except TiFSError as error:
            raise StorageError(f"Failed to delete '{key}' from TiDB Cloud Filesystem") from error

    async def list(self, query: str = "") -> builtins.list[str]:
        """List keys starting with ``query``, sorted ascending, with the storage prefix stripped.

        Walks directories with ``list-files`` (``find-files`` is capped at 100 results).
        Keys must not contain spaces: ``ti`` truncates them in listings.

        Raises:
            StorageError: If the listing fails.
        """
        prefix = _normalize_prefix(query)
        # Start from the deepest directory the prefix fully names.
        start = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
        keys: builtins.list[str] = []
        pending = [start]
        try:
            while pending:
                level, pending = pending, []
                listings = await asyncio.gather(*(self._client.list_dir(self._dir(d)) for d in level))
                for directory, entries in zip(level, listings, strict=True):
                    for entry in entries:
                        key = f"{directory}/{entry['name']}" if directory else entry["name"]
                        if entry.get("is_dir"):
                            if key.startswith(prefix) or prefix.startswith(key + "/"):
                                pending.append(key)
                        elif key.startswith(prefix):
                            keys.append(key)
        except TiFSError as error:
            raise StorageError(f"Failed to list keys with prefix '{query}' from TiDB Cloud Filesystem") from error
        return sorted(keys)

    async def search(self, query: str) -> builtins.list[StorageSearchResult]:
        """Search stored content.

        Delegates to the configured search strategy. Without one, uses TiDB Cloud
        Filesystem's full-text and semantic index (at most 20 results; scores are
        derived from rank).

        Raises:
            StorageError: If the search fails.
        """
        if self._search_strategy is not None:
            return await self._search_strategy.search(self, query)
        base = self._root or "/"
        try:
            entries = await self._client.search(query, path=base)
        except TiFSError as error:
            raise StorageError("Search failed in TiDB Cloud Filesystem") from error
        results: builtins.list[StorageSearchResult] = []
        for rank, entry in enumerate(entries):
            key = posixpath.relpath(entry.path, base)
            if key.startswith(".."):
                continue
            score = entry.score if entry.score is not None else 1.0 / (rank + 1)
            results.append(StorageSearchResult(key=key, score=score))
        return results

    def namespace(self, prefix: str) -> TiDBFilesystemStorage | _NamespacedStorage:
        """Return a view of this storage with all keys prefixed.

        The view is a storage rooted at the namespace's directory, so its ``search()``
        is scoped there. A generic prefixing view would search the whole root and
        filter afterwards, losing matches beyond the service's 20-result cap. With a
        ``search_strategy``, the generic view is kept so the strategy sees one index.
        """
        if self._search_strategy is not None:
            return _NamespacedStorage(self, prefix)
        normalized = _normalize_prefix(prefix).strip("/")
        root = f"{self._root}/{normalized}" if normalized else self._root
        view = TiDBFilesystemStorage(prefix=root, client=self._client)
        # Tell Strands this view is already scoped, like its own namespaced views.
        view._namespaced = _NAMESPACED
        return view
