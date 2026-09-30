# Strands Agents on Amazon Bedrock AgentCore Runtime with TiDB Cloud Filesystem

This example stores the agent's conversation in TiDB Cloud Filesystem, so a conversation survives the
AgentCore microVM. It uses `SnapshotSessionManager` with `TiDBFilesystemStorage`, and uses the AgentCore
runtime session ID as the conversation ID.

The image contains only this directory:

- `strands-tidb-filesystem` is installed from GitHub (`requirements.txt`, pinned to `v0.1.0`).
- `ti` is installed with the official ti-cli installer.

No FUSE and no extra privileges are needed.

## Files

| File | Purpose |
|---|---|
| `agent.py` | `BedrockAgentCoreApp` entry point. Creates the agent per request and restores the session from TiDB Cloud Filesystem. |
| `requirements.txt` | `bedrock-agentcore`, `strands-agents`, and this package from GitHub |
| `Dockerfile` | `python:3.12-slim` with `git`, ti-cli and the requirements, running as UID 1000 |

## Configuration (runtime environment variables)

| Variable | Required | Description |
|---|---|---|
| `TI_FS_TOKEN` | yes | File system token. Prefer a scoped, expiring one (see below). |
| `TI_REGION_CODE` | yes | Region of the file system, e.g. `aws-ap-southeast-1`. |
| `BEDROCK_MODEL_ID` | no | Bedrock model or inference profile. Defaults to the Strands default model. |

Create a token that can only touch the prefix the agent uses (`/agents`):

```bash
ti fs generate-file-system-scoped-token --subject agentcore --ttl 24h \
  --allow /agents:read,list,search,write,delete --query fs_token --output text
```

## Deploy

Run these from this directory. `ACCOUNT` and `REGION` are your AWS account and AgentCore Region.

1. Build for `linux/arm64` and push to ECR:

   ```bash
   docker buildx build --platform linux/arm64 --provenance=false -t tifs-agentcore-sample .
   aws ecr create-repository --repository-name tifs-agentcore-sample
   aws ecr get-login-password | docker login -u AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com
   docker tag tifs-agentcore-sample $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/tifs-agentcore-sample:latest
   docker push $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/tifs-agentcore-sample:latest
   ```

2. Create an execution role that `bedrock-agentcore.amazonaws.com` can assume. It needs:

   - `ecr:BatchGetImage` and `ecr:GetDownloadUrlForLayer` on the repository, and `ecr:GetAuthorizationToken`.
   - `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`, `logs:DescribeLogStreams` and
     `logs:DescribeLogGroups` on `/aws/bedrock-agentcore/runtimes/*`.
   - `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` for the model you use.

   No TiDB Cloud permission is needed on AWS: access is controlled by `TI_FS_TOKEN`.

3. Create the runtime (boto3 shown, since older AWS CLI versions lack some AgentCore options):

   ```python
   import boto3

   boto3.client("bedrock-agentcore-control", region_name=REGION).create_agent_runtime(
       agentRuntimeName="tifs_agentcore_sample",
       roleArn=ROLE_ARN,
       agentRuntimeArtifact={"containerConfiguration": {"containerUri": IMAGE_URI}},
       networkConfiguration={"networkMode": "PUBLIC"},
       environmentVariables={
           "TI_FS_TOKEN": TOKEN,
           "TI_REGION_CODE": "aws-ap-southeast-1",
           "BEDROCK_MODEL_ID": "apac.amazon.nova-pro-v1:0",  # optional
       },
   )
   ```

   For production, keep the token in AWS Secrets Manager rather than a plain environment variable.

4. Invoke with a session ID, stop the session, and invoke again with the same ID:

   ```python
   import json, boto3

   rt = boto3.client("bedrock-agentcore", region_name=REGION)

   def ask(session_id, prompt):
       r = rt.invoke_agent_runtime(agentRuntimeArn=RUNTIME_ARN, runtimeSessionId=session_id,
                                   payload=json.dumps({"prompt": prompt}).encode())
       return json.loads(b"".join(r["response"]))["response"]

   sid = "demo-session-0000000000000000000000000001"  # at least 33 characters
   ask(sid, "My favourite database is TiDB. Please remember it.")
   rt.stop_runtime_session(agentRuntimeArn=RUNTIME_ARN, runtimeSessionId=sid)  # terminates the microVM
   ask(sid, "What is my favourite database?")  # a new microVM restores the conversation: "TiDB"
   ```

   The snapshot is stored at `/agents/session/<session-id>/scopes/agent/default/snapshots/snapshot_latest.json`:

   ```bash
   ti fs find-files --path /agents
   ```

## Verified

Verified on 2026-09-30 in ap-northeast-1 (microVM runtime), with `strands-tidb-filesystem` 0.1.0 from
GitHub, `strands-agents` 1.57.1 and `apac.amazon.nova-pro-v1:0`. The file system was in
`aws-ap-southeast-1`.

| Step | Result |
|---|---|
| Session A: "My favourite database is TiDB." | noted (9.6 s, including cold start) |
| Session A: "What is my favourite database?" | `TiDB` (1.4 s) |
| `StopRuntimeSession` for A, then the same question | `TiDB` (1.9 s): restored from TiDB Cloud Filesystem in a new microVM |
| Session B: same question | `UNKNOWN`: sessions are isolated |

## Clean up

Delete the runtime, the ECR repository, the IAM role and the runtime's log group
(`/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT`). Then remove the stored sessions if you no longer
need them:

```bash
ti fs delete-file --path /agents --recursive
```
