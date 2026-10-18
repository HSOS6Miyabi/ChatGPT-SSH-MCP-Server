"""Synchronous SSH/SFTP operations with short-lived, in-memory connections."""

from __future__ import annotations

import base64
import binascii
import io
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

import paramiko

T = TypeVar("T")


class SSHServiceError(ValueError):
    """An error suitable for returning to an MCP tool caller."""


@dataclass
class Session:
    client: paramiko.SSHClient
    host: str
    port: int
    username: str
    expires_at: float
    lock: threading.RLock


class SSHService:
    def __init__(
        self,
        *,
        known_hosts: str | None = None,
        key_root: str | None = None,
        transfer_root: str | None = None,
        profiles_file: str | None = None,
        ttl: int = 1800,
        max_sessions: int = 32,
        max_output_bytes: int = 1024 * 1024,
        max_inline_bytes: int = 1024 * 1024,
        max_command_seconds: int = 120,
        client_factory: Callable[[], paramiko.SSHClient] = paramiko.SSHClient,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl < 1 or max_sessions < 1 or max_output_bytes < 1 or max_inline_bytes < 1 or max_command_seconds < 1:
            raise ValueError("All service limits must be positive")
        self.known_hosts = Path(known_hosts or os.environ.get("SSH_MCP_KNOWN_HOSTS", "~/.ssh/known_hosts")).expanduser()
        self.key_root = Path(key_root or os.environ.get("SSH_MCP_KEY_ROOT", "~/.ssh")).expanduser().resolve()
        self.transfer_root = Path(transfer_root or os.environ.get("SSH_MCP_TRANSFER_ROOT", "./transfers")).expanduser().resolve()
        self.transfer_root.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl
        self.max_sessions = max_sessions
        self.max_output_bytes = max_output_bytes
        self.max_inline_bytes = max_inline_bytes
        self.max_command_seconds = max_command_seconds
        self._client_factory = client_factory
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()
        self._profiles = self._load_profiles(profiles_file or os.environ.get("SSH_MCP_PROFILES_FILE"))

    @staticmethod
    def _load_profiles(file: str | None) -> dict[str, dict[str, Any]]:
        if not file:
            return {}
        p = Path(file).expanduser()
        if os.name != "nt" and p.stat().st_mode & 0o077:
            raise SSHServiceError("Profiles file must be private (chmod 600)")
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, dict) for k, v in data.items()):
            raise SSHServiceError("Profiles file must contain an object mapping profile names to settings")
        return data

    def profiles(self) -> dict[str, Any]:
        """Show profile names and targets without returning passwords or key material."""
        return {name: {"host": item.get("host"), "port": item.get("port", 22),
                       "username": item.get("username"), "auth_method":
                       "password" if item.get("password_env") else "private_key" if item.get("key_path") else "unknown"}
                for name, item in self._profiles.items()}

    def _key_path(self, name: str) -> str:
        candidate = Path(name).expanduser()
        candidate = (candidate if candidate.is_absolute() else self.key_root / candidate).resolve()
        if not candidate.is_relative_to(self.key_root):
            raise SSHServiceError("SSH key path must be inside SSH_MCP_KEY_ROOT")
        if not candidate.is_file():
            raise SSHServiceError("SSH private key file does not exist")
        return str(candidate)

    def _local_path(self, name: str) -> Path:
        if not name or "\x00" in name:
            raise SSHServiceError("Invalid local file path")
        p = Path(name)
        if p.is_absolute():
            raise SSHServiceError("Local paths must be relative to SSH_MCP_TRANSFER_ROOT")
        resolved = (self.transfer_root / p).resolve()
        if not resolved.is_relative_to(self.transfer_root):
            raise SSHServiceError("Local file path escapes SSH_MCP_TRANSFER_ROOT")
        return resolved

    @staticmethod
    def _key_from_text(text: str, passphrase: str | None) -> paramiko.PKey:
        for name in ("Ed25519Key", "RSAKey", "ECDSAKey"):
            key_class = getattr(paramiko, name, None)
            if key_class is None:
                continue
            try:
                return key_class.from_private_key(io.StringIO(text), password=passphrase)
            except (paramiko.SSHException, ValueError):
                pass
        raise SSHServiceError("Private key is invalid, unsupported, or has an incorrect passphrase")

    def _evict_expired(self) -> None:
        """Called under _lock; do not close connections with an active operation."""
        now = self._clock()
        for ident, session in list(self._sessions.items()):
            if not session.lock.acquire(blocking=False):
                continue
            try:
                transport = session.client.get_transport()
                if session.expires_at <= now or transport is None or not transport.is_active():
                    self._sessions.pop(ident, None)
                    session.client.close()
            finally:
                session.lock.release()

    def connect(
        self,
        *,
        profile: str | None = None,
        host: str | None = None,
        port: int = 22,
        username: str | None = None,
        password: str | None = None,
        key_path: str | None = None,
        private_key: str | None = None,
        passphrase: str | None = None,
    ) -> dict[str, Any]:
        if profile:
            if profile not in self._profiles:
                raise SSHServiceError("Unknown SSH profile")
            if any(x is not None for x in (host, username, password, key_path, private_key, passphrase)) or port != 22:
                raise SSHServiceError("When using a profile, omit other connection parameters")
            cfg = self._profiles[profile]
            host, port, username = cfg.get("host"), cfg.get("port", 22), cfg.get("username")
            if cfg.get("password_env"):
                password = os.environ.get(cfg["password_env"])
                if not password:
                    raise SSHServiceError("Password environment variable for profile is unset")
            key_path = cfg.get("key_path")
            if cfg.get("passphrase_env"):
                passphrase = os.environ.get(cfg["passphrase_env"])
        if not isinstance(host, str) or not host.strip() or any(ch in host for ch in "\x00\r\n"):
            raise SSHServiceError("Host is required")
        if not isinstance(username, str) or not username.strip() or any(ch in username for ch in "\x00\r\n"):
            raise SSHServiceError("Username is required")
        if type(port) is not int or not 1 <= port <= 65535:
            raise SSHServiceError("Port must be in the range 1..65535")
        provided = sum(value is not None for value in (password, key_path, private_key))
        if provided != 1:
            raise SSHServiceError("Specify exactly one authentication method: password, key_path, or private_key")
        if not self.known_hosts.is_file():
            raise SSHServiceError("SSH known_hosts file does not exist; verify and add the server host key first")

        kwargs: dict[str, Any] = dict(hostname=host, port=port, username=username, timeout=12,
                                      auth_timeout=15, banner_timeout=15, look_for_keys=False, allow_agent=False)
        if password is not None:
            kwargs["password"] = password
        elif key_path is not None:
            kwargs["key_filename"] = self._key_path(key_path)
            kwargs["passphrase"] = passphrase
        else:
            kwargs["pkey"] = self._key_from_text(private_key or "", passphrase)
        client = self._client_factory()
        try:
            client.load_system_host_keys(str(self.known_hosts))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            client.connect(**kwargs)
        except Exception as exc:
            client.close()
            raise SSHServiceError(f"SSH connection failed: {type(exc).__name__}: {exc}") from None
        # Credentials do not survive this function; the authenticated transport is retained in memory.
        with self._lock:
            self._evict_expired()
            if len(self._sessions) >= self.max_sessions:
                client.close()
                raise SSHServiceError("Maximum concurrent SSH connections reached")
            ident = secrets.token_urlsafe(24)
            self._sessions[ident] = Session(client, host, port, username, self._clock() + self.ttl, threading.RLock())
        return {"connection_id": ident, "host": host, "port": port, "username": username,
                "expires_after_idle_seconds": self.ttl}

    def connections(self) -> list[dict[str, Any]]:
        with self._lock:
            self._evict_expired()
            now = self._clock()
            return [{"connection_id": ident, "host": s.host, "port": s.port, "username": s.username,
                     "expires_in_seconds": max(0, int(s.expires_at - now))} for ident, s in self._sessions.items()]

    def _with_connection(self, ident: str, action: Callable[[Session], T]) -> T:
        with self._lock:
            self._evict_expired()
            session = self._sessions.get(ident)
            if session is None:
                raise SSHServiceError("SSH connection not found or expired; connect again")
        with session.lock:
            with self._lock:
                if self._sessions.get(ident) is not session:
                    raise SSHServiceError("SSH connection has been closed")
                session.expires_at = self._clock() + self.ttl
            return action(session)

    def disconnect(self, ident: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.pop(ident, None)
        if session is None:
            return {"disconnected": False, "reason": "Connection already closed or expired"}
        with session.lock:
            session.client.close()
        return {"disconnected": True}

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            with session.lock:
                session.client.close()

    def execute(self, ident: str, command: str, stdin: str = "", timeout_seconds: int = 30) -> dict[str, Any]:
        if not isinstance(command, str) or not command or "\x00" in command:
            raise SSHServiceError("A nonempty shell command is required")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= self.max_command_seconds:
            raise SSHServiceError(f"timeout_seconds must be between 1 and {self.max_command_seconds}")
        if not isinstance(stdin, str):
            raise SSHServiceError("Standard input must be text")
        input_data = stdin.encode("utf-8")
        if len(input_data) > self.max_inline_bytes:
            raise SSHServiceError("Standard input exceeds configured size limit")

        def run(session: Session) -> dict[str, Any]:
            transport = session.client.get_transport()
            if transport is None or not transport.is_active():
                raise SSHServiceError("SSH transport is disconnected")
            start = self._clock()
            try:
                channel = transport.open_session(timeout=min(12, timeout_seconds))
            except (OSError, EOFError, paramiko.SSHException) as exc:
                raise SSHServiceError(f"SSH channel failed: {type(exc).__name__}: {exc}") from None
            stdout = bytearray()
            stderr = bytearray()
            truncated = False
            timed_out = False
            try:
                channel.settimeout(min(12, timeout_seconds))
                channel.exec_command(command)
                # Nonblocking send/receive prevents deadlock when a command writes
                # more than SSH's channel window before reading standard input.
                channel.settimeout(0.0)
                position = 0
                stdin_closed = False
                while True:
                    if self._clock() - start >= timeout_seconds:
                        timed_out = True
                        break
                    progress = False
                    if not stdin_closed and position < len(input_data) and channel.send_ready():
                        try:
                            count = channel.send(input_data[position:position + 32768])
                        except socket.timeout:
                            count = 0
                        position += count
                        progress = progress or count > 0
                    if not stdin_closed and position == len(input_data):
                        channel.shutdown_write()
                        stdin_closed = True
                        progress = True
                    for ready, receive, target in ((channel.recv_ready, channel.recv, stdout),
                                                   (channel.recv_stderr_ready, channel.recv_stderr, stderr)):
                        if ready():
                            remaining = self.max_output_bytes - len(stdout) - len(stderr)
                            if remaining <= 0:
                                truncated = True
                                break
                            chunk = receive(min(32768, remaining + 1))
                            target.extend(chunk[:remaining])
                            progress = progress or bool(chunk)
                            if len(chunk) > remaining:
                                truncated = True
                                break
                    if truncated:
                        break
                    if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                        break
                    if not progress:
                        time.sleep(0.02)
                exit_code = None if timed_out or truncated else channel.recv_exit_status()
                return {"exit_code": exit_code, "stdout": stdout.decode("utf-8", errors="replace"),
                        "stderr": stderr.decode("utf-8", errors="replace"), "timed_out": timed_out,
                        "truncated": truncated}
            except (OSError, EOFError, paramiko.SSHException) as exc:
                raise SSHServiceError(f"SSH command failed: {type(exc).__name__}: {exc}") from None
            finally:
                channel.close()

        return self._with_connection(ident, run)

    @staticmethod
    def _check_remote_path(remote_path: str) -> None:
        if not remote_path or "\x00" in remote_path:
            raise SSHServiceError("Remote path must be nonempty")

    def _sftp(self, ident: str, operation: Callable[[paramiko.SFTPClient], T]) -> T:
        def work(session: Session) -> T:
            sftp = session.client.open_sftp()
            try:
                return operation(sftp)
            except FileExistsError:
                raise SSHServiceError("SFTP destination already exists") from None
            except (OSError, EOFError, paramiko.SSHException) as exc:
                raise SSHServiceError(f"SFTP operation failed: {type(exc).__name__}: {exc}") from None
            finally:
                sftp.close()
        return self._with_connection(ident, work)

    @staticmethod
    def _info(attrs: Any, name: str | None = None) -> dict[str, Any]:
        mode = attrs.st_mode
        return {"name": name if name is not None else getattr(attrs, "filename", None),
                "size": attrs.st_size, "permissions": oct(stat.S_IMODE(mode)) if mode is not None else None,
                "type": "directory" if mode is not None and stat.S_ISDIR(mode) else
                        "symlink" if mode is not None and stat.S_ISLNK(mode) else "file",
                "modified_at_unix": attrs.st_mtime}

    def listdir(self, ident: str, remote_path: str = ".") -> dict[str, Any]:
        self._check_remote_path(remote_path)
        def action(sftp: paramiko.SFTPClient) -> dict[str, Any]:
            entries = []
            for attr in sftp.listdir_iter(remote_path):
                if len(entries) >= 1000:
                    raise SSHServiceError("Directory contains more than 1000 entries; narrow the path")
                entries.append(self._info(attr))
            entries.sort(key=lambda a: a["name"])
            return {"path": remote_path, "entries": entries}
        return self._sftp(ident, action)

    def stat(self, ident: str, remote_path: str, follow_symlinks: bool = True) -> dict[str, Any]:
        self._check_remote_path(remote_path)
        return self._sftp(ident, lambda s: {"path": remote_path,
                                          **self._info(s.stat(remote_path) if follow_symlinks else s.lstat(remote_path))})

    def read_file(self, ident: str, remote_path: str, encoding: str = "utf8", offset: int = 0,
                  length: int = 65536) -> dict[str, Any]:
        self._check_remote_path(remote_path)
        if encoding not in ("utf8", "base64"):
            raise SSHServiceError("encoding must be utf8 or base64")
        if offset < 0 or not 1 <= length <= self.max_inline_bytes:
            raise SSHServiceError("Invalid offset or length")
        def action(sftp: paramiko.SFTPClient) -> dict[str, Any]:
            with sftp.open(remote_path, "rb") as fh:
                fh.seek(offset)
                data = fh.read(length + 1)
            more = len(data) > length
            data = data[:length]
            if encoding == "utf8":
                try:
                    body = data.decode("utf-8")
                except UnicodeDecodeError:
                    raise SSHServiceError("Byte range is not valid UTF-8; use base64 or adjust offset/length") from None
            else:
                body = base64.b64encode(data).decode("ascii")
            return {"path": remote_path, "encoding": encoding, "data": body,
                    "offset": offset, "bytes_read": len(data), "has_more": more}
        return self._sftp(ident, action)

    def write_file(self, ident: str, remote_path: str, data: str, encoding: str = "utf8",
                   overwrite: bool = False, append: bool = False) -> dict[str, Any]:
        self._check_remote_path(remote_path)
        if encoding == "utf8":
            payload = data.encode("utf-8")
        elif encoding == "base64":
            try:
                payload = base64.b64decode(data, validate=True)
            except binascii.Error:
                raise SSHServiceError("Invalid base64 data") from None
        else:
            raise SSHServiceError("encoding must be utf8 or base64")
        if len(payload) > self.max_inline_bytes:
            raise SSHServiceError("File write exceeds configured size limit; use upload for larger files")
        if append and overwrite:
            raise SSHServiceError("Specify either append or overwrite")
        def action(sftp: paramiko.SFTPClient) -> dict[str, Any]:
            mode = "ab" if append else "wb" if overwrite else "xb"
            with sftp.open(remote_path, mode) as fh:
                fh.write(payload)
                fh.flush()
            return {"path": remote_path, "bytes_written": len(payload), "append": append}
        return self._sftp(ident, action)

    def mkdir(self, ident: str, remote_path: str, permissions: int = 0o755) -> dict[str, Any]:
        self._check_remote_path(remote_path)
        if not 0 <= permissions <= 0o777:
            raise SSHServiceError("Permissions must be between 0 and 0o777")
        return self._sftp(ident, lambda s: (s.mkdir(remote_path, mode=permissions), {"created": remote_path})[1])

    def rename(self, ident: str, source: str, destination: str, overwrite: bool = False) -> dict[str, Any]:
        self._check_remote_path(source)
        self._check_remote_path(destination)
        def action(sftp: paramiko.SFTPClient) -> dict[str, Any]:
            if overwrite:
                sftp.posix_rename(source, destination)  # Requires server support for posix-rename extension.
            else:
                try:
                    sftp.lstat(destination)
                except FileNotFoundError:
                    pass
                else:
                    raise SSHServiceError("Destination exists; use overwrite only if the server supports posix-rename")
                sftp.rename(source, destination)
            return {"source": source, "destination": destination}
        return self._sftp(ident, action)

    def delete(self, ident: str, remote_path: str, directory: bool = False) -> dict[str, Any]:
        self._check_remote_path(remote_path)
        def action(sftp: paramiko.SFTPClient) -> dict[str, Any]:
            if directory:
                sftp.rmdir(remote_path)  # Intentionally non-recursive.
            else:
                sftp.remove(remote_path)
            return {"deleted": remote_path, "directory": directory}
        return self._sftp(ident, action)

    def chmod(self, ident: str, remote_path: str, permissions: int) -> dict[str, Any]:
        self._check_remote_path(remote_path)
        if not 0 <= permissions <= 0o777:
            raise SSHServiceError("Permissions must be between 0 and 0o777")
        return self._sftp(ident, lambda s: (s.chmod(remote_path, permissions),
                                          {"path": remote_path, "permissions": oct(permissions)})[1])

    def upload(self, ident: str, local_path: str, remote_path: str, overwrite: bool = False) -> dict[str, Any]:
        self._check_remote_path(remote_path)
        source = self._local_path(local_path)
        if not source.is_file():
            raise SSHServiceError("Upload source file does not exist")
        def action(sftp: paramiko.SFTPClient) -> dict[str, Any]:
            if overwrite:
                sftp.put(str(source), remote_path, confirm=True)
                size = source.stat().st_size
            else:
                # Exclusive creation is atomic on SFTP servers that implement
                # SSH_FXF_EXCL; checking lstat before put would permit overwrites.
                with source.open("rb") as incoming, sftp.open(remote_path, "xb") as outgoing:
                    size = 0
                    while chunk := incoming.read(65536):
                        outgoing.write(chunk)
                        size += len(chunk)
            return {"local_path": str(source.relative_to(self.transfer_root)),
                    "remote_path": remote_path, "bytes_uploaded": size}
        return self._sftp(ident, action)

    def download(self, ident: str, remote_path: str, local_path: str, overwrite: bool = False) -> dict[str, Any]:
        self._check_remote_path(remote_path)
        destination = self._local_path(local_path)
        if destination.exists() and not overwrite:
            raise SSHServiceError("Local destination already exists")
        if not destination.parent.is_dir():
            raise SSHServiceError("Local destination parent directory does not exist")
        def action(sftp: paramiko.SFTPClient) -> dict[str, Any]:
            import tempfile
            fd, temporary = tempfile.mkstemp(prefix=".sftp-", dir=destination.parent)
            os.close(fd)
            try:
                sftp.get(remote_path, temporary)
                if overwrite:
                    os.replace(temporary, destination)
                else:
                    # Atomic create-if-absent prevents concurrent overwrites.
                    os.link(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return {"remote_path": remote_path, "local_path": str(destination.relative_to(self.transfer_root)),
                    "bytes_downloaded": destination.stat().st_size}
        return self._sftp(ident, action)