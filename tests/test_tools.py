"""Tests for the search_files / find_files agent tools, including root scoping."""

import pytest
from strands import Agent

from strands_tidb_filesystem import RemoteEntry, make_find_files, make_search_files


def _text(result):
    return result["content"][0]["text"]


@pytest.fixture
def seeded(fake_ti):
    fake_ti.client.write_sync("/agents/alice/notes.md", b"alice likes tidb")
    fake_ti.client.write_sync("/agents/bob/secret.md", b"bob likes tidb")
    return fake_ti


def test_search_is_scoped_to_root(seeded):
    agent = Agent(tools=[make_search_files(seeded.client, root="/agents/alice")], callback_handler=None)
    text = _text(agent.tool.search_files(query="tidb"))
    assert text == "/agents/alice/notes.md"


def test_paths_outside_root_are_refused(seeded):
    agent = Agent(tools=[make_search_files(seeded.client, root="/agents/alice")], callback_handler=None)
    assert "outside" in _text(agent.tool.search_files(query="tidb", path="../bob"))


@pytest.mark.parametrize("query", ["--json", "--layer=main", "--auth=server"])
def test_flag_like_queries_are_neutralised(seeded, query):
    agent = Agent(tools=[make_search_files(seeded.client, root="/agents/alice")], callback_handler=None)
    agent.tool.search_files(query=query)
    argv = [c["argv"] for c in seeded.calls() if c["argv"][1] == "search-file-content"][-1]
    assert argv[argv.index("--pattern") + 1] == " " + query
    assert argv[argv.index("--path") + 1] == "/agents/alice"


def test_results_outside_root_are_dropped(seeded, monkeypatch):
    async def leaky_search(*args, **kwargs):
        return [RemoteEntry(path="/agents/bob/secret.md"), RemoteEntry(path="/agents/alice/notes.md")]

    monkeypatch.setattr(seeded.client, "search", leaky_search)
    agent = Agent(tools=[make_search_files(seeded.client, root="/agents/alice")], callback_handler=None)
    assert _text(agent.tool.search_files(query="tidb")) == "/agents/alice/notes.md"


def test_find_rejects_flag_like_values(seeded):
    agent = Agent(tools=[make_find_files(seeded.client, root="/agents/alice")], callback_handler=None)
    assert "must not start with '-'" in _text(agent.tool.find_files(name_pattern="--auth=local"))
    assert "find-files" not in seeded.commands()


def test_find_is_scoped_to_root(seeded):
    agent = Agent(tools=[make_find_files(seeded.client, root="/agents/alice")], callback_handler=None)
    assert _text(agent.tool.find_files(name_pattern="*.md")) == "/agents/alice/notes.md"
