"""Check MCP tool registration and bearer-auth ASGI behavior using test doubles."""
import asyncio
import importlib
import sys
import types

import httpx
import pytest


@pytest.fixture
def server_module(paramiko_stub, monkeypatch):
    mcp_pkg = types.ModuleType("mcp")
    mcp_server_pkg = types.ModuleType("mcp.server")
    mcp_fastmcp_pkg = types.ModuleType("mcp.server.fastmcp")
    mcp_types_pkg = types.ModuleType("mcp.types")
    mcp_transport_pkg = types.ModuleType("mcp.server.transport_security")

    class TransportSecuritySettings:
        def __init__(self, **kwargs):
            self.options = kwargs

    mcp_transport_pkg.TransportSecuritySettings = TransportSecuritySettings

    class ToolAnnotations:
        def __init__(self, **kwargs):
            self.options = kwargs

    class FastMCP:
        def __init__(self, *args, **kwargs):
            self.tools = {}
            self.options = kwargs

        def tool(self, **kwargs):
            def deco(fn):
                self.tools[fn.__name__] = (fn, kwargs)
                return fn
            return deco

        def streamable_http_app(self):
            async def app(scope, receive, send):
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"OK"})
            return app

    mcp_fastmcp_pkg.FastMCP = FastMCP
    mcp_types_pkg.ToolAnnotations = ToolAnnotations
    monkeypatch.setitem(sys.modules, "mcp", mcp_pkg)
    monkeypatch.setitem(sys.modules, "mcp.server", mcp_server_pkg)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", mcp_fastmcp_pkg)
    monkeypatch.setitem(sys.modules, "mcp.types", mcp_types_pkg)
    monkeypatch.setitem(sys.modules, "mcp.server.transport_security", mcp_transport_pkg)
    sys.modules.pop("ssh_mcp.server", None)
    return importlib.import_module("ssh_mcp.server")


def test_tool_catalog(server_module):
    tools = server_module.mcp.tools
    assert len(tools) == 15
    assert set(tools) == {"ssh_profiles", "ssh_connect", "ssh_connections", "ssh_disconnect",
                          "ssh_execute", "sftp_list", "sftp_stat", "sftp_read", "sftp_write",
                          "sftp_mkdir", "sftp_rename", "sftp_delete", "sftp_chmod", "sftp_upload", "sftp_download"}
    assert tools["ssh_execute"][1]["annotations"].options["destructiveHint"]
    assert tools["sftp_read"][1]["annotations"].options["readOnlyHint"]
    assert server_module.mcp.options["stateless_http"] is True
    assert server_module.mcp.options["streamable_http_path"] == "/mcp"
    assert server_module.mcp.options["transport_security"].options["enable_dns_rebinding_protection"]


@pytest.mark.asyncio
async def test_bearer_auth_rejects_unknown_and_allows_valid(server_module):
    app = server_module.BearerAuth(server_module.mcp.streamable_http_app(), "example-token")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        assert (await client.post("/mcp")).status_code == 401
        assert (await client.post("/mcp", headers={"Authorization": "Bearer invalid"})).status_code == 401
        r = await client.post("/mcp", headers={"Authorization": "Bearer example-token"})
        assert r.status_code == 200
        assert r.text == "OK"


@pytest.mark.asyncio
async def test_registered_ssh_tools_invoke_service(server_module, service, monkeypatch):
    monkeypatch.setattr(server_module, "get_service", lambda: service)
    conn = await server_module.ssh_connect(host="example.org", username="alice", password="secret")
    ident = conn["connection_id"]
    assert "executed" in (await server_module.ssh_execute(ident, "echo hi"))["stdout"]
    assert (await server_module.sftp_read(ident, "/a.txt"))["data"] == "hello 世界"
    assert (await server_module.ssh_disconnect(ident))["disconnected"]