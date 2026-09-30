"""Tests for TiDBFilesystemSessionManager, including parity with S3SessionManager."""

import json

import boto3
import pytest
from moto import mock_aws
from strands import Agent
from strands.session import S3SessionManager
from strands.types.exceptions import SessionException
from strands.types.session import Session, SessionAgent, SessionMessage, SessionType

from strands_tidb_filesystem import TiDBFilesystemSessionManager


def _manager(fake_ti, session_id="s1", prefix="sessions/"):
    return TiDBFilesystemSessionManager(session_id, prefix=prefix, client=fake_ti.client)


def _converse(manager):
    """Drive the manager the way an agent does, without calling a model."""
    agent = Agent(agent_id="assistant", session_manager=manager, callback_handler=None)
    for message in (
        {"role": "user", "content": [{"text": "hello"}]},
        {"role": "assistant", "content": [{"text": "hi there"}]},
    ):
        agent.messages.append(message)
        manager.append_message(message, agent)
        manager.sync_agent(agent)
    return agent


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and ".mounts" not in p.parts)


def test_layout_matches_s3_session_manager(fake_ti):
    _converse(_manager(fake_ti))
    assert _tree(fake_ti.remote) == [
        "sessions/session_s1/agents/agent_assistant/agent.json",
        "sessions/session_s1/agents/agent_assistant/messages/message_0.json",
        "sessions/session_s1/agents/agent_assistant/messages/message_1.json",
        "sessions/session_s1/session.json",
    ]


@mock_aws
def test_same_files_and_json_as_s3(fake_ti):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="parity-bucket")
    _converse(S3SessionManager("s1", bucket="parity-bucket", prefix="sessions/", region_name="us-east-1"))
    _converse(_manager(fake_ti))

    s3 = boto3.client("s3", region_name="us-east-1")
    s3_objects = {
        o["Key"]: json.loads(s3.get_object(Bucket="parity-bucket", Key=o["Key"])["Body"].read())
        for o in s3.list_objects_v2(Bucket="parity-bucket")["Contents"]
    }
    ours = {key: json.loads((fake_ti.remote / key).read_text()) for key in _tree(fake_ti.remote)}
    assert sorted(s3_objects) == sorted(ours)

    def strip(data):
        return {k: v for k, v in data.items() if k not in ("created_at", "updated_at")}

    for key in s3_objects:
        assert strip(s3_objects[key]) == strip(ours[key]), key


def test_conversation_is_restored_by_a_new_process(fake_ti):
    _converse(_manager(fake_ti))
    restored = Agent(agent_id="assistant", session_manager=_manager(fake_ti), callback_handler=None)
    assert [m["content"][0]["text"] for m in restored.messages] == ["hello", "hi there"]


def test_updates_reuse_known_created_at_without_reading(fake_ti):
    manager = _manager(fake_ti)
    _converse(manager)
    calls = fake_ti.commands()
    # The only read is the session lookup at startup; update_agent never re-reads agent.json.
    assert calls.count("read-file") == 1
    reads = [c["argv"] for c in fake_ti.calls() if c["argv"][1] == "read-file"]
    assert all(not argv[-1].endswith("agent.json") for argv in reads)


def test_update_after_restart_preserves_created_at(fake_ti):
    manager = _manager(fake_ti)
    manager.create_agent("s1", SessionAgent(agent_id="a", state={}, conversation_manager_state={}))
    original = manager.read_agent("s1", "a").created_at

    fresh = _manager(fake_ti)
    fresh.update_agent("s1", SessionAgent(agent_id="a", state={"x": 1}, conversation_manager_state={}))
    stored = fresh.read_agent("s1", "a")
    assert stored.created_at == original and stored.state == {"x": 1}


def test_list_messages_sorted_with_pagination(fake_ti):
    manager = _manager(fake_ti)
    manager.create_agent("s1", SessionAgent(agent_id="a", state={}, conversation_manager_state={}))
    for i in (10, 2, 1, 0):
        manager.create_message(
            "s1", "a", SessionMessage(message={"role": "user", "content": [{"text": str(i)}]}, message_id=i)
        )
    ids = [m.message_id for m in manager.list_messages("s1", "a")]
    assert ids == [0, 1, 2, 10]
    assert [m.message_id for m in manager.list_messages("s1", "a", limit=2, offset=1)] == [1, 2]
    assert manager.list_messages("s1", "missing") == []


def test_create_existing_session_fails(fake_ti):
    manager = _manager(fake_ti)
    with pytest.raises(SessionException, match="already exists"):
        manager.create_session(Session(session_id="s1", session_type=SessionType.AGENT))


def test_delete_session(fake_ti):
    manager = _manager(fake_ti)
    _converse(manager)
    manager.delete_session("s1")
    assert _tree(fake_ti.remote) == []
    with pytest.raises(SessionException, match="does not exist"):
        manager.delete_session("s1")


def test_missing_records_and_bad_json(fake_ti):
    manager = _manager(fake_ti)
    assert manager.read_agent("s1", "nobody") is None
    with pytest.raises(SessionException, match="does not exist"):
        manager.update_agent("s1", SessionAgent(agent_id="nobody", state={}, conversation_manager_state={}))
    (fake_ti.remote / "sessions" / "session_s1" / "agents" / "agent_bad").mkdir(parents=True)
    (fake_ti.remote / "sessions" / "session_s1" / "agents" / "agent_bad" / "agent.json").write_text("{nope")
    with pytest.raises(SessionException, match="Invalid JSON"):
        manager.read_agent("s1", "bad")


def test_identifiers_are_validated(fake_ti):
    with pytest.raises(ValueError):
        _manager(fake_ti, session_id="a/b")


def test_backend_errors_become_session_exceptions(fake_ti, monkeypatch):
    manager = _manager(fake_ti)
    monkeypatch.setenv("FAKE_TI_FAIL", "permission")
    with pytest.raises(SessionException) as info:
        manager.read_session("s1")
    assert isinstance(info.value.__cause__, PermissionError)


def test_client_cannot_be_combined_with_connection_args(fake_ti):
    with pytest.raises(SessionException):
        TiDBFilesystemSessionManager("s1", file_system_id="fs-1", client=fake_ti.client)
