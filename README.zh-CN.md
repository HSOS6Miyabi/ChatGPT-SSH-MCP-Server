# ChatGPT SSH / SFTP MCP Server

这是一个面向 ChatGPT Chat Mode 的远程 MCP Server。通过 MCP 工具，ChatGPT 可以连接用户授权的 SSH 服务器，执行 Shell 命令或代码，并通过 SFTP 管理远程文件。项目使用官方 MCP Python SDK 和 Paramiko，提供 Streamable HTTP 接口，也支持标准输入输出（stdio）传输。

## 功能

| 类别 | MCP 工具 | 功能 |
| --- | --- | --- |
| 连接管理 | `ssh_profiles` | 列出已配置的服务器，不返回密码或私钥 |
| 连接管理 | `ssh_connect` | 使用密码、密钥文件、私钥内容或预设配置连接服务器 |
| 连接管理 | `ssh_connections`、`ssh_disconnect` | 查看和断开连接 |
| 命令执行 | `ssh_execute` | 执行远程命令，处理标准输入，返回输出、退出码及超时状态 |
| 文件读取 | `sftp_list`、`sftp_stat`、`sftp_read` | 读取目录、文件属性及文本或二进制数据 |
| 文件修改 | `sftp_write`、`sftp_mkdir`、`sftp_rename`、`sftp_delete`、`sftp_chmod` | 写入文件及修改远程文件系统 |
| 文件传输 | `sftp_upload`、`sftp_download` | 在 MCP Server 本机与远程 SSH 服务器之间传输文件 |

支持任意可从 MCP Server 所在网络访问的 SSH 服务器，并允许指定 SSH 端口。同一服务进程可以管理多个 SSH 连接，每个连接使用独立标识符；长时间未使用的连接会过期。

## 一、安装

需要 Python 3.10 或更高版本。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
mkdir -p transfers "$HOME/.ssh"
```

Windows PowerShell 环境激活命令：

```powershell
.venv\Scripts\Activate.ps1
```

SSH 主机密钥必须预先写入 `known_hosts`。例如，访问 2222 端口时：

```bash
ssh-keyscan -p 2222 example.com > /tmp/hostkey
ssh-keygen -lf /tmp/hostkey
```

请通过服务器管理界面或管理员提供的可信渠道核对公钥指纹，然后执行：

```bash
cat /tmp/hostkey >> "$HOME/.ssh/known_hosts"
chmod 600 "$HOME/.ssh/known_hosts"
rm /tmp/hostkey
```

MCP Server 会拒绝连接尚未信任的主机。这项验证也适用于密码认证。

## 二、配置服务器

推荐使用预设配置，使 SSH 密码和私钥留在 MCP Server 上。

```bash
cp profiles.example.json profiles.json
chmod 600 profiles.json
export SSH_MCP_PROFILES_FILE="$PWD/profiles.json"
```

将 `profiles.json` 中的示例主机、端口和用户名修改为实际值。密码认证示例：

```json
{
  "production": {
    "host": "server.example.com",
    "port": 2222,
    "username": "admin",
    "password_env": "PRODUCTION_SSH_PASSWORD"
  }
}
```

在 MCP Server 进程的环境变量中设置 `PRODUCTION_SSH_PASSWORD`。对于私钥认证，将 `password_env` 替换为 `"key_path": "id_ed25519"`；密钥文件应放在 `SSH_MCP_KEY_ROOT` 下（默认是 `~/.ssh`）。加密私钥可以指定 `"passphrase_env": "SSH_KEY_PASSPHRASE"`。

`ssh_connect` 也支持直接指定 `host`、`port`、`username` 和 `password` 或 `private_key`。直接传递凭据可能使敏感信息出现在 ChatGPT 会话或工具调用记录中，因此建议优先使用配置文件。

## 三、启动 MCP Server

```bash
chatgpt-ssh-mcp
```

默认接口：`http://127.0.0.1:8000/mcp`。

ChatGPT 通过远程 MCP 连接访问服务器，因此推荐配置 [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)，将私有 MCP 接口连接至 ChatGPT，无须向互联网开放监听端口。Tunnel 的目标地址使用上述本地 MCP URL。也可以设置 `MCP_TRANSPORT=stdio` 并使用 Tunnel 支持的 `--mcp-command` 启动方式。

如果需要公共 HTTPS 接口，请使用 Caddy 或 nginx 提供 TLS，并设置访问令牌及公共域名：

```bash
export MCP_BEARER_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export MCP_ALLOWED_HOSTS='mcp.example.com,mcp.example.com:*'
chatgpt-ssh-mcp
```

仓库包含 `Caddyfile.example`。将 MCP Server 的地址配置为 `https://mcp.example.com/mcp`，并在 ChatGPT 的连接设置中选择相应的 Bearer API Key 认证方式。认证选项受当前 ChatGPT 产品界面与权限限制；另一种选择是使用 Secure MCP Tunnel。向互联网提供此服务时，必须配置有效身份认证。

## 四、在 ChatGPT Chat Mode 中连接

1. 使用 ChatGPT 网页端，进入 **Plugins → + → Add custom MCP server**。
2. 根据部署方式选择 **Tunnel** 或填写 HTTPS MCP URL，然后完成身份认证配置。
3. 创建并安装连接。在新对话中通过 `@` 选择该插件。
4. 调用 `ssh_profiles` 查看可用配置，通过 `ssh_connect(profile="production")` 获取 `connection_id`，然后使用其他 SSH/SFTP 工具。

示例：

- “连接 production 服务器，执行 `uname -a`，告诉我系统版本。”
- “通过 SFTP 列出 `/var/log` 的文件。”
- “读取 `/etc/nginx/nginx.conf` 并解释配置。”
- “将指定文本写入 `/tmp/test.txt`。”
- “通过 SSH 执行 `python3 -c 'print(2 + 2)'`。”

截至 2026 年 10 月，OpenAI 官方文档列明：ChatGPT Business、Enterprise、Edu 可使用包含写入和修改操作的自定义 MCP；Plus/Pro 的自定义 MCP 权限仍主要限于读取操作。软件本身可以提供所有工具，但 ChatGPT 是否允许调用写入工具取决于账户权限。参见[官方说明](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)。

## 五、文件传输及限制

- `sftp_read`、`sftp_write` 可以直接通过 MCP 传输 UTF-8 文本或 Base64 数据。读取大文件时，可以使用 `offset` 和 `length` 分段读取。
- `sftp_upload`、`sftp_download` 的 `local_path` 指 **MCP Server 本机 `SSH_MCP_TRANSFER_ROOT` 目录中的文件**，并不指用户电脑上的文件。ChatGPT 聊天附件不会自动出现在该目录中。
- 默认最大单次文本或二进制数据为 1 MiB，单次命令输出总量为 1 MiB，命令超时上限为 120 秒，SSH 连接空闲有效期为 1800 秒。可以通过 `.env.example` 内列出的环境变量修改这些限制。
- 命令通过独立的 SSH 通道执行。连续调用中的工作目录或 Shell 变量不会自动保留，建议使用明确的完整命令。
- SSH 连接保存在当前进程内，重启 MCP Server 后需要重新调用 `ssh_connect`。
- 此程序按单个可信操作人员的使用场景设计；多个用户共用同一个 Bearer Token 时，会共用连接管理权限。不同用户应分别部署服务实例。

## 六、测试

```bash
python -m pip install -e '.[test]'
pytest -q
python -m compileall -q src tests
```

测试包括 SSH/SFTP 操作逻辑、访问认证、工具注册与 MCP Streamable HTTP 协议集成测试。仓库配置了 GitHub Actions，可以在 Python 3.11、3.12 和 3.13 下自动执行测试。

完整的环境变量说明、Docker 部署说明及其他技术细节参见 [README.md](README.md)。

## 自动化测试和构建

在 Python 虚拟环境中安装开发依赖后，运行：

```bash
python -m pip install -e '.[test]' build twine
bash scripts/check.sh
```

GitHub Actions 在 Python 3.10、3.11、3.12、3.13 环境运行测试，使用官方 MCP SDK 检查 Streamable HTTP 服务的初始化和工具发现。测试通过后，自动构建 Python wheel 和源码包，检查包元数据，并提供可下载的构建产物；同时构建 Docker 镜像并测试 HTTP 访问认证。CI 不会自动向软件包仓库或容器镜像仓库发布产物。

默认情况下，SFTP 上传会使用独占创建方式，防止已有远程文件被覆盖。下载至 MCP Server 时也采用原子方式创建本地文件。如需替换现有文件，须明确指定 `overwrite=true`。请确保本地传输目录仅允许 MCP 服务账户访问。