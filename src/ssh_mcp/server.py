"""ChatGPT-compatible Streamable HTTP MCP server with SSH/SFTP tools."""

from __future__ import annotations

import asyncio
from functools import lru_cache
import hmac
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations


def _csv(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*",
                   *_csv("MCP_ALLOWED_HOSTS")],
    allowed_origins=_csv("MCP_ALLOWED_ORIGINS"),
)

from .service import SSHService

mcp = FastMCP(
    "Remote SSH & SFTP",
    instructions=(
        "Manage user-authorized remote SSH servers. Use ssh_profiles and ssh_connect to open a connection. "
        "Use the returned connection_id in other tools. Commands run in the remote user's default shell. "
        "File paths for SFTP are on the remote host. Paths for sftp_upload/sftp_download refer to "
        "the MCP server's configured transfers directory, not the ChatGPT user's computer. "
        "Never echo credentials. Ask for confirmation before destructive remote operations."
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/mcp",
    transport_security=transport_security,
)


def annotations(*, read_only: bool, destructive: bool = False) -> ToolAnnotations:
    return ToolAnnotations(readOnlyHint=read_only, destructiveHint=destructive, openWorldHint=True)


@lru_cache(maxsize=1)
def get_service() -> SSHService:
    return SSHService(
        ttl=int(os.getenv("SSH_MCP_SESSION_TTL", "1800")),
        max_sessions=int(os.getenv("SSH_MCP_MAX_SESSIONS", "32")),
        max_output_bytes=int(os.getenv("SSH_MCP_MAX_OUTPUT_BYTES", "1048576")),
        max_inline_bytes=int(os.getenv("SSH_MCP_MAX_INLINE_BYTES", "1048576")),
        max_command_seconds=int(os.getenv("SSH_MCP_MAX_COMMAND_SECONDS", "120")),
    )


async def _run(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


@mcp.tool(annotations=annotations(read_only=True))
async def ssh_profiles() -> dict:
    """List configured server profiles. Does not reveal credentials."""
    return await _run(get_service().profiles)


@mcp.tool(annotations=annotations(read_only=False))
async def ssh_connect(
    profile: str | None = None,
    host: str | None = None,
    port: int = 22,
    username: str | None = None,
    password: str | None = None,
    key_path: str | None = None,
    private_key: str | None = None,
    passphrase: str | None = None,
) -> dict:
    """Open an SSH connection by named server profile or host credentials; return a connection_id.

    Prefer profiles (password_env or key_path on the MCP host) to avoid putting credentials
    in ChatGPT conversation history. For direct connections, provide exactly one of password,
    key_path (inside SSH_MCP_KEY_ROOT), or private_key (PEM/OpenSSH contents). An encrypted key
    may require passphrase. SSH host keys MUST be trusted in known_hosts before connecting.
    """
    return await _run(get_service().connect, profile=profile, host=host, port=port, username=username,
                      password=password, key_path=key_path, private_key=private_key, passphrase=passphrase)


@mcp.tool(annotations=annotations(read_only=True))
async def ssh_connections() -> list[dict]:
    """Show active connection IDs, target hosts, and idle expiration times."""
    return await _run(get_service().connections)


@mcp.tool(annotations=annotations(read_only=False))
async def ssh_disconnect(connection_id: str) -> dict:
    """Close an SSH connection and release its in-memory session."""
    return await _run(get_service().disconnect, connection_id)


@mcp.tool(annotations=annotations(read_only=False, destructive=True))
async def ssh_execute(connection_id: str, command: str, stdin: str = "", timeout_seconds: int = 30) -> dict:
    """Execute arbitrary shell commands or code on a connected SSH server.

    Set stdin to send UTF-8 text. Returns stdout, stderr, exit_code, timed_out and truncated.
    Exit code is null when the command exceeds execution time or output limits. Execute only
    commands authorized by the user; confirm significant or destructive changes first.
    """
    return await _run(get_service().execute, connection_id, command, stdin, timeout_seconds)


@mcp.tool(annotations=annotations(read_only=True))
async def sftp_list(connection_id: str, remote_path: str = ".") -> dict:
    """List files and directories on the remote SFTP server."""
    return await _run(get_service().listdir, connection_id, remote_path)


@mcp.tool(annotations=annotations(read_only=True))
async def sftp_stat(connection_id: str, remote_path: str, follow_symlinks: bool = True) -> dict:
    """Inspect remote file metadata, optionally without following symbolic links."""
    return await _run(get_service().stat, connection_id, remote_path, follow_symlinks)


@mcp.tool(annotations=annotations(read_only=True))
async def sftp_read(connection_id: str, remote_path: str, encoding: str = "utf8", offset: int = 0,
                    length: int = 65536) -> dict:
    """Read a remote file range as UTF-8 text or Base64; use offset and has_more for large files."""
    return await _run(get_service().read_file, connection_id, remote_path, encoding, offset, length)


@mcp.tool(annotations=annotations(read_only=False, destructive=True))
async def sftp_write(connection_id: str, remote_path: str, data: str, encoding: str = "utf8",
                     overwrite: bool = False, append: bool = False) -> dict:
    """Write UTF-8 or Base64 content to a remote file. Default refuses to overwrite existing files.

    Set overwrite=true to replace, or append=true to append. Max bytes per call is configured by
    SSH_MCP_MAX_INLINE_BYTES. For large files, use sftp_upload from the MCP transfers directory.
    """
    return await _run(get_service().write_file, connection_id, remote_path, data, encoding, overwrite, append)


@mcp.tool(annotations=annotations(read_only=False))
async def sftp_mkdir(connection_id: str, remote_path: str, permissions: int = 493) -> dict:
    """Create a remote directory. Permissions use an integer Unix mode (493 means 0755)."""
    return await _run(get_service().mkdir, connection_id, remote_path, permissions)


@mcp.tool(annotations=annotations(read_only=False, destructive=True))
async def sftp_rename(connection_id: str, source: str, destination: str, overwrite: bool = False) -> dict:
    """Rename or move a remote file/directory. Overwrite requires SFTP posix-rename extension."""
    return await _run(get_service().rename, connection_id, source, destination, overwrite)


@mcp.tool(annotations=annotations(read_only=False, destructive=True))
async def sftp_delete(connection_id: str, remote_path: str, directory: bool = False) -> dict:
    """Delete a remote file or an EMPTY remote directory (directory=true); never recursive."""
    return await _run(get_service().delete, connection_id, remote_path, directory)


@mcp.tool(annotations=annotations(read_only=False, destructive=True))
async def sftp_chmod(connection_id: str, remote_path: str, permissions: int) -> dict:
    """Set a remote file's Unix permissions as an integer (e.g., 420 for 0644)."""
    return await _run(get_service().chmod, connection_id, remote_path, permissions)


@mcp.tool(annotations=annotations(read_only=False, destructive=True))
async def sftp_upload(connection_id: str, local_path: str, remote_path: str, overwrite: bool = False) -> dict:
    """Transfer an MCP-host file to the remote SSH server via SFTP.

    local_path is relative to SSH_MCP_TRANSFER_ROOT on the MCP server; it is not a ChatGPT
    conversation attachment or a path on the ChatGPT user's laptop.
    """
    return await _run(get_service().upload, connection_id, local_path, remote_path, overwrite)


@mcp.tool(annotations=annotations(read_only=False, destructive=True))
async def sftp_download(connection_id: str, remote_path: str, local_path: str,
                        overwrite: bool = False) -> dict:
    """Download a remote SFTP file into SSH_MCP_TRANSFER_ROOT on the MCP server."""
    return await _run(get_service().download, connection_id, remote_path, local_path, overwrite)


class BearerAuth:
    """Simple ASGI bearer-token authentication; leaves lifespan messages untouched."""

    def __init__(self, app, token: str | None):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.token is None:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"")
        expected = ("Bearer " + self.token).encode("utf-8")
        if not hmac.compare_digest(auth, expected):
            body = b'{"error":"Unauthorized"}'
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", b"Bearer"),
                                    (b"content-length", str(len(body)).encode("ascii"))]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def make_app():
    return BearerAuth(mcp.streamable_http_app(), os.environ.get("MCP_BEARER_TOKEN"))


def main() -> None:
    import uvicorn

    transport = os.getenv("MCP_TRANSPORT", "http").lower()
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    if transport != "http":
        raise SystemExit("MCP_TRANSPORT must be http or stdio")
    host = os.getenv("MCP_HOST", "127.0.0.1")
    port = int(os.getenv("MCP_PORT", "8000"))
    token = os.environ.get("MCP_BEARER_TOKEN")
    if host not in ("127.0.0.1", "::1", "localhost"):
        if not token or len(token) < 24:
            raise SystemExit("Non-loopback listener requires a long MCP_BEARER_TOKEN and an HTTPS reverse proxy")
        if not _csv("MCP_ALLOWED_HOSTS"):
            raise SystemExit("Set MCP_ALLOWED_HOSTS to the public DNS hostname of the HTTPS reverse proxy")
    get_service()  # Fail early for invalid configuration.
    uvicorn.run(make_app(), host=host, port=port, workers=1,
                log_level=os.getenv("MCP_LOG_LEVEL", "info"), access_log=False)


if __name__ == "__main__":
    main()