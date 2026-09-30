# Strands agent for Amazon Bedrock AgentCore Runtime, storing sessions in TiDB Cloud Filesystem.
# The image contains only this directory; strands-tidb-filesystem is installed from GitHub.
#
# AgentCore Runtime requires linux/arm64 images. Build from this directory:
#   docker buildx build --platform linux/arm64 --provenance=false -t my-agent .
FROM python:3.12-slim

# git: to install strands-tidb-filesystem from GitHub. curl: for the ti-cli installer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# ti-cli (`ti`), installed onto PATH. Only its file commands are used, so no FUSE or extra privileges.
RUN curl -fsSL https://github.com/tidbcloud/ti-cli/releases/latest/download/install.sh \
    | sh -s -- --yes --install-dir /usr/local/bin

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent.py .

# Numeric user avoids AgentCore's overlay issue with many-layer images; ti needs a writable HOME.
USER 1000
ENV HOME=/tmp TI_TELEMETRY=off
EXPOSE 8080
CMD ["python", "agent.py"]
