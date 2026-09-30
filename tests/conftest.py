"""Shared fixtures: a fake ``ti`` CLI backed by a temporary directory."""

import json
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from strands_tidb_filesystem import TiFSClient

FAKE_TI = Path(__file__).with_name("fake_ti.py")


@dataclass
class FakeTi:
    """Handle on the fake CLI: the remote root and its invocation log."""

    client: TiFSClient
    remote: Path
    log: Path

    def calls(self) -> list[dict]:
        """Every recorded invocation."""
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def commands(self) -> list[str]:
        """The ``fs`` subcommand of every invocation."""
        return [c["argv"][1] for c in self.calls()]


@pytest.fixture
def fake_ti(tmp_path, monkeypatch) -> FakeTi:
    """A :class:`TiFSClient` whose ``ti`` binary is the fake CLI."""
    remote = tmp_path / "remote"
    remote.mkdir()
    log = tmp_path / "ti.log"
    ti = tmp_path / "bin" / "ti"
    ti.parent.mkdir()
    ti.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE_TI} "$@"\n')
    ti.chmod(ti.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_TI_ROOT", str(remote))
    monkeypatch.setenv("FAKE_TI_LOG", str(log))
    monkeypatch.delenv("TI_FS_TOKEN", raising=False)
    client = TiFSClient(fs_token="secret-token", region_name="aws-us-east-1", ti_path=str(ti))
    return FakeTi(client=client, remote=remote, log=log)
