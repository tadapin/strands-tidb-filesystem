"""Resume a conversation from any machine, container or AgentCore session.

Run it twice (anywhere with the same file system) with the same session id:

    python examples/session_resume.py demo "My favourite database is TiDB."
    python examples/session_resume.py demo "What is my favourite database?"
"""

import asyncio
import sys

from strands import Agent
from strands.session import SnapshotSessionManager

from strands_tidb_filesystem import TiDBFilesystemStorage


async def main(session_id: str, prompt: str) -> None:
    agent = Agent(
        storage=TiDBFilesystemStorage(prefix="agents/"),
        session_manager=SnapshotSessionManager(session_id),
    )
    await agent.invoke_async(prompt)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], " ".join(sys.argv[2:])))
