"""Minimal Paramiko test double; production installs the real Paramiko package."""
import sys
import types
from pathlib import Path
import io
import os
import stat

import pytest


class FakeSSHException(Exception):
    pass


class FakeRejectPolicy:
    pass


class FakeTransport:
    def __init__(self):
        self.active = True
        self.commands = []

    def is_active(self):
        return self.active

    def open_session(self, timeout=12):
        channel = FakeChannel()
        self.commands.append(channel)
        return channel


class FakeChannel:
    def __init__(self):
        self.command = ""
        self.sent = b""
        self.exit_code = 0
        self.stdout = b""
        self.stderr = b""
        self.closed = False

    def exec_command(self, command):
        self.command = command
        if command == "large-output":
            self.stdout = b"123456789"
        elif command == "stderr-output":
            self.stderr = b"failure"
            self.exit_code = 7
        else:
            self.stdout = ("executed: " + command).encode()

    def settimeout(self, value):
        self.timeout = value

    def send_ready(self):
        return True

    def send(self, data):
        self.sent += data
        return len(data)

    def shutdown_write(self):
        pass

    def recv_ready(self):
        return bool(self.stdout)

    def recv_stderr_ready(self):
        return bool(self.stderr)

    def recv(self, size):
        data, self.stdout = self.stdout[:size], self.stdout[size:]
        return data

    def recv_stderr(self, size):
        data, self.stderr = self.stderr[:size], self.stderr[size:]
        return data

    def exit_status_ready(self):
        return True

    def recv_exit_status(self):
        return self.exit_code

    def close(self):
        self.closed = True


class FakeAttrs:
    def __init__(self, name, content, mode=stat.S_IFREG | 0o644):
        self.filename = name
        self.st_size = len(content)
        self.st_mode = mode
        self.st_mtime = 1234567


class FakeFile(io.BytesIO):
    def __init__(self, fs, path, mode):
        self.fs = fs
        self.path = path
        self.mode = mode
        self._initialized = False
        current = fs.get(path, b"")
        if "x" in mode and path in fs:
            raise FileExistsError(path)
        super().__init__(b"" if "w" in mode or "x" in mode else current)
        self._initialized = True
        if "a" in mode:
            self.seek(0, io.SEEK_END)

    def close(self):
        if self._initialized and any(flag in self.mode for flag in "wax"):
            self.fs[self.path] = self.getvalue()
        if self._initialized:
            super().close()


class FakeSFTP:
    def __init__(self, fs, dirs):
        self.fs = fs
        self.dirs = dirs
        self.closed = False

    def listdir_iter(self, root):
        yield from self.listdir_attr(root)

    def listdir_attr(self, root):
        if root not in self.dirs:
            raise FileNotFoundError(root)
        prefix = root.rstrip("/") + "/"
        return [FakeAttrs(k.removeprefix(prefix), v) for k, v in self.fs.items()
                if k.startswith(prefix) and "/" not in k[len(prefix):]]

    def lstat(self, path):
        if path in self.fs:
            return FakeAttrs(path, self.fs[path])
        if path in self.dirs:
            return FakeAttrs(path, b"", stat.S_IFDIR | 0o755)
        raise FileNotFoundError(path)

    def stat(self, path):
        return self.lstat(path)

    def open(self, path, mode):
        if "r" in mode and path not in self.fs:
            raise FileNotFoundError(path)
        return FakeFile(self.fs, path, mode)

    def mkdir(self, path, mode):
        self.dirs.add(path)

    def rename(self, a, b):
        self.fs[b] = self.fs.pop(a)

    def posix_rename(self, a, b):
        self.rename(a, b)

    def remove(self, path):
        del self.fs[path]

    def rmdir(self, path):
        self.dirs.remove(path)

    def chmod(self, path, mode):
        self.lstat(path)

    def put(self, src, dest, confirm=True):
        self.fs[dest] = Path(src).read_bytes()

    def get(self, src, dest):
        Path(dest).write_bytes(self.fs[src])

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self):
        self.transport = FakeTransport()
        self.fs = {"/a.txt": "hello 世界".encode("utf-8"), "/b.bin": b"\x00\xff"}
        self.dirs = {"/", "/tmp"}
        self.kwargs = {}
        self.closed = False
        self.loaded_keys = None

    def load_system_host_keys(self, path):
        self.loaded_keys = path

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, **kwargs):
        self.kwargs = kwargs
        if kwargs.get("password") == "bad":
            raise FakeSSHException("bad password")

    def get_transport(self):
        return self.transport

    def open_sftp(self):
        return FakeSFTP(self.fs, self.dirs)

    def close(self):
        self.closed = True
        self.transport.active = False



@pytest.fixture
def paramiko_stub(monkeypatch):
    """Paramiko's SSHClient API is simulated to run tests without network access."""
    stub = types.ModuleType("paramiko")
    stub.SSHClient = FakeClient
    stub.SFTPClient = FakeSFTP
    stub.SSHException = FakeSSHException
    stub.RejectPolicy = FakeRejectPolicy
    for key in ("Ed25519Key", "RSAKey", "ECDSAKey"):
        setattr(stub, key, type(key, (), {"from_private_key": classmethod(
            lambda cls, data, password=None: object() if "BEGIN" in data.read() else (_ for _ in ()).throw(FakeSSHException()))}))
    monkeypatch.setitem(sys.modules, "paramiko", stub)
    for name in list(sys.modules):
        if name.startswith("ssh_mcp"):
            monkeypatch.delitem(sys.modules, name, raising=False)
    import ssh_mcp.service as service
    return service


@pytest.fixture
def service(tmp_path, paramiko_stub):
    ssh_dir = tmp_path / "keys"
    ssh_dir.mkdir()
    known = tmp_path / "known_hosts"
    known.write_text("example.org ssh-ed25519 example-public-key\n")
    root = tmp_path / "transfers"
    root.mkdir()
    return paramiko_stub.SSHService(known_hosts=str(known), key_root=str(ssh_dir),
                                    transfer_root=str(root), ttl=100, client_factory=FakeClient)