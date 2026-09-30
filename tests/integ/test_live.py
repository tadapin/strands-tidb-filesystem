"""Live checks against a real TiDB Cloud Filesystem.

Run with ``pytest -m integ`` after setting ``TI_FS_TOKEN`` (plus ``TI_REGION_CODE``), or
``TI_FS_FILE_SYSTEM_ID`` for a file system whose token is saved in the ``ti`` profile.
Tests only write under ``/strands-integ/<random>`` and delete it afterwards, so a scoped
token (``--allow /strands-integ:read,list,search,write,delete``) is enough.
"""

import os
import shutil
import time
import uuid

import pytest
from strands import Agent
from strands.session import SnapshotSessionManager

from strands_tidb_filesystem import TiDBFilesystemSessionManager, TiDBFilesystemStorage, TiFSClient


def _ti_installed() -> bool:
    return bool(shutil.which("ti") or os.access(os.path.expanduser("~/.ti/bin/ti"), os.X_OK))


pytestmark = [
    pytest.mark.integ,
    pytest.mark.skipif(
        not ((os.environ.get("TI_FS_TOKEN") or os.environ.get("TI_FS_FILE_SYSTEM_ID")) and _ti_installed()),
        reason="needs ti, and TI_FS_TOKEN or TI_FS_FILE_SYSTEM_ID (with a token saved in the ti profile)",
    ),
]


@pytest.fixture
def prefix():
    client = TiFSClient()
    base = f"strands-integ/{uuid.uuid4().hex[:8]}"
    yield base
    client.delete_sync(f"/{base}", recursive=True)


async def test_client_contract(prefix):
    client = TiFSClient()
    path = f"/{prefix}/a.bin"
    await client.write(path, b"\x00first")
    await client.write(path, b"\x00second")  # overwrite
    assert await client.read(path) == b"\x00second"
    assert await client.read(f"/{prefix}/missing") is None
    assert (await client.stat(path))["size_bytes"] == 7
    assert client.read_sync(path) == b"\x00second"


async def test_storage_contract(prefix):
    storage = TiDBFilesystemStorage(prefix=prefix)
    await storage.write("sessions/s1/snapshot.json", b'{"topic": "TiDB vector search"}')
    await storage.write("sessions/s2/snapshot.json", b"{}")
    assert await storage.read("sessions/s1/snapshot.json") == b'{"topic": "TiDB vector search"}'
    assert await storage.read("sessions/none") is None
    assert await storage.list("sessions/") == ["sessions/s1/snapshot.json", "sessions/s2/snapshot.json"]
    await storage.delete("sessions/s2/snapshot.json")
    assert await storage.list("sessions/") == ["sessions/s1/snapshot.json"]


def _converse(manager):
    agent = Agent(agent_id="assistant", session_manager=manager, callback_handler=None)
    for message in (
        {"role": "user", "content": [{"text": "hello"}]},
        {"role": "assistant", "content": [{"text": "hi there"}]},
    ):
        agent.messages.append(message)
        manager.append_message(message, agent)
        manager.sync_agent(agent)


def test_session_manager_round_trip_and_latency(prefix):
    started = time.monotonic()
    _converse(TiDBFilesystemSessionManager("s1", prefix=prefix))
    elapsed = time.monotonic() - started
    restored = Agent(
        agent_id="assistant",
        session_manager=TiDBFilesystemSessionManager("s1", prefix=prefix),
        callback_handler=None,
    )
    assert [m["content"][0]["text"] for m in restored.messages] == ["hello", "hi there"]
    print(f"\n2-message conversation persisted in {elapsed:.1f}s")


async def test_snapshot_session_manager_with_storage(prefix):
    storage = TiDBFilesystemStorage(prefix=prefix)
    manager = SnapshotSessionManager("snap-1", storage=storage)
    agent = Agent(agent_id="assistant", session_manager=manager, callback_handler=None)
    agent.messages.append({"role": "user", "content": [{"text": "remember me"}]})
    started = time.monotonic()
    await manager.save_snapshot(agent, is_latest=True)
    print(f"\nsnapshot saved in {time.monotonic() - started:.1f}s")

    restored = Agent(
        agent_id="assistant",
        session_manager=SnapshotSessionManager("snap-1", storage=TiDBFilesystemStorage(prefix=prefix)),
        callback_handler=None,
    )
    assert restored.messages[0]["content"][0]["text"] == "remember me"
