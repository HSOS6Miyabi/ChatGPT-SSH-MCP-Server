"""Full MCP HTTP discovery against the installed official SDK (network-free)."""

import asyncio
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest


@pytest.mark.asyncio
async def test_mcp_streamable_http_discovery(tmp_path):
    pytest.importorskip("mcp.client.session")
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env.update(MCP_HOST="127.0.0.1", MCP_PORT=str(port), MCP_TRANSPORT="http",
               SSH_MCP_TRANSFER_ROOT=str(tmp_path / "transfers"))
    env.pop("SSH_MCP_PROFILES_FILE", None)
    env.pop("MCP_BEARER_TOKEN", None)
    process = subprocess.Popen([sys.executable, "-m", "ssh_mcp"], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise AssertionError("MCP server exited during startup: " + process.stderr.read().decode())
            with socket.socket() as probe:
                probe.settimeout(0.1)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("MCP HTTP endpoint did not start")

        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                init = await session.initialize()
                assert init.serverInfo.name == "Remote SSH & SFTP"
                result = await session.list_tools()
                assert len(result.tools) == 15
                assert {t.name for t in result.tools} >= {"ssh_execute", "sftp_read", "sftp_write"}
                info = await session.call_tool("ssh_profiles")
                assert not info.isError
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()