FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    SSH_MCP_KNOWN_HOSTS=/home/mcp/.ssh/known_hosts \
    SSH_MCP_KEY_ROOT=/home/mcp/.ssh \
    SSH_MCP_TRANSFER_ROOT=/home/mcp/transfers \
    SSH_MCP_PROFILES_FILE=/home/mcp/profiles.json

WORKDIR /opt/ssh-mcp
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir . && \
    useradd --uid 10001 --create-home --shell /usr/sbin/nologin mcp && \
    mkdir -p /home/mcp/.ssh /home/mcp/transfers && \
    chown -R mcp:mcp /home/mcp

USER mcp
EXPOSE 8000
CMD ["chatgpt-ssh-mcp"]