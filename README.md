# strands-tidb-filesystem

Store [Strands Agents](https://strandsagents.com) sessions, memory and offloaded context in
[TiDB Cloud Filesystem](https://www.pingcap.com/tidb/tidb-cloud-filesystem/). The two classes are drop-in
replacements for Strands' S3 backends:

| Strands (S3) | This package |
|---|---|
| `S3Storage("bucket", prefix="agents/")` | `TiDBFilesystemStorage("<file-system-id>", prefix="agents/")` |
| `S3SessionManager("session-id", bucket="bucket", prefix="agents/")` | `TiDBFilesystemSessionManager("session-id", file_system_id="<file-system-id>", prefix="agents/")` |

A file system plays the role of a bucket, and each key or session record is stored as a file. Because it
is a file system, you can also browse, copy and search the stored data with the `ti` CLI. Search is
full-text and semantic.

> [!NOTE]
> TiDB Cloud Filesystem is in public preview, and so is this package.

## Recommended setup

Use `SnapshotSessionManager` with `TiDBFilesystemStorage` as the agent-level storage:

```python
from strands import Agent
from strands.session import SnapshotSessionManager
from strands_tidb_filesystem import TiDBFilesystemStorage

agent = Agent(
    storage=TiDBFilesystemStorage(prefix="agents/"),
    session_manager=SnapshotSessionManager("user-42"),
)
```

Agent-level storage is shared by every Strands subsystem that persists data:

- `SnapshotSessionManager`: conversation and agent state
- `FileMemoryStore`: long-term memory
- the context offloader

Each subsystem writes under its own namespace. The agent state is saved as one snapshot per invocation,
so each turn costs a single write.

Use `TiDBFilesystemSessionManager` when you are replacing an existing `S3SessionManager`, or when you
need multi-agent (graph/swarm) state. It writes the same files as `S3SessionManager`, so sessions can be
copied between the two. Like `S3SessionManager`, it writes every message individually, so a turn costs
several writes instead of one.

## What is (and isn't) stored

Like the S3 backends, this package stores only what Strands writes through these interfaces:
conversations, agent state, memory and offloaded context. Files that the agent's tools write to the local
disk, such as `shell` output, are not captured. On ephemeral hosts such as Amazon Bedrock AgentCore
Runtime, those files disappear with the session.

## Install

Installing takes two steps: this Python package, and the [ti-cli](https://github.com/tidbcloud/ti-cli) (`ti`)
that it runs. `ti` is the supported interface to TiDB Cloud Filesystem. No FUSE and no mount are needed.

### 1. Install the Python package

The package is distributed from GitHub only; it is not published on PyPI. Install it straight from the
Git repository, pinned to a release tag:

```bash
# pip
pip install "strands-tidb-filesystem @ git+https://github.com/tadapin/strands-tidb-filesystem.git@v0.1.0"

# uv: add it to your project's dependencies
uv add "git+https://github.com/tadapin/strands-tidb-filesystem.git" --tag v0.1.0

# uv: install it into the current environment
uv pip install "strands-tidb-filesystem @ git+https://github.com/tadapin/strands-tidb-filesystem.git@v0.1.0"

# or, from a local checkout of this repository
pip install /path/to/strands-tidb-filesystem
```

In `requirements.txt`:

```text
strands-agents>=1.56.0
strands-tidb-filesystem @ git+https://github.com/tadapin/strands-tidb-filesystem.git@v0.1.0
```

- `git` must be available where you install.
- Pin a tag (`@v0.1.0`) or a commit so builds are reproducible. The package version comes from the Git
  tag: `@v0.1.0` installs version `0.1.0`. A commit after the latest tag installs a development version
  such as `0.1.1.dev1+gea62143`.
- Python 3.10 or later is required. The package depends only on `strands-agents`.

### 2. Install ti-cli

```bash
curl -fsSL https://github.com/tidbcloud/ti-cli/releases/latest/download/install.sh | sh -s -- --yes
export PATH="$HOME/.ti/bin:$PATH"
```

The package finds `ti` on `PATH`, or in `~/.ti/bin`. If you can't install it ahead of time (for example,
with a zip-based deployment), call `strands_tidb_filesystem.ensure_ti()` at startup. It runs the same
installer. `ensure_ti(version="v0.2.6")` pins the version. If a different version of `ti` is already
installed, it raises `TiVersionMismatchError` instead of replacing it.

## Docker

Install ti-cli and the package in the image, and pass the credentials at run time. The container needs no
extra privileges.

```dockerfile
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# ti-cli, installed onto PATH. Add `--version vX.Y.Z` to pin it.
RUN curl -fsSL https://github.com/tidbcloud/ti-cli/releases/latest/download/install.sh \
    | sh -s -- --yes --install-dir /usr/local/bin

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt   # includes the Git URL of this package (see above)
COPY . .

# ti keeps its configuration and logs under $HOME/.ti, so HOME must be writable.
ENV HOME=/tmp TI_TELEMETRY=off
USER 1000
CMD ["python", "agent.py"]
```

```bash
docker build -t my-agent .
docker run --rm -e TI_FS_TOKEN -e TI_REGION_CODE my-agent
```

- `git` is needed only when installing the package from its Git repository.
- For Amazon Bedrock AgentCore Runtime, build for `linux/arm64`:
  `docker buildx build --platform linux/arm64 ...`.
- See [`examples/agentcore/Dockerfile`](examples/agentcore/Dockerfile) for a complete image.
- Set the token and region on the container or runtime as environment variables. Don't bake them into
  the image.

## Credentials

Pass one of the following. TiDB Cloud API keys are not needed.

| Option | How |
|---|---|
| A file system token | `TI_FS_TOKEN`, or `fs_token=` |
| A file system ID whose token is saved in the `ti` profile | `TI_FS_FILE_SYSTEM_ID`, or `file_system_id=` (first run `ti fs import-file-system-token`) |

Also set the region with `TI_REGION_CODE` or `region_name=`.

For agents, prefer a scoped, expiring token limited to the prefix they use:

```bash
export TI_FS_TOKEN="$(ti fs generate-file-system-scoped-token --ttl 24h \
  --allow /agents:read,list,search,write,delete --query fs_token --output text)"
```

## API

### `TiDBFilesystemStorage`

```python
TiDBFilesystemStorage(
    file_system_id=None,   # like S3Storage's bucket; defaults to TI_FS_FILE_SYSTEM_ID or the token's file system
    *,
    prefix="",             # like S3Storage's prefix
    region_name=None,
    fs_token=None,
    client=None,           # a preconfigured TiFSClient (like boto_session)
    search_strategy=None,  # same meaning as in S3Storage
)
```

It implements the Strands `Storage` protocol: `write`, `read`, `delete`, `list`, `search` and `namespace`.

- Missing keys read as `None`.
- Failures raise `StorageError`, with the cause attached.
- Without a `search_strategy`, `search()` uses TiDB Cloud Filesystem's full-text and semantic index (up
  to 20 results).

### `TiDBFilesystemSessionManager`

```python
TiDBFilesystemSessionManager(session_id, file_system_id=None, prefix="", region_name=None, fs_token=None, client=None)
```

- It is a `RepositorySessionManager`, like `S3SessionManager`.
- Failures raise `SessionException`, with the underlying error as the cause.
- The layout is the same as `S3SessionManager`'s:

```
/<prefix>/session_<session_id>/session.json
                              /agents/agent_<agent_id>/agent.json
                              /agents/agent_<agent_id>/messages/message_<n>.json
                              /multi_agents/multi_agent_<id>/multi_agent.json
```

### Search tools

`make_search_files()` and `make_find_files()` create agent tools over stored files. Pass `root=` to limit
what they can see.

- `search_files` searches by meaning or keywords, including text extracted from PDFs and images (up to
  20 results).
- `find_files` searches by name, date, size or tag (up to 100 results).

```python
storage = TiDBFilesystemStorage(prefix="agents/")
agent = Agent(storage=storage, tools=[make_search_files(storage.client, root="/agents")])
```

### Errors

`TiFSClient` classifies `ti` failures into:

- `TiFSNotFoundError`
- `TiFSAuthError`: the token is invalid, expired or revoked
- `TiFSPermissionError`: the token's scope does not allow the operation
- `TiFSQuotaError`
- `TiFSConflictError`
- `TiFSTransientError`

`TiDBFilesystemStorage` and `TiDBFilesystemSessionManager` wrap these in the Strands exception types.

## Amazon Bedrock AgentCore Runtime

See [`examples/agentcore`](examples/agentcore) for a complete example:

- Install `ti` in the arm64 image, or call `ensure_ti()`.
- Pass `TI_FS_TOKEN` and `TI_REGION_CODE` to the runtime.
- Use the runtime session ID as the Strands session ID.

Conversations then survive the microVM and can be resumed from any session, agent or machine that uses
the same file system.

The example was verified on AgentCore Runtime (microVM, ap-northeast-1, September 2026). After
`StopRuntimeSession` terminated the microVM, the next invocation with the same session ID restored the
conversation from TiDB Cloud Filesystem, while a different session ID started empty. Set
`BEDROCK_MODEL_ID` to use a model other than the Strands default.

## Limitations

- **One `ti` process per operation.** Each read, write, delete or listing runs one `ti` command, so fewer
  writes per turn (the snapshot-based setup) means less overhead.
- **Keys with spaces.** `ti` truncates names containing spaces in listings and search results, so avoid
  spaces in storage keys.
- **Pinning ti-cli.** The installer's `--version` pins the `ti` binary, but the file system component that
  ti-cli installs alongside it is always the latest release. Behaviour can therefore change between
  installs even with the same `--version`.

## Development

```bash
pip install -e ".[dev]"
hatch run prepare                                     # format, lint, typecheck, unit tests (uses a fake `ti`)
TI_FS_TOKEN=... TI_REGION_CODE=... pytest -m integ    # live tests; they only touch /strands-integ/<random>
TI_FS_FILE_SYSTEM_ID=... pytest -m integ              # same, using a token saved in the ti profile
```

The unit tests include a parity check that runs the same conversation through `S3SessionManager` (on
moto) and `TiDBFilesystemSessionManager`, and compares the stored files.

## License

Apache-2.0
