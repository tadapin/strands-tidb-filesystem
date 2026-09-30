"""Async wrapper around the ``ti`` CLI for TiDB Cloud Filesystem.

``ti`` is the only officially supported interface to TiDB Cloud Filesystem, so
every remote operation in this package goes through it. Credentials are passed
to the child process through its environment, never on the command line.

Contract notes (ti-cli, verified against source; re-check with Phase 0 fixtures):

- Every path is a flag; positional arguments are rejected.
- Success output is JSON on stdout (``read-file`` writes raw bytes instead).
- Errors are printed to stderr as ``ti [ERROR]: <message>`` with no JSON body.
  Companion failures exit 1 with the server's message; see ``_map_error`` for
  how messages are classified (missing path, auth, permission, quota, conflict, transient).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

from .errors import (
    TiFSAuthError,
    TiFSConflictError,
    TiFSError,
    TiFSNotFoundError,
    TiFSPermissionError,
    TiFSQuotaError,
    TiFSTransientError,
    TiNotInstalledError,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120.0
_ERROR_PREFIX = "ti [ERROR]:"
_INSTALL_HINT = (
    "Install it with `curl -fsSL https://github.com/tidbcloud/ti-cli/releases/latest/download/install.sh "
    "| sh -s -- --yes`, or call strands_tidb_filesystem.ensure_ti()."
)


@dataclass
class RemoteEntry:
    """A file returned by ``ti fs`` search or find."""

    path: str
    name: str | None = None
    size: int | None = None
    score: float | None = None


def _parse_json(stdout: bytes) -> Any:
    """Parse ``ti``'s JSON output, tolerating companion noise printed before it."""
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Some commands let the companion print to stdout before ti's JSON; keep the trailing object.
        start = text.rfind("\n{")
        if start != -1:
            return json.loads(text[start + 1 :])
        raise


_BANNER_KEYS = ("component:", "version:", "git_hash:", "git_branch:", "build_time:", "go_version:")


def _error_message(stderr: bytes) -> str:
    """The error text after ``ti [ERROR]:``, which can span several lines, minus version banners."""
    lines = [line.strip() for line in stderr.decode("utf-8", errors="replace").splitlines() if line.strip()]
    for i, line in enumerate(lines):
        if line.startswith(_ERROR_PREFIX):
            lines = [line[len(_ERROR_PREFIX) :].strip(), *lines[i + 1 :]]
            break
    detail = [line for line in lines if line and not line.startswith(_BANNER_KEYS)]
    return " / ".join(detail) if detail else "ti command failed"


# Classification of `ti` / `ti-drive9` failures. Companion errors are always exit 1 (2 for usage) with
# the server's message, so matching is mostly on text. "not found" alone is NOT enough: messages such
# as "tenant not found" or "upload not found" do not mean the requested path is missing, and treating
# them as such would make sync mode wipe the working copy.
_NOT_A_MISSING_PATH = re.compile(
    r"tenant not found|was not found in the selected region|upload not found|secret not found|"
    r"token not found|context \S+ not found|no such file or directory|companion"
)
_MISSING_PATH = re.compile(r": not found\b|\bhttp 404\b|remote resource not found")
_AUTH = re.compile(
    r"invalid api key|missing tenant scope|\bhttp 401\b|token (?:expired|revoked)|invalid fs token|"
    r"missing (?:fs )?token|does not match"
)
_PERMISSION = re.compile(
    r"fs access denied|scoped token cannot|\bhttp 403\b|tenant is unavailable|owner-only|forbidden|"
    r"permission denied: token"
)
_QUOTA = re.compile(r"quota exceeded|upload too large|requested quota exceeds|\bhttp 40[2]\b|\bhttp 413\b|\b507\b")
_CONFLICT = re.compile(r"directory not empty|revision conflict|upload is not active|already exists|\bhttp 409\b")
_TRANSIENT = re.compile(
    r"backend unavailable|\bhttp 50[234]\b|connection reset|\beof\b|i/o timeout|timeout awaiting|temporarily"
)


# Remote (":/a/b") and local ("/a/b") paths inside a message, up to a ": " separator, whitespace or a quote.
_PATH = re.compile(r':?/[^\s"]*?(?=:(?:\s|$)|[\s"]|$)')


def _without_paths(message: str) -> str:
    """Replace paths in ``message`` so words inside them (e.g. ``/forbidden/``) don't affect classification."""
    return _PATH.sub("<path>", message)


def _map_error(command: str, exit_code: int, stderr: bytes) -> TiFSError:
    message = _error_message(stderr)
    lowered = _without_paths(message).lower()
    cls: type[TiFSError] = TiFSError
    if exit_code == 3 or _AUTH.search(lowered):
        cls = TiFSAuthError
    elif _PERMISSION.search(lowered):
        cls = TiFSPermissionError
    elif _QUOTA.search(lowered):
        cls = TiFSQuotaError
    elif _MISSING_PATH.search(lowered) and not _NOT_A_MISSING_PATH.search(lowered):
        cls = TiFSNotFoundError
    elif _CONFLICT.search(lowered):
        cls = TiFSConflictError
    elif _TRANSIENT.search(lowered):
        cls = TiFSTransientError
    return cls(message, command=command, exit_code=exit_code)


# Legacy TDC_* twins make `ti` exit 2 when they disagree with the TI_* value we inject.
_TWINS = {
    "TI_FS_TOKEN": "TDC_FS_TOKEN",
    "TI_REGION_CODE": "TDC_REGION_CODE",
    "TI_FS_FILE_SYSTEM_ID": "TDC_FS_FILE_SYSTEM_ID",
}


@dataclass
class _Command:
    """One ``ti`` invocation: arguments, optional stdin, and how to read stdout."""

    args: list[str]
    stdin: bytes | None = None
    raw: bool = False
    timeout: float | None = None
    # Errors of these types are returned as the result instead of raised (e.g. "missing" -> None).
    tolerate: tuple[type[TiFSError], ...] = field(default_factory=tuple)

    @property
    def name(self) -> str:
        return " ".join(self.args[:2])


class TiFSClient:
    """Run ``ti fs`` commands against one file system, from async or sync code.

    Every operation has an async form (``read``) and a blocking form (``read_sync``)
    that share the same command construction and error handling. Blocking forms exist
    for Strands interfaces that are synchronous, such as ``SessionRepository``.

    Args:
        file_system_id: Use the token saved in the ``ti`` profile for this file system
            (``ti fs import-file-system-token``). Defaults to ``TI_FS_FILE_SYSTEM_ID``.
        fs_token: File system token. Defaults to ``TI_FS_TOKEN``.
        region_name: Region code such as ``aws-us-east-1``. Defaults to ``TI_REGION_CODE``,
            then the ``ti`` profile.
        ti_path: Path to the ``ti`` binary. Defaults to ``ti`` on ``PATH`` or ``~/.ti/bin/ti``.
        timeout: Default timeout in seconds for each command.
    """

    def __init__(
        self,
        file_system_id: str | None = None,
        *,
        fs_token: str | None = None,
        region_name: str | None = None,
        ti_path: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Initialize the client."""
        self._file_system_id = file_system_id
        self._fs_token = fs_token
        self._region_name = region_name
        self._ti_path = ti_path
        self.timeout = timeout

    @property
    def ti_path(self) -> str:
        """Resolve the ``ti`` binary, raising :class:`TiNotInstalledError` if it is missing."""
        if self._ti_path:
            return self._ti_path
        found = shutil.which("ti") or _existing(os.path.expanduser("~/.ti/bin/ti"))
        if not found:
            raise TiNotInstalledError(f"The `ti` CLI was not found. {_INSTALL_HINT}")
        self._ti_path = found
        return found

    def env(self) -> dict[str, str]:
        """Environment for ``ti`` child processes, with credentials injected."""
        env = dict(os.environ)
        # Telemetry is a synchronous POST (up to 3s) after every command; opt out unless set explicitly.
        env.setdefault("TI_TELEMETRY", "off")
        if self._fs_token and not self._file_system_id:
            # An inherited file system ID that does not match the token makes ti exit 3.
            env.pop("TI_FS_FILE_SYSTEM_ID", None)
            env.pop("TDC_FS_FILE_SYSTEM_ID", None)
        if self._file_system_id and not self._fs_token:
            # Use the profile token for this file system, not an unrelated inherited one.
            env.pop("TI_FS_TOKEN", None)
            env.pop("TDC_FS_TOKEN", None)
        injected = {
            "TI_FS_TOKEN": self._fs_token,
            "TI_REGION_CODE": self._region_name,
            "TI_FS_FILE_SYSTEM_ID": self._file_system_id,
        }
        for name, value in injected.items():
            if value:
                env[name] = value
                env.pop(_TWINS[name], None)
        return env

    # ---- Execution ----

    async def run(self, *args: str, stdin: bytes | None = None, timeout: float | None = None, raw: bool = False) -> Any:
        """Run ``ti <args>`` and return parsed JSON, or raw stdout bytes if ``raw``.

        Raises:
            TiFSError: A subclass describing the failure (not found, auth, permission, ...).
        """
        return await self._run(_Command(list(args), stdin=stdin, raw=raw, timeout=timeout))

    def run_sync(self, *args: str, stdin: bytes | None = None, timeout: float | None = None, raw: bool = False) -> Any:
        """Blocking form of :meth:`run`."""
        return self._run_sync(_Command(list(args), stdin=stdin, raw=raw, timeout=timeout))

    async def _run(self, cmd: _Command) -> Any:
        timeout = cmd.timeout or self.timeout
        proc = await asyncio.create_subprocess_exec(
            self.ti_path,
            *cmd.args,
            stdin=asyncio.subprocess.PIPE if cmd.stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(cmd.stdin), timeout)
        except asyncio.TimeoutError as e:
            _kill(proc)
            await proc.wait()
            raise TiFSError(f"timed out after {timeout}s", command=cmd.name) from e
        except BaseException:
            # Cancelled (or interrupted): stop `ti` so a write or delete can't complete behind the caller's back.
            _kill(proc)
            raise
        return self._result(cmd, proc.returncode or 0, stdout, stderr)

    def _run_sync(self, cmd: _Command) -> Any:
        timeout = cmd.timeout or self.timeout
        try:
            proc = subprocess.run(
                [self.ti_path, *cmd.args],
                input=cmd.stdin,
                stdin=None if cmd.stdin is not None else subprocess.DEVNULL,
                capture_output=True,
                env=self.env(),
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise TiFSError(f"timed out after {timeout}s", command=cmd.name) from e
        return self._result(cmd, proc.returncode, proc.stdout, proc.stderr)

    @staticmethod
    def _result(cmd: _Command, returncode: int, stdout: bytes, stderr: bytes) -> Any:
        if returncode != 0:
            error = _map_error(cmd.name, returncode, stderr)
            if isinstance(error, cmd.tolerate):
                return error
            raise error
        return stdout if cmd.raw else _parse_json(stdout)

    # ---- Commands (shared by the async and sync forms) ----

    @staticmethod
    def _read(path: str) -> _Command:
        return _Command(["fs", "read-file", "--path", path], raw=True, tolerate=(TiFSNotFoundError,))

    @staticmethod
    def _write(path: str, data: bytes) -> _Command:
        # Single-file uploads always overwrite and create parent directories (`--overwrite` is not forwarded).
        return _Command(["fs", "copy-file", "--from-stdin", "--to-remote", path], stdin=data)

    @staticmethod
    def _delete(path: str, recursive: bool) -> _Command:
        args = ["fs", "delete-file", "--path", path]
        if recursive:
            args.append("--recursive")
        return _Command(args, tolerate=(TiFSNotFoundError,))

    @staticmethod
    def _stat(path: str) -> _Command:
        return _Command(["fs", "describe-file", "--path", path], tolerate=(TiFSNotFoundError,))

    @staticmethod
    def _list_dir(path: str) -> _Command:
        return _Command(["fs", "list-files", "--path", path], tolerate=(TiFSNotFoundError,))

    # ---- Async API ----

    async def read(self, path: str) -> bytes | None:
        """Read a remote file. Returns ``None`` if it does not exist."""
        data: bytes | None = _value_or_none(await self._run(self._read(path)))
        return data

    async def write(self, path: str, data: bytes) -> None:
        """Write a remote file, replacing it and creating parent directories."""
        await self._run(self._write(path, data))

    async def delete(self, path: str, *, recursive: bool = False) -> bool:
        """Delete a file, or a directory tree with ``recursive``. Returns False if it did not exist."""
        return _value_or_none(await self._run(self._delete(path, recursive))) is not None

    async def stat(self, path: str) -> dict[str, Any] | None:
        """Describe a remote file (``size_bytes``, ``is_dir``, ``mtime``...), or ``None`` if missing."""
        info: dict[str, Any] | None = _value_or_none(await self._run(self._stat(path)))
        return info

    async def list_dir(self, path: str = "/") -> list[dict[str, Any]]:
        """List a directory: entries with ``name``, ``size_bytes`` and ``is_dir``. Empty if missing.

        ``ti`` truncates names that contain spaces.
        """
        return _entries_of(await self._run(self._list_dir(path)))

    async def search(self, pattern: str, *, path: str = "/", limit: int = 0) -> list[RemoteEntry]:
        """Full-text / semantic search over file content (``ti fs grep``). Empty if ``path`` is missing.

        The companion returns at most 20 results; ``limit`` can only lower that. Results
        carry paths only, and paths containing spaces are truncated.
        """
        # `ti` passes the pattern on to its file system component as a plain argument, and that
        # component strips anything that looks like its own flags (e.g. "--json", "--layer=x")
        # wherever it appears, which would turn the path into the query and search from "/".
        # A leading space keeps the query's meaning but no longer matches those flags.
        if pattern.startswith("-"):
            pattern = " " + pattern
        args = ["fs", "search-file-content", "--pattern", pattern, "--path", path]
        if limit:
            args += ["--limit", str(limit)]
        return _results(await self._run(_Command(args, tolerate=(TiFSNotFoundError,))))

    async def find(
        self,
        *,
        path: str = "/",
        name: str | None = None,
        tag: str | None = None,
        newer: str | None = None,
        older: str | None = None,
        min_size: int | None = None,
        max_size: int | None = None,
        limit: int = 0,
    ) -> list[RemoteEntry]:
        """Find files (not directories) by name glob, tag, date (YYYY-MM-DD) or size. Empty if ``path`` is missing.

        Raises ``ValueError`` if a filter value starts with ``-``.

        The server returns at most 100 results, ordered by path; ``limit`` can only lower
        that. Paths containing spaces are truncated.
        """
        args = ["fs", "find-files", "--path", path]
        for flag, value in (
            ("--file-name-pattern", name),
            ("--tag", tag),
            ("--newer", newer),
            ("--older", older),
            ("--min-size-bytes", min_size),
            ("--max-size-bytes", max_size),
        ):
            if value is not None:
                text = str(value)
                # Values starting with "-" can be taken as flags further down the chain (see search()).
                if text.startswith("-"):
                    raise ValueError(f"{flag} must not start with '-': {text!r}")
                args += [flag, text]
        if limit:
            args += ["--limit", str(limit)]
        return _results(await self._run(_Command(args, tolerate=(TiFSNotFoundError,))))

    # ---- Blocking API ----

    def read_sync(self, path: str) -> bytes | None:
        """Blocking form of :meth:`read`."""
        data: bytes | None = _value_or_none(self._run_sync(self._read(path)))
        return data

    def write_sync(self, path: str, data: bytes) -> None:
        """Blocking form of :meth:`write`."""
        self._run_sync(self._write(path, data))

    def delete_sync(self, path: str, *, recursive: bool = False) -> bool:
        """Blocking form of :meth:`delete`."""
        return _value_or_none(self._run_sync(self._delete(path, recursive))) is not None

    def stat_sync(self, path: str) -> dict[str, Any] | None:
        """Blocking form of :meth:`stat`."""
        info: dict[str, Any] | None = _value_or_none(self._run_sync(self._stat(path)))
        return info

    def list_dir_sync(self, path: str = "/") -> list[dict[str, Any]]:
        """Blocking form of :meth:`list_dir`."""
        return _entries_of(self._run_sync(self._list_dir(path)))


def _value_or_none(result: Any) -> Any:
    return None if isinstance(result, TiFSError) else result


def _entries_of(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, TiFSError):
        return []
    entries: list[dict[str, Any]] = result.get("entries") or []
    return entries


def _existing(path: str) -> str | None:
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _results(result: Any) -> list[RemoteEntry]:
    if isinstance(result, TiFSError):  # searching a directory that does not exist yet
        return []
    return [
        RemoteEntry(path=r["path"], name=r.get("name"), size=r.get("size_bytes"), score=r.get("score"))
        for r in result.get("results") or []
    ]
