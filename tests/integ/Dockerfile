# Linux image for the live tests.
#   docker build -f tests/integ/Dockerfile -t strands-tidb-fs-integ .
#   docker run --rm -e TI_FS_TOKEN -e TI_REGION_CODE strands-tidb-fs-integ
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://github.com/tidbcloud/ti-cli/releases/latest/download/install.sh \
    | sh -s -- --yes --install-dir /usr/local/bin

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir -e '.[dev]'
COPY tests ./tests

ENV TI_TELEMETRY=off
CMD ["pytest", "-m", "integ", "-v", "-rs", "--tb=short"]
