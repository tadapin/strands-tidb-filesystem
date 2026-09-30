"""Session manager on TiDB Cloud Filesystem, a drop-in for ``S3SessionManager``."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, cast

from strands import _identifier
from strands.session.repository_session_manager import RepositorySessionManager
from strands.session.session_repository import SessionRepository
from strands.types.exceptions import SessionException
from strands.types.session import Session, SessionAgent, SessionMessage

from .client import TiFSClient
from .errors import TiFSError

if TYPE_CHECKING:
    from strands.multiagent.base import MultiAgentBase

logger = logging.getLogger(__name__)

SESSION_PREFIX = "session_"
AGENT_PREFIX = "agent_"
MESSAGE_PREFIX = "message_"
MULTI_AGENT_PREFIX = "multi_agent_"


class TiDBFilesystemSessionManager(RepositorySessionManager, SessionRepository):
    """Session manager that stores sessions in TiDB Cloud Filesystem.

    Mirrors :class:`strands.session.S3SessionManager`, including its layout, so
    sessions can be copied between the two backends:

    ```bash
    /<prefix>/
    └── session_<session_id>/
        ├── session.json
        ├── agents/
        │   └── agent_<agent_id>/
        │       ├── agent.json
        │       └── messages/
        │           └── message_<n>.json
        └── multi_agents/
            └── multi_agent_<id>/multi_agent.json
    ```

    Like ``S3SessionManager``, each message is written individually, and each write
    runs one ``ti`` command. To write once per invocation instead, prefer
    ``SnapshotSessionManager`` with :class:`~strands_tidb_filesystem.TiDBFilesystemStorage`.

    Args:
        session_id: ID for the session. Must not contain path separators.
        file_system_id: File system to use (the counterpart of an S3 bucket), with the
            token saved in the ``ti`` profile. Defaults to ``TI_FS_FILE_SYSTEM_ID``, or
            the file system encoded in ``fs_token``.
        prefix: Path prefix for storage organization.
        region_name: Region code such as ``aws-us-east-1``. Defaults to ``TI_REGION_CODE``.
        fs_token: File system token. Defaults to ``TI_FS_TOKEN``.
        client: Pre-configured :class:`TiFSClient`. Cannot be combined with
            ``file_system_id``, ``region_name`` or ``fs_token``.
        **kwargs: Additional keyword arguments for future extensibility.
    """

    def __init__(
        self,
        session_id: str,
        file_system_id: str | None = None,
        prefix: str = "",
        region_name: str | None = None,
        fs_token: str | None = None,
        client: TiFSClient | None = None,
        **kwargs: Any,
    ):
        """Initialize the session manager and load or create the session."""
        if client is not None and (file_system_id or region_name or fs_token):
            raise SessionException("Cannot specify client together with file_system_id, region_name or fs_token")
        self.client = client or TiFSClient(file_system_id, fs_token=fs_token, region_name=region_name)
        self.prefix = prefix
        # created_at of records this instance created or read, so updates can skip a read.
        self._created_at: dict[str, str] = {}
        super().__init__(session_id=session_id, session_repository=self)

    # ---- Paths ----

    def _get_session_path(self, session_id: str) -> str:
        session_id = _identifier.validate(session_id, _identifier.Identifier.SESSION)
        prefix = self.prefix.strip("/")
        return f"/{prefix}/{SESSION_PREFIX}{session_id}/" if prefix else f"/{SESSION_PREFIX}{session_id}/"

    def _get_agent_path(self, session_id: str, agent_id: str) -> str:
        agent_id = _identifier.validate(agent_id, _identifier.Identifier.AGENT)
        return f"{self._get_session_path(session_id)}agents/{AGENT_PREFIX}{agent_id}/"

    def _get_message_path(self, session_id: str, agent_id: str, message_id: int) -> str:
        if not isinstance(message_id, int):
            raise ValueError(f"message_id=<{message_id}> | message id must be an integer")
        return f"{self._get_agent_path(session_id, agent_id)}messages/{MESSAGE_PREFIX}{message_id}.json"

    def _get_multi_agent_path(self, session_id: str, multi_agent_id: str) -> str:
        multi_agent_id = _identifier.validate(multi_agent_id, _identifier.Identifier.AGENT)
        return f"{self._get_session_path(session_id)}multi_agents/{MULTI_AGENT_PREFIX}{multi_agent_id}/"

    # ---- I/O ----

    def _read_json(self, path: str) -> dict[str, Any] | None:
        try:
            data = self.client.read_sync(path)
        except TiFSError as e:
            raise SessionException(f"TiDB Cloud Filesystem error reading {path}: {e}") from e
        if data is None:
            return None
        try:
            return cast(dict[str, Any], json.loads(data.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise SessionException(f"Invalid JSON in {path}: {e}") from e

    def _write_json(self, path: str, data: dict[str, Any]) -> None:
        content = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        try:
            self.client.write_sync(path, content)
        except TiFSError as e:
            raise SessionException(f"Failed to write {path}: {e}") from e

    def _previous_created_at(self, path: str, read: Any) -> str | None:
        """``created_at`` of an existing record, from the cache or by reading it."""
        if path in self._created_at:
            return self._created_at[path]
        previous = read()
        return None if previous is None else cast(str, previous.created_at)

    # ---- Sessions ----

    def create_session(self, session: Session, **kwargs: Any) -> Session:
        """Create a new session."""
        path = f"{self._get_session_path(session.session_id)}session.json"
        try:
            exists = self.client.stat_sync(path) is not None
        except TiFSError as e:
            raise SessionException(f"TiDB Cloud Filesystem error checking session existence: {e}") from e
        if exists:
            raise SessionException(f"Session {session.session_id} already exists")
        self._write_json(path, session.to_dict())
        return session

    def read_session(self, session_id: str, **kwargs: Any) -> Session | None:
        """Read session data."""
        data = self._read_json(f"{self._get_session_path(session_id)}session.json")
        return None if data is None else Session.from_dict(data)

    def delete_session(self, session_id: str, **kwargs: Any) -> None:
        """Delete a session and all its data."""
        path = self._get_session_path(session_id)
        try:
            deleted = self.client.delete_sync(path.rstrip("/"), recursive=True)
        except TiFSError as e:
            raise SessionException(f"TiDB Cloud Filesystem error deleting session {session_id}: {e}") from e
        if not deleted:
            raise SessionException(f"Session {session_id} does not exist")
        self._created_at = {k: v for k, v in self._created_at.items() if not k.startswith(path)}

    # ---- Agents ----

    def create_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        """Create a new agent."""
        path = f"{self._get_agent_path(session_id, session_agent.agent_id)}agent.json"
        self._write_json(path, session_agent.to_dict())
        self._created_at[path] = session_agent.created_at

    def read_agent(self, session_id: str, agent_id: str, **kwargs: Any) -> SessionAgent | None:
        """Read agent data."""
        path = f"{self._get_agent_path(session_id, agent_id)}agent.json"
        data = self._read_json(path)
        if data is None:
            return None
        agent = SessionAgent.from_dict(data)
        self._created_at[path] = agent.created_at
        return agent

    def update_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        """Update agent data, preserving its creation timestamp."""
        agent_id = session_agent.agent_id
        path = f"{self._get_agent_path(session_id, agent_id)}agent.json"
        created_at = self._previous_created_at(path, lambda: self.read_agent(session_id, agent_id))
        if created_at is None:
            raise SessionException(f"Agent {agent_id} in session {session_id} does not exist")
        session_agent.created_at = created_at
        self._write_json(path, session_agent.to_dict())

    # ---- Messages ----

    def create_message(self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs: Any) -> None:
        """Create a new message."""
        path = self._get_message_path(session_id, agent_id, session_message.message_id)
        self._write_json(path, session_message.to_dict())
        self._created_at[path] = session_message.created_at

    def read_message(self, session_id: str, agent_id: str, message_id: int, **kwargs: Any) -> SessionMessage | None:
        """Read message data."""
        path = self._get_message_path(session_id, agent_id, message_id)
        data = self._read_json(path)
        if data is None:
            return None
        message = SessionMessage.from_dict(data)
        self._created_at[path] = message.created_at
        return message

    def update_message(self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs: Any) -> None:
        """Update message data, preserving its creation timestamp."""
        message_id = session_message.message_id
        path = self._get_message_path(session_id, agent_id, message_id)
        created_at = self._previous_created_at(path, lambda: self.read_message(session_id, agent_id, message_id))
        if created_at is None:
            raise SessionException(f"Message {message_id} does not exist")
        session_message.created_at = created_at
        self._write_json(path, session_message.to_dict())

    def list_messages(
        self, session_id: str, agent_id: str, limit: int | None = None, offset: int = 0, **kwargs: Any
    ) -> list[SessionMessage]:
        """List an agent's messages sorted by message id, with pagination."""
        directory = f"{self._get_agent_path(session_id, agent_id)}messages"
        try:
            entries = self.client.list_dir_sync(directory)
        except TiFSError as e:
            raise SessionException(f"TiDB Cloud Filesystem error reading messages: {e}") from e
        indexed: list[tuple[int, str]] = []
        for entry in entries:
            name = entry["name"]
            if not entry.get("is_dir") and name.startswith(MESSAGE_PREFIX) and name.endswith(".json"):
                try:
                    indexed.append((int(name[len(MESSAGE_PREFIX) : -5]), f"{directory}/{name}"))
                except ValueError:
                    continue
        paths = [path for _, path in sorted(indexed)]
        paths = paths[offset : offset + limit] if limit is not None else paths[offset:]
        if not paths:
            return []
        if len(paths) == 1:
            results = [self._read_json(paths[0])]
        else:
            with ThreadPoolExecutor(max_workers=min(8, len(paths))) as executor:
                results = list(executor.map(self._read_json, paths))
        messages = []
        for path, data in zip(paths, results, strict=True):
            if data:
                message = SessionMessage.from_dict(data)
                self._created_at[path] = message.created_at
                messages.append(message)
        return messages

    # ---- Multi-agent ----

    def create_multi_agent(self, session_id: str, multi_agent: MultiAgentBase, **kwargs: Any) -> None:
        """Create a new multi-agent state."""
        path = f"{self._get_multi_agent_path(session_id, multi_agent.id)}multi_agent.json"
        self._write_json(path, multi_agent.serialize_state())

    def read_multi_agent(self, session_id: str, multi_agent_id: str, **kwargs: Any) -> dict[str, Any] | None:
        """Read multi-agent state."""
        return self._read_json(f"{self._get_multi_agent_path(session_id, multi_agent_id)}multi_agent.json")

    def update_multi_agent(self, session_id: str, multi_agent: MultiAgentBase, **kwargs: Any) -> None:
        """Update multi-agent state."""
        path = f"{self._get_multi_agent_path(session_id, multi_agent.id)}multi_agent.json"
        try:
            exists = self.client.stat_sync(path) is not None
        except TiFSError as e:
            raise SessionException(f"TiDB Cloud Filesystem error reading {path}: {e}") from e
        if not exists:
            raise SessionException(f"MultiAgent state {multi_agent.id} in session {session_id} does not exist")
        self._write_json(path, multi_agent.serialize_state())
