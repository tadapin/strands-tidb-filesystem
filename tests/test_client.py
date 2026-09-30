"""Tests for TiFSClient against the fake ti CLI."""

import pytest

from strands_tidb_filesystem import TiFSClient, TiFSError, TiFSNotFoundError, TiFSPermissionError, TiNotInstalledError


async def test_write_then_read_round_trips_bytes(fake_ti):
    data = bytes(range(256))
    await fake_ti.client.write("/dir/blob.bin", data)
    assert await fake_ti.client.read("/dir/blob.bin") == data


async def test_token_is_passed_in_env_not_argv(fake_ti):
    await fake_ti.client.write("/a.txt", b"x")
    call = fake_ti.calls()[0]
    assert call["token"] == "secret-token"
    assert all("secret-token" not in arg for arg in call["argv"])


async def test_missing_paths_are_none_or_empty(fake_ti):
    assert await fake_ti.client.read("/nope.txt") is None
    assert await fake_ti.client.stat("/nope.txt") is None
    assert await fake_ti.client.list_dir("/nope") == []
    assert await fake_ti.client.delete("/nope.txt") is False
    with pytest.raises(TiFSNotFoundError):
        await fake_ti.client.run("fs", "read-file", "--path", "/nope.txt", raw=True)


def test_sync_api_matches_async(fake_ti):
    client = fake_ti.client
    client.write_sync("/s/a.json", b"{}")
    assert client.read_sync("/s/a.json") == b"{}"
    assert client.stat_sync("/s/a.json")["size_bytes"] == 2
    assert [e["name"] for e in client.list_dir_sync("/s")] == ["a.json"]
    assert client.delete_sync("/s", recursive=True) is True
    assert client.read_sync("/s/a.json") is None


async def test_permission_error_mapping(fake_ti, monkeypatch):
    monkeypatch.setenv("FAKE_TI_FAIL", "permission")
    with pytest.raises(PermissionError) as info:
        await fake_ti.client.read("/a.txt")
    assert isinstance(info.value, TiFSPermissionError)
    assert info.value.exit_code == 4


async def test_search_and_find_return_entries(fake_ti):
    await fake_ti.client.write("/docs/a.md", b"TiDB vector search")
    await fake_ti.client.write("/docs/b.py", b"print('hi')")
    hits = await fake_ti.client.search("vector", path="/docs")
    assert [h.path for h in hits] == ["/docs/a.md"]
    found = await fake_ti.client.find(path="/docs", name="*.py")
    assert [f.path for f in found] == ["/docs/b.py"]


async def test_usage_errors_are_tifs_errors(fake_ti):
    with pytest.raises(TiFSError) as info:
        await fake_ti.client.run("fs", "nonexistent-command")
    assert info.value.exit_code == 2


async def test_missing_binary_has_install_hint(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setenv("HOME", "/nonexistent")
    with pytest.raises(TiNotInstalledError, match="install.sh"):
        await TiFSClient().read("/a")


def test_companion_http_status_is_mapped():
    from strands_tidb_filesystem.client import _map_error

    err = _map_error("fs copy-file", 1, b'\nti [ERROR]: fs cp: stat remote source "/x": HTTP 404:\n')
    assert isinstance(err, TiFSNotFoundError)
    assert isinstance(_map_error("fs read-file", 1, b"ti [ERROR]: fs cat: HTTP 403: denied"), TiFSPermissionError)


def test_multiline_errors_keep_the_cause_and_drop_banners():
    from strands_tidb_filesystem.client import _error_message

    stderr = (
        b"ti [ERROR]: component: drive9 mount\nversion: 788ec76\ngit_hash: abc\ngo_version: go1.25.1\n"
        b"drive9: mount mode: webdav\nmount: drive9 mount: --profile=coding-agent requires --mode=fuse\n"
    )
    assert _error_message(stderr) == (
        "drive9: mount mode: webdav / mount: drive9 mount: --profile=coding-agent requires --mode=fuse"
    )


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (b'ti [ERROR]: fs cp: stat remote source "/x": HTTP 404:', "TiFSNotFoundError"),
        (b"ti [ERROR]: fs cat: read :/a.txt: not found", "TiFSNotFoundError"),
        # "not found" that is not a missing path must not look like one (sync would wipe the working copy).
        (b"ti [ERROR]: fs cp: tenant not found", "TiFSError"),
        (b"ti [ERROR]: fs cp: upload not found", "TiFSError"),
        (b"ti [ERROR]: fs cp: open /ws/a: no such file or directory", "TiFSError"),
        (b'ti [ERROR]: fs cp: context "/ws/notes" not found; run: drive9 ctx ls', "TiFSError"),
        (b"ti [ERROR]: fs ls: invalid API key", "TiFSAuthError"),
        (b"ti [ERROR]: fs cp: fs access denied", "TiFSPermissionError"),
        (b"ti [ERROR]: fs cp: scoped token cannot write /x", "TiFSPermissionError"),
        (b"ti [ERROR]: fs cp: tenant storage quota exceeded", "TiFSQuotaError"),
        (b"ti [ERROR]: fs rm: directory not empty", "TiFSConflictError"),
        (b"ti [ERROR]: fs ls: storage backend unavailable; contact support", "TiFSTransientError"),
    ],
)
def test_error_classification(stderr, expected):
    from strands_tidb_filesystem.client import _map_error

    assert type(_map_error("fs x", 1, stderr)).__name__ == expected


def test_exit_code_3_is_an_auth_error():
    from strands_tidb_filesystem.client import _map_error
    from strands_tidb_filesystem.errors import TiFSAuthError

    assert isinstance(_map_error("fs x", 3, b"ti [ERROR]: invalid FS token format"), TiFSAuthError)


def test_env_drops_conflicting_legacy_and_inherited_values(monkeypatch):
    monkeypatch.setenv("TDC_FS_TOKEN", "old")
    monkeypatch.setenv("TI_FS_FILE_SYSTEM_ID", "other-fs")
    env = TiFSClient(fs_token="new", ti_path="ti").env()
    assert env["TI_FS_TOKEN"] == "new"
    assert "TDC_FS_TOKEN" not in env and "TI_FS_FILE_SYSTEM_ID" not in env
    assert env["TI_TELEMETRY"] == "off"

    monkeypatch.setenv("TI_FS_TOKEN", "unrelated")
    env = TiFSClient("fs-1", ti_path="ti").env()
    assert env["TI_FS_FILE_SYSTEM_ID"] == "fs-1" and "TI_FS_TOKEN" not in env


@pytest.mark.parametrize("word", ["forbidden", "companion", "eof", "already exists", "does not match", "507"])
def test_words_inside_paths_do_not_change_classification(word):
    from strands_tidb_filesystem.client import _map_error

    path = f"/{word.replace(' ', '_')}/a.txt"
    err = _map_error("fs read-file", 1, f"ti [ERROR]: fs cat: read :{path}: not found".encode())
    assert isinstance(err, TiFSNotFoundError), word
    err = _map_error("fs copy-file", 1, f'ti [ERROR]: fs cp: stat remote source "{path}": HTTP 404:'.encode())
    assert isinstance(err, TiFSNotFoundError), word


async def test_search_and_find_in_missing_directory_are_empty(fake_ti):
    assert await fake_ti.client.search("anything", path="/does-not-exist") == []
    assert await fake_ti.client.find(path="/does-not-exist") == []


async def test_cancelled_write_stops_ti(fake_ti, monkeypatch):
    import asyncio

    monkeypatch.setenv("FAKE_TI_SLEEP", "1.5")
    task = asyncio.create_task(fake_ti.client.write("/late.txt", b"should not land"))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(2.0)
    assert not (fake_ti.remote / "late.txt").exists()


def _fake_installed_ti(tmp_path, version="0.2.6"):
    ti = tmp_path / "ti"
    ti.write_text(f'#!/bin/sh\necho "ti {version} (abc, 2026-09-25T00:00:00Z, linux/arm64)"\n')
    ti.chmod(0o755)
    return tmp_path


def test_ensure_ti_accepts_matching_version(tmp_path):
    from strands_tidb_filesystem import ensure_ti

    install_dir = _fake_installed_ti(tmp_path)
    assert ensure_ti(version="0.2.6", install_dir=str(install_dir)) == str(install_dir / "ti")
    assert ensure_ti(version="v0.2.6", install_dir=str(install_dir)) == str(install_dir / "ti")
    assert ensure_ti(install_dir=str(install_dir)) == str(install_dir / "ti")


def test_ensure_ti_rejects_other_installed_version(tmp_path):
    from strands_tidb_filesystem import TiVersionMismatchError, ensure_ti

    install_dir = _fake_installed_ti(tmp_path, version="0.2.7")
    with pytest.raises(TiVersionMismatchError, match="v0.2.7, but v0.2.6 was requested"):
        ensure_ti(version="v0.2.6", install_dir=str(install_dir))
