import base64
import json
import os
from pathlib import Path

import pytest


def connect(service, **kwargs):
    return service.connect(host="example.org", username="alice", password="secret", **kwargs)["connection_id"]


def test_connect_execute_and_disconnect(service):
    ident = connect(service)
    entries = service.connections()
    assert entries[0]["host"] == "example.org"
    assert len(ident) >= 24
    result = service.execute(ident, "whoami", stdin="hello", timeout_seconds=5)
    assert result == {"exit_code": 0, "stdout": "executed: whoami", "stderr": "", "truncated": False,
                      "timed_out": False}
    assert service._sessions[ident].client.transport.commands[0].sent == b"hello"
    assert service.execute(ident, "stderr-output")["exit_code"] == 7
    assert service.disconnect(ident)["disconnected"]
    with pytest.raises(ValueError, match="not found"):
        service.execute(ident, "ls")


def test_connect_validations(service):
    base = dict(host="example.org", username="alice")
    with pytest.raises(ValueError, match="exactly one"):
        service.connect(**base)
    with pytest.raises(ValueError, match="exactly one"):
        service.connect(**base, password="x", private_key="text")
    with pytest.raises(ValueError, match="Port"):
        service.connect(**base, password="x", port=0)
    with pytest.raises(ValueError, match="connection failed"):
        service.connect(**base, password="bad")
    with pytest.raises(ValueError, match="private key file"):
        service.connect(**base, key_path="missing")
    with pytest.raises(ValueError, match="SSH key path"):
        service.connect(**base, key_path="../escape")
    (service.key_root / "id_ed25519").write_text("dummy")
    ident = service.connect(**base, key_path="id_ed25519")["connection_id"]
    assert service._sessions[ident].client.kwargs["key_filename"].endswith("id_ed25519")
    ident2 = service.connect(**base, private_key="-----BEGIN KEY-----")["connection_id"]
    assert "pkey" in service._sessions[ident2].client.kwargs


def test_strict_host_keys(service):
    service.known_hosts.unlink()
    with pytest.raises(ValueError, match="known_hosts"):
        connect(service)


def test_output_and_input_limits(service):
    ident = connect(service)
    service.max_output_bytes = 4
    result = service.execute(ident, "large-output")
    assert result["truncated"] and result["exit_code"] is None
    assert result["stdout"] == "1234"
    service.max_inline_bytes = 3
    with pytest.raises(ValueError, match="input exceeds"):
        service.execute(ident, "cat", stdin="four")
    with pytest.raises(ValueError, match="timeout_seconds"):
        service.execute(ident, "cat", timeout_seconds=999)


def test_sftp_all_core_operations(service):
    ident = connect(service)
    assert [x["name"] for x in service.listdir(ident, "/")["entries"]] == ["a.txt", "b.bin"]
    assert service.stat(ident, "/a.txt")["size"] == len("hello 世界".encode())
    text = service.read_file(ident, "/a.txt")
    assert text["data"] == "hello 世界"
    assert not text["has_more"]
    segment = service.read_file(ident, "/a.txt", "base64", offset=0, length=4)
    assert segment["has_more"]
    assert base64.b64decode(segment["data"]) == b"hell"
    with pytest.raises(ValueError, match="UTF-8"):
        service.read_file(ident, "/b.bin")
    binary = service.read_file(ident, "/b.bin", "base64")
    assert binary["data"] == "AP8="
    service.write_file(ident, "/new.txt", "content")
    with pytest.raises(ValueError, match="exists"):
        service.write_file(ident, "/new.txt", "replace")
    service.write_file(ident, "/new.txt", "added", append=True)
    assert service.read_file(ident, "/new.txt")["data"] == "contentadded"
    service.write_file(ident, "/new.txt", "cmVwbGFjZWQ=", encoding="base64", overwrite=True)
    assert service.read_file(ident, "/new.txt")["data"] == "replaced"
    service.rename(ident, "/new.txt", "/moved.txt")
    service.chmod(ident, "/moved.txt", 0o600)
    assert service.stat(ident, "/moved.txt")["size"] == 8
    service.mkdir(ident, "/empty")
    service.delete(ident, "/empty", directory=True)
    service.delete(ident, "/moved.txt")
    assert "/moved.txt" not in service._sessions[ident].client.fs


def test_sftp_upload_and_download_with_root_restrictions(service):
    ident = connect(service)
    root = service.transfer_root
    (root / "upload.bin").write_bytes(b"\0\1\2")
    service.upload(ident, "upload.bin", "/uploaded.bin")
    assert service._sessions[ident].client.fs["/uploaded.bin"] == b"\0\1\2"
    with pytest.raises(ValueError, match="already exists"):
        service.upload(ident, "upload.bin", "/uploaded.bin")
    result = service.download(ident, "/uploaded.bin", "received.bin")
    assert result["bytes_downloaded"] == 3
    assert (root / "received.bin").read_bytes() == b"\0\1\2"
    with pytest.raises(ValueError, match="already exists"):
        service.download(ident, "/uploaded.bin", "received.bin")
    for path in ("../escape", "/etc/passwd", "nested/../../escape"):
        with pytest.raises(ValueError, match="relative|escapes"):
            service.download(ident, "/uploaded.bin", path)
    (root / "link").symlink_to(root.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        service.upload(ident, "link/known_hosts", "/bad")


def test_invalid_payload_and_remote_paths(service):
    ident = connect(service)
    with pytest.raises(ValueError, match="base64"):
        service.write_file(ident, "/x", "?", encoding="base64")
    with pytest.raises(ValueError, match="Invalid offset"):
        service.read_file(ident, "/x", offset=-1)
    with pytest.raises(ValueError, match="Remote path"):
        service.delete(ident, "")
    with pytest.raises(ValueError, match="Permissions"):
        service.chmod(ident, "/a.txt", 0o1000)


def test_session_expiry_and_connection_cap(service, paramiko_stub):
    ticks = [100]
    service._clock = lambda: ticks[0]
    service.ttl = 5
    service.max_sessions = 1
    ident = connect(service)
    with pytest.raises(ValueError, match="Maximum"):
        connect(service)
    ticks[0] = 110
    assert service.connections() == []
    assert service._sessions.get(ident) is None
    ident2 = connect(service)
    assert ident2 != ident


def test_profile_env_and_private_file(tmp_path, paramiko_stub, monkeypatch):
    folder = tmp_path / "keys"
    folder.mkdir()
    (folder / "key.pem").write_text("test")
    known = tmp_path / "known_hosts"
    known.write_text("test host key")
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps({"password-host": {"host": "10.0.0.2", "username": "admin",
                                                     "password_env": "MY_TEST_PASSWORD"},
                                    "key-host": {"host": "10.0.0.3", "username": "user",
                                                 "key_path": "key.pem"}}))
    os.chmod(profiles, 0o600)
    monkeypatch.setenv("MY_TEST_PASSWORD", "protected")
    service = paramiko_stub.SSHService(known_hosts=str(known), key_root=str(folder),
                                       transfer_root=str(tmp_path / "transfers"),
                                       profiles_file=str(profiles))
    assert service.profiles()["password-host"]["auth_method"] == "password"
    a = service.connect(profile="password-host")["connection_id"]
    assert service._sessions[a].client.kwargs["password"] == "protected"
    b = service.connect(profile="key-host")["connection_id"]
    assert service._sessions[b].client.kwargs["key_filename"].endswith("key.pem")
    with pytest.raises(ValueError, match="omit other"):
        service.connect(profile="password-host", username="root")
    os.chmod(profiles, 0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        paramiko_stub.SSHService(known_hosts=str(known), profiles_file=str(profiles),
                                 transfer_root=str(tmp_path / "transfers2"))


def test_command_drains_stdout_before_sending_stdin(service):
    """SSH channel window backpressure must not deadlock standard input."""
    from conftest import FakeChannel

    ident = connect(service)

    class BackpressureChannel(FakeChannel):
        def exec_command(self, command):
            self.stdout = b"ready"
            self.exit_code = 0

        def send_ready(self):
            return not self.stdout

        def exit_status_ready(self):
            return self.sent == b"hello"

        def send(self, data):
            assert not self.stdout
            return super().send(data)

    channel = BackpressureChannel()
    service._sessions[ident].client.transport.open_session = lambda timeout: channel
    result = service.execute(ident, "cat", stdin="hello", timeout_seconds=2)
    assert result["exit_code"] == 0
    assert result["stdout"] == "ready"
    assert channel.sent == b"hello"
    assert channel.closed


def test_expired_connection_remains_open_during_active_operation(service):
    import threading

    ident = connect(service)
    started, finished = threading.Event(), threading.Event()
    clock = [10.0]
    service._clock = lambda: clock[0]
    service.ttl = 1
    failures = []

    def operation(_):
        started.set()
        assert finished.wait(timeout=3)

    def worker():
        try:
            service._with_connection(ident, operation)
        except Exception as error:
            failures.append(error)

    t = threading.Thread(target=worker)
    t.start()
    try:
        assert started.wait(timeout=3)
        clock[0] = 30.0
        assert service.connections()[0]["connection_id"] == ident
        assert not service._sessions[ident].client.closed
    finally:
        finished.set()
        t.join(timeout=3)
    assert not t.is_alive() and not failures
    assert service.connections() == []


def test_upload_does_not_replace_existing_remote_file(service):
    ident = connect(service)
    (service.transfer_root / "incoming.txt").write_text("replacement")
    with pytest.raises(ValueError, match="exists"):
        service.upload(ident, "incoming.txt", "/a.txt")
    assert service.read_file(ident, "/a.txt")["data"] == "hello 世界"


def test_download_no_overwrite_is_atomic(service, monkeypatch):
    import ssh_mcp.service as impl

    ident = connect(service)
    original_link = impl.os.link
    dest = service.transfer_root / "raced.txt"

    def racing_link(source, destination):
        dest.write_bytes(b"from-another-writer")
        return original_link(source, destination)

    monkeypatch.setattr(impl.os, "link", racing_link)
    with pytest.raises(ValueError, match="exists"):
        service.download(ident, "/a.txt", "raced.txt")
    assert dest.read_bytes() == b"from-another-writer"
    assert not list(service.transfer_root.glob(".sftp-*"))


def test_directory_listing_limit(service):
    ident = connect(service)
    service._sessions[ident].client.fs.update({f"/file{i}.txt": b"x" for i in range(1001)})
    with pytest.raises(ValueError, match="1000"):
        service.listdir(ident, "/")