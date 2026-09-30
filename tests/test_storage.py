"""Tests for TiDBFilesystemStorage, mirroring the S3Storage contract."""

import pytest
from strands.storage import Storage
from strands.types.exceptions import StorageError

from strands_tidb_filesystem import TiDBFilesystemStorage, TiFSClient


@pytest.fixture
def storage(fake_ti):
    return TiDBFilesystemStorage(client=fake_ti.client, prefix="agents/")


def test_satisfies_storage_protocol(storage):
    assert isinstance(storage, Storage)


def test_client_cannot_be_combined_with_connection_args(fake_ti):
    with pytest.raises(StorageError):
        TiDBFilesystemStorage("fs-1", client=fake_ti.client)


def test_constructor_mirrors_s3storage():
    storage = TiDBFilesystemStorage("fs-1", prefix="a/b/", region_name="aws-us-east-1")
    env = storage.client.env()
    assert env["TI_FS_FILE_SYSTEM_ID"] == "fs-1" and env["TI_REGION_CODE"] == "aws-us-east-1"


async def test_round_trip_under_prefix(storage, fake_ti):
    await storage.write("sessions/s1/snapshot.json", b'{"a": 1}')
    assert (fake_ti.remote / "agents" / "sessions" / "s1" / "snapshot.json").exists()
    assert await storage.read("sessions/s1/snapshot.json") == b'{"a": 1}'
    assert await storage.read("sessions/none") is None
    await storage.delete("sessions/s1/snapshot.json")
    await storage.delete("sessions/s1/snapshot.json")
    assert await storage.read("sessions/s1/snapshot.json") is None


async def test_no_prefix_uses_root(fake_ti):
    storage = TiDBFilesystemStorage(client=fake_ti.client)
    await storage.write("k", b"v")
    assert (fake_ti.remote / "k").read_bytes() == b"v"
    assert await storage.list() == ["k"]


async def test_list_by_prefix_sorted_and_stripped(storage):
    for key in ("sessions/b/x", "sessions/a/x", "offload/1"):
        await storage.write(key, b"v")
    assert await storage.list("sessions/") == ["sessions/a/x", "sessions/b/x"]
    assert await storage.list("") == ["offload/1", "sessions/a/x", "sessions/b/x"]
    assert await storage.list("sessions/a") == ["sessions/a/x"]
    assert await storage.list("missing/") == []


async def test_list_is_not_capped_at_find_limit(storage):
    for i in range(120):
        await storage.write(f"offload/{i:03d}", b"v")
    keys = await storage.list("offload/")
    assert len(keys) == 120 and keys[0] == "offload/000"


async def test_search_uses_file_system_index(storage):
    await storage.write("notes/1", b"customer prefers TiDB")
    await storage.write("notes/2", b"unrelated")
    results = await storage.search("tidb")
    assert [r.key for r in results] == ["notes/1"]


async def test_search_strategy_is_used_when_given(fake_ti):
    calls = []

    class Strategy:
        async def index(self, storage, key, data):
            calls.append(("index", key))

        async def search(self, storage, query):
            calls.append(("search", query))
            return []

    storage = TiDBFilesystemStorage(client=fake_ti.client, search_strategy=Strategy())
    await storage.write("a/b", b"x")
    await storage.search("q")
    assert calls == [("index", "a/b"), ("search", "q")]


async def test_namespace_scopes_keys(storage, fake_ti):
    view = storage.namespace("memory/agent-memory")
    await view.write("facts.md", b"likes tea")
    assert (fake_ti.remote / "agents" / "memory" / "agent-memory" / "facts.md").exists()
    assert await view.list() == ["facts.md"]


async def test_rejects_traversal_keys(storage):
    with pytest.raises(StorageError):
        await storage.write("../escape", b"x")


async def test_backend_errors_become_storage_errors(storage, monkeypatch):
    monkeypatch.setenv("FAKE_TI_FAIL", "permission")
    with pytest.raises(StorageError) as info:
        await storage.write("k", b"v")
    assert isinstance(info.value.__cause__, PermissionError)


def test_client_defaults_from_environment(monkeypatch):
    monkeypatch.setenv("TI_FS_FILE_SYSTEM_ID", "fs-env")
    assert isinstance(TiDBFilesystemStorage().client, TiFSClient)


async def test_snapshot_session_manager_round_trip(storage):
    from strands import Agent
    from strands.session import SnapshotSessionManager

    manager = SnapshotSessionManager("snap-1", storage=storage)
    agent = Agent(agent_id="assistant", session_manager=manager, callback_handler=None)
    agent.messages.append({"role": "user", "content": [{"text": "remember me"}]})
    await manager.save_snapshot(agent, is_latest=True)

    restored = Agent(
        agent_id="assistant",
        session_manager=SnapshotSessionManager("snap-1", storage=storage),
        callback_handler=None,
    )
    assert restored.messages[0]["content"][0]["text"] == "remember me"


async def test_search_on_empty_prefix_returns_nothing(fake_ti):
    storage = TiDBFilesystemStorage(client=fake_ti.client, prefix="never-written/")
    assert await storage.search("anything") == []


async def test_namespace_search_is_scoped_to_its_directory(storage, fake_ti):
    from strands.storage.storage import _resolve_namespace

    await storage.write("other/agent/notes.md", b"tidb tidb tidb")
    view = _resolve_namespace(storage, "memory/agent-memory")
    assert _resolve_namespace(view, "ignored") is view  # already scoped, like Strands' own views
    await view.write("facts.md", b"likes tidb")

    results = await view.search("tidb")
    assert [r.key for r in results] == ["facts.md"]
    search_calls = [c["argv"] for c in fake_ti.calls() if c["argv"][1] == "search-file-content"]
    path = search_calls[-1][search_calls[-1].index("--path") + 1]
    assert path == "/agents/memory/agent-memory"


async def test_namespace_with_search_strategy_keeps_generic_view(fake_ti):
    from strands.storage.storage import _NamespacedStorage

    class Strategy:
        async def index(self, storage, key, data):
            pass

        async def search(self, storage, query):
            return []

    storage = TiDBFilesystemStorage(client=fake_ti.client, search_strategy=Strategy())
    assert isinstance(storage.namespace("x"), _NamespacedStorage)
