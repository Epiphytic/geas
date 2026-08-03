import shutil
import subprocess
from pathlib import Path

import pytest

from research_agent.sandbox import BubblewrapSandbox, SandboxError


def test_bubblewrap_command_has_no_host_content_mount_or_network() -> None:
    sandbox = BubblewrapSandbox(
        bubblewrap="/usr/bin/bwrap",
        prlimit="/usr/bin/prlimit",
    )

    command = sandbox.command("/usr/bin/pdftotext", ("-", "-"))

    assert "--unshare-all" in command
    assert "--share-net" not in command
    assert "--clearenv" in command
    assert "--cap-drop" in command
    assert command[
        command.index("--tmpfs") : command.index("--tmpfs") + 2
    ] == ("--tmpfs", "/tmp")
    assert str(Path.cwd()) not in command
    assert "/home" not in command
    assert command[-3:] == ("/usr/bin/pdftotext", "-", "-")


def test_bubblewrap_runner_passes_document_only_on_stdin(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, b"derived text", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    sandbox = BubblewrapSandbox(
        bubblewrap="/usr/bin/bwrap",
        prlimit="/usr/bin/prlimit",
    )

    output = sandbox.run(
        "/usr/bin/pdftotext",
        ("-", "-"),
        input_bytes=b"original bytes",
    )

    assert output == b"derived text"
    assert observed["input"] == b"original bytes"
    assert observed["close_fds"] is True
    assert observed["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    assert "original bytes" not in observed["command"]


def test_native_executables_outside_system_roots_are_rejected(tmp_path) -> None:
    executable = tmp_path / "parser"
    executable.touch()
    sandbox = BubblewrapSandbox(
        bubblewrap="/usr/bin/bwrap",
        prlimit="/usr/bin/prlimit",
    )

    with pytest.raises(SandboxError, match="outside allowed system roots"):
        sandbox.command(str(executable), ())


def _working_native_sandbox() -> BubblewrapSandbox:
    if not shutil.which("bwrap") or not shutil.which("prlimit"):
        pytest.skip("Bubblewrap native parser prerequisites are unavailable")
    sandbox = BubblewrapSandbox()
    try:
        sandbox.run("/usr/bin/true", (), input_bytes=b"", timeout_seconds=2)
    except SandboxError:
        pytest.skip("this host does not permit the required Bubblewrap namespaces")
    return sandbox


def test_live_sandbox_cannot_see_repository_or_host_etc() -> None:
    sandbox = _working_native_sandbox()
    script = (
        f"test ! -e {Path.cwd()!s} && "
        "test ! -e /etc/passwd && "
        "test \"$(pwd)\" = /tmp"
    )

    sandbox.run("/usr/bin/sh", ("-c", script), input_bytes=b"")


def test_live_sandbox_has_no_network_route() -> None:
    sandbox = _working_native_sandbox()

    with pytest.raises(SandboxError):
        sandbox.run(
            "/usr/bin/python3",
            (
                "-c",
                'import socket; socket.create_connection(("1.1.1.1", 443), timeout=1)',
            ),
            input_bytes=b"",
            timeout_seconds=3,
        )
