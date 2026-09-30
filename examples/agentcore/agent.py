"""Strands agent on Amazon Bedrock AgentCore Runtime that keeps its conversation,
memory and offloaded context in TiDB Cloud Filesystem instead of S3.

Environment (set on the runtime; prefer a scoped, expiring token):
    TI_FS_TOKEN     ti fs generate-file-system-scoped-token --subject agentcore --ttl 24h \
                        --allow /agents:read,list,search,write,delete --query fs_token --output text
    TI_REGION_CODE  e.g. aws-us-east-1
    BEDROCK_MODEL_ID  optional; defaults to the Strands default model

Payload: {"prompt": "..."}; the AgentCore runtime session id is the conversation id.
"""

import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel
from strands.session import SnapshotSessionManager

from strands_tidb_filesystem import TiDBFilesystemStorage, ensure_ti, make_search_files

app = BedrockAgentCoreApp()
storage = TiDBFilesystemStorage(prefix="agents/")  # like S3Storage("bucket", prefix="agents/")
model = BedrockModel(model_id=os.environ["BEDROCK_MODEL_ID"]) if os.environ.get("BEDROCK_MODEL_ID") else None


@app.entrypoint
async def invoke(payload: dict, context) -> dict:
    ensure_ti()  # no-op when the image already has ti (see Dockerfile)
    agent = Agent(
        model=model,
        storage=storage,
        session_manager=SnapshotSessionManager(context.session_id),
        tools=[make_search_files(storage.client, root="/agents")],
    )
    result = await agent.invoke_async(payload.get("prompt", "Hello"))
    return {"response": str(result)}


if __name__ == "__main__":
    app.run()
