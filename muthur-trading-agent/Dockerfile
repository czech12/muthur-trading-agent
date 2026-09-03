FROM python:3.12-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

FROM python:3.12-slim

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin agent

COPY --from=builder --chown=agent:agent /root/.local /home/agent/.local
COPY --chown=agent:agent src/ /app/src/
# config/agent.yaml is NOT copied in - it ships via a Kubernetes ConfigMap
# mounted over /app/config/agent.yaml (see fluxcd/muthur-trading-agent/), which
# always overrides whatever the image would otherwise have here.

RUN date -u +"%Y.%m.%d-%H:%M:%S" > /app/BUILD_INFO

ENV PATH=/home/agent/.local/bin:$PATH \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_PATH=/app/config/agent.yaml \
    HEARTBEAT_PATH=/tmp/heartbeat \
    UV_CACHE_DIR=/tmp/uv-cache

WORKDIR /app
USER agent

# `uvx` fetches and caches alpaca-mcp-server (and its pinned fastmcp version, see
# mcp_client.py) into UV_CACHE_DIR on first run rather than baking them into this
# image - this means the pod needs outbound network access at startup, same as pip
# does at build time here.
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s \
  CMD python -c "import os,time,sys; p=os.environ['HEARTBEAT_PATH']; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p)<90 else 1)"

ENTRYPOINT ["python", "src/main.py"]
