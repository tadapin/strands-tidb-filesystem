# AgentCore Runtime requires linux/arm64 images:
#   docker buildx build --platform linux/arm64 -f examples/agentcore/Dockerfile -t my-agent .
FROM --platform=linux/arm64 python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# ti-cli (`ti`). Only its file commands are used, so no FUSE or extra privileges are needed.
RUN curl -fsSL https://github.com/tidbcloud/ti-cli/releases/latest/download/install.sh \
    | sh -s -- --yes --install-dir /usr/local/bin

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir /app -r examples/agentcore/requirements.txt

# Numeric user avoids AgentCore's overlay issue with many-layer images.
USER 1000
ENV HOME=/tmp TI_TELEMETRY=off
EXPOSE 8080
CMD ["python", "examples/agentcore/agent.py"]
