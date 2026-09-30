"""TiDB Cloud Filesystem backends for Strands Agents: drop-ins for S3Storage and S3SessionManager."""

from strands_tidb_filesystem.client import RemoteEntry, TiFSClient
from strands_tidb_filesystem.errors import (
    TiFSAuthError,
    TiFSConflictError,
    TiFSError,
    TiFSNotFoundError,
    TiFSPermissionError,
    TiFSQuotaError,
    TiFSTransientError,
    TiNotInstalledError,
    TiVersionMismatchError,
)
from strands_tidb_filesystem.installer import ensure_ti
from strands_tidb_filesystem.session_manager import TiDBFilesystemSessionManager
from strands_tidb_filesystem.storage import TiDBFilesystemStorage
from strands_tidb_filesystem.tools import make_find_files, make_search_files

__all__ = [
    "RemoteEntry",
    "TiDBFilesystemSessionManager",
    "TiDBFilesystemStorage",
    "TiFSAuthError",
    "TiFSClient",
    "TiFSConflictError",
    "TiFSError",
    "TiFSNotFoundError",
    "TiFSPermissionError",
    "TiFSQuotaError",
    "TiFSTransientError",
    "TiNotInstalledError",
    "TiVersionMismatchError",
    "ensure_ti",
    "make_find_files",
    "make_search_files",
]
