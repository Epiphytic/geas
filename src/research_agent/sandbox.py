from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class SandboxError(RuntimeError):
    pass


class BubblewrapSandbox:
    """Fail-closed runner for native parsers that communicate over stdio."""

    version = "bubblewrap-native-parser/1"
    allowed_executable_roots = (Path("/usr"),)

    def __init__(
        self,
        *,
        bubblewrap: str | None = None,
        prlimit: str | None = None,
    ) -> None:
        self.bubblewrap = bubblewrap or shutil.which("bwrap")
        self.prlimit = prlimit or shutil.which("prlimit")

    def command(
        self,
        executable: str,
        arguments: tuple[str, ...],
    ) -> tuple[str, ...]:
        if self.bubblewrap is None:
            raise SandboxError("Bubblewrap is required for native document parsing")
        if self.prlimit is None:
            raise SandboxError("prlimit is required for native document parsing")
        parser = Path(executable).resolve()
        if not parser.is_absolute() or not any(
            parser.is_relative_to(root) for root in self.allowed_executable_roots
        ):
            raise SandboxError("native parser executable is outside allowed system roots")
        return (
            self.bubblewrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--unshare-user",
            "--disable-userns",
            "--clearenv",
            "--cap-drop",
            "ALL",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind-try",
            "/bin",
            "/bin",
            "--ro-bind-try",
            "/lib",
            "/lib",
            "--ro-bind-try",
            "/lib64",
            "/lib64",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--chdir",
            "/tmp",
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--setenv",
            "HOME",
            "/nonexistent",
            "--",
            self.prlimit,
            "--as=536870912",
            "--cpu=30",
            "--nproc=64",
            "--nofile=64",
            "--core=0",
            "--",
            str(parser),
            *arguments,
        )

    def run(
        self,
        executable: str,
        arguments: tuple[str, ...],
        *,
        input_bytes: bytes,
        timeout_seconds: int = 35,
        max_output_bytes: int = 25_000_000,
    ) -> bytes:
        command = self.command(executable, arguments)
        try:
            completed = subprocess.run(
                command,
                input=input_bytes,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
                close_fds=True,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
        except (OSError, subprocess.TimeoutExpired):
            raise SandboxError("native parser sandbox could not complete") from None
        if completed.returncode != 0:
            raise SandboxError("native parser sandbox rejected or failed the document")
        if len(completed.stdout) > max_output_bytes:
            raise SandboxError("native parser output exceeds the sandbox limit")
        return completed.stdout
