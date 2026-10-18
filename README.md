# ChatGPT SSH & SFTP MCP Server

A self-hosted **remote MCP server** for SSH command execution and SFTP file management in ChatGPT Chat Mode. Implemented with the official MCP Python SDK and Paramiko. Includes a single-user, in-memory SSH connection registry, strict SSH host-key checks, bounded command responses, and an optional HTTP bearer token.

## Capabilities

| Tool | Purpose |
| --- | --- |
| `ssh_profiles` | List named SSH targets without disclosing credentials |
| `ssh_connect` | Connect via configured profile, password, key file, or inline private key |
| `ssh_connections` | List active connections |
| `ssh_disconnect` | Close an active connection |
| `ssh_execute` | Run arbitrary remote shell commands and return stdout, stderr, status, timeout flags |
| `sftp_list` | List a remote directory |
| `sftp_stat` | Read remote file metadata |
| `sftp_read` | Read a file range in UTF-8 or Base64 |
| `sftp_write` | Create, replace, or append a remote file |
| `sftp_mkdir` | Create a remote directory |
| `sftp_rename` | Rename or move a remote path |
| `sftp_delete` | Remove a remote file or empty directory |
| `sftp_chmod` | Set remote Unix permissions |
| `sftp_upload` | Upload a file from the MCP host's transfer directory |
| `sftp_download` | Download a file to the MCP host's transfer directory |

Supports arbitrary SSH destinations **reachable from the MCP server's network** and custom SSH ports. Sessions survive MCP HTTP requests while the same Python process remains running; idle SSH connections expire automatically. Run a single process; a restart invalidates connection IDs.

## Prerequisites

- Python 3.10 or later on the MCP host (Linux, macOS, or Windows).
- Reachability from the MCP host to target servers' SSH ports.
- Valid SSH host keys in a private `known_hosts` file.
- A secure transport from ChatGPT to the MCP endpoint: **Secure MCP Tunnel** or a TLS-enabled reverse proxy with authentication.
- ChatGPT account/workspace permission to add custom MCP tools with write capabilities.

**Compatibility:** As of October 2026, OpenAI documentation says that ChatGPT Business and Enterprise/Edu support custom MCP write/modify actions in Chat Mode. Plus/Pro custom MCP access remains restricted to read/fetch actions. Installing this server cannot change those permissions. ChatGPT connects to remote MCP servers rather than a directly attached localhost HTTP endpoint.

References: [Connect custom MCP servers](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt), [Connect and test a plugin](https://developers.openai.com/plugins/deploy/connect-chatgpt), [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

## Install and run

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
mkdir -p transfers "$HOME/.ssh"
```

For example, to trust the SSH host key for `example.com` at port 2222:

```bash
ssh-keyscan -p 2222 example.com > /tmp/target_hostkey
ssh-keygen -lf /tmp/target_hostkey
# Independently compare the printed fingerprint against the one from your server administrator.
cat /tmp/target_hostkey >> "$HOME/.ssh/known_hosts"
chmod 600 "$HOME/.ssh/known_hosts"
rm /tmp/target_hostkey
```

Use a standard SSH key in `~/.ssh` (or set `SSH_MCP_KEY_ROOT` to another private directory). To define reusable targets:

```bash
cp profiles.example.json profiles.json
chmod 600 profiles.json
export SSH_MCP_PROFILES_FILE="$PWD/profiles.json"
# For the production profile in profiles.example.json:
export PRODUCTION_SSH_PASSWORD='your-password'
```

Edit the example profile names and hosts before using them. A `password_env` entry reads a secret from the MCP process environment. A `key_path` entry refers to a private key stored **on the MCP host**; paths are confined to `SSH_MCP_KEY_ROOT`. A key passphrase can be supplied using the `passphrase_env` profile setting.

Start a private MCP HTTP endpoint:

```bash
chatgpt-ssh-mcp
# http://127.0.0.1:8000/mcp
```

`python -m ssh_mcp` is equivalent. The endpoint binds to localhost by default. For a Tunnel or a trusted local MCP client, this can be used without a public listener. You can also set `MCP_TRANSPORT=stdio` for MCP stdio clients or a stdio Secure MCP Tunnel.

### Secure MCP Tunnel (recommended for private deployments)

Open the [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) documentation, create a tunnel in your OpenAI organization/workspace, and follow the official `tunnel-client` setup. Configure the tunnel to forward to:

```text
http://127.0.0.1:8000/mcp
```

Keep both the SSH MCP server and tunnel client running. In ChatGPT Plugins, select **Add custom MCP server → Tunnel**, then the corresponding tunnel ID. This avoids exposing SSH control tools through an internet-facing web listener. You can also use the documented `tunnel-client --mcp-command` option with the stdio transport.

### Public HTTPS endpoint

Use a reverse proxy such as Caddy or nginx. A minimal Caddy configuration is in `Caddyfile.example`. Keep the MCP HTTP listener on `127.0.0.1` and terminate TLS at the proxy. Set both a long bearer token and explicit public host allowlist:

```bash
export MCP_BEARER_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export MCP_ALLOWED_HOSTS='mcp.example.com,mcp.example.com:*'
chatgpt-ssh-mcp
```

Configure your ChatGPT custom MCP connection with endpoint `https://mcp.example.com/mcp` and **API key / Bearer** authentication, supplying the bearer token in ChatGPT's connection settings. Do not send the bearer token as part of a prompt. API-key authentication availability may vary by ChatGPT connection/publishing flow; consult the current plugin settings. Alternatively, use an authenticated Secure MCP Tunnel or a properly configured OAuth-capable gateway. Public publication workflows may require OAuth.

The built-in allowlist checks the incoming `Host` header. To add additional allowed browser origins, use `MCP_ALLOWED_ORIGINS` (comma-separated HTTPS origin URLs). This server is intended for **one trusted operator**; a shared token provides shared access to all active connections. Create separate deployments for different people or security boundaries.

## Docker (optional)

Build the included `Dockerfile` or adapt `compose.example.yml`. The container runs without root privileges (UID 10001). Prepare private, read-only SSH material under `secrets/ssh`, a `secrets/profiles.json` file owned/readable by UID 10001, and a writable `transfers` directory. Set `MCP_BEARER_TOKEN` and `MCP_ALLOWED_HOSTS` before starting the Compose example. Expose the public HTTPS hostname through your own reverse proxy; only local port 8000 is published in the Compose example.

## ChatGPT connection and use

1. Open ChatGPT on the web and navigate to **Plugins → + → Add custom MCP server** (subject to account/workspace permissions).
2. Select a Secure MCP Tunnel or enter your publicly reachable `https://.../mcp` endpoint; configure matching authentication.
3. Create/install the resulting plugin. Start a new Chat Mode conversation and select the plugin with `@`.
4. Ask the model to call `ssh_profiles`, `ssh_connect(profile="staging")`, and then use the returned `connection_id` for operations. ChatGPT may request confirmation for write actions.

Example requests:

- "Connect to the `staging` SSH profile and run `uname -a`."
- "List `/var/log` with SFTP on that connection."
- "Read the first 32 KB of `/etc/nginx/nginx.conf` and tell me what it configures."
- "Create `/tmp/hello.txt` with content `Hello` using SFTP."
- "Run `python3 -c 'print(2 + 2)'` over SSH."

**Credential handling:** Prefer a named profile with `password_env` or `key_path`. Direct `ssh_connect(password=...)` and `ssh_connect(private_key=...)` are supported for flexibility, but those arguments may appear in ChatGPT conversation or tool-call records. The server does not include secrets in successful responses or persist them to disk. Never paste permanent privileged credentials into chat when a profile is available.

### File-transfer semantics

The `sftp_read` and `sftp_write` tools transfer content through MCP responses/arguments, supporting UTF-8 and Base64. Large files can be read in segments using `offset` and `length`. `sftp_upload` and `sftp_download` operate on files in **`SSH_MCP_TRANSFER_ROOT` on the MCP host**, not on the ChatGPT user's computer; conversation attachments do not automatically become local files in that directory. Place files into the transfer directory using your own secure file delivery process.

Default limits: 1 MiB inline SFTP reads/writes, 1 MiB combined command output, 120-second maximum command time, 30-minute idle connection expiration, up to 32 connections. Limits can be adjusted with environment variables in `.env.example`. Each SSH command executes in a fresh remote session channel and is not an interactive PTY: shell state such as `cd` does not persist across calls. Use explicit working-directory commands or single scripts.

## Configuration variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `MCP_TRANSPORT` | `http` | `http` or `stdio` |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8000` | HTTP listening address |
| `MCP_BEARER_TOKEN` | unset | Optional HTTP bearer-token check; required for public deployments |
| `MCP_ALLOWED_HOSTS` | unset | Additional allowed HTTP Host headers; required for public proxy hostname |
| `MCP_ALLOWED_ORIGINS` | unset | Additional permitted HTTP Origin values |
| `SSH_MCP_PROFILES_FILE` | unset | Private JSON file for named server profiles |
| `SSH_MCP_KNOWN_HOSTS` | `~/.ssh/known_hosts` | Verified SSH server host keys |
| `SSH_MCP_KEY_ROOT` | `~/.ssh` | Directory allowed to contain SSH private keys |
| `SSH_MCP_TRANSFER_ROOT` | `./transfers` | Confined local SFTP file exchange directory |
| `SSH_MCP_SESSION_TTL` | `1800` | Idle SSH connection expiry in seconds |
| `SSH_MCP_MAX_SESSIONS` | `32` | Maximum simultaneous active SSH connections |
| `SSH_MCP_MAX_OUTPUT_BYTES` | `1048576` | Combined stdout/stderr limit per command |
| `SSH_MCP_MAX_INLINE_BYTES` | `1048576` | Maximum bytes per inline SFTP operation |
| `SSH_MCP_MAX_COMMAND_SECONDS` | `120` | Maximum accepted SSH command timeout |

## Tests

```bash
python -m pip install -e '.[test]'
pytest -q
python -m compileall -q src tests
```

The suite includes core SSH/SFTP behavioral tests (with simulated SSH transport), bearer authentication tests, tool registration tests, and a real MCP Streamable HTTP protocol integration test (`test_mcp_integration.py`) using the installed official MCP SDK. An automated GitHub Actions workflow is included for Python 3.11, 3.12, and 3.13.

To inspect and invoke the real MCP endpoint with a graphical client:

```bash
npx -y @modelcontextprotocol/inspector
```

Connect the inspector to `http://127.0.0.1:8000/mcp` or to your HTTPS endpoint.

## Security

This server can execute arbitrary code with the SSH account's privileges. Run it only for trusted operators and limit SSH account privileges to the servers and paths needed. The server validates remote host keys against `known_hosts` and rejects untrusted hosts. It prevents MCP-host local transfer paths from escaping the configured transfer directory, but does not restrict remote target paths or commands: those operations are intentionally powerful. Protect the endpoint with a Tunnel or TLS and authentication; never publish it anonymously. Back up remote files before destructive changes.

## Development and CI

Run `python -m pip install -e '.[test]'` in a virtual environment, then
`bash scripts/check.sh` to compile the Python sources, execute the tests,
check dependency consistency, and (when `build` is installed) construct wheel
and source distributions.

GitHub Actions runs the full test suite on Python 3.10 through 3.13,
including a Streamable HTTP handshake and MCP tool discovery using the
installed official SDK. After tests pass, it builds and verifies a wheel and
source archive, uploads them as workflow artifacts, and builds a Docker image.
The Docker job also checks that an unauthenticated HTTP request is rejected.
The pipeline does not push container images or publish to a package registry.

Remote command output and input are bounded by the configured maximum sizes.
Commands have an execution timeout; a timed-out command's SSH channel is closed.
SFTP files are created exclusively by default and overwrite operations require
an explicit `overwrite=true`. Local downloaded files are created atomically.
The transfer directory should be accessible only to the MCP service account.