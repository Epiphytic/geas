"""Trusted, explicit Geas self-update provenance and orchestration."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal, NoReturn, Protocol, cast

from research_agent.models import StrictModel

TRUSTED_GEAS_URL = "https://github.com/Epiphytic/geas.git"
TRUSTED_GEAS_BRANCH = "main"
CONTINUATION_ENV = "GEAS_UPDATE_CONTINUATION"

_GIT_ID = re.compile(r"^[0-9a-f]{40}$")
_AUTO_RECEIPT = object()


class GeasUpdateError(RuntimeError):
    """A deterministic Geas provenance or update check failed."""


class CommandRunner(Protocol):
    def __call__(
        self, command: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]: ...


class GeasInstallProvenance(StrictModel):
    """Verified local installation metadata; never sourced from a skill manifest."""

    installer: Literal["uv-tool-directory", "git-development"]
    directory: Path
    repository_url: str
    branch: str
    commit: str
    version: str


class GeasUpdateReceipt(StrictModel):
    """The exact old/new software identity carried across one re-exec hop."""

    installer: Literal["uv-tool-directory", "git-development"]
    directory: Path
    repository_url: str = TRUSTED_GEAS_URL
    branch: str = TRUSTED_GEAS_BRANCH
    old_commit: str
    new_commit: str
    old_version: str
    new_version: str
    reinstalled: bool
    reexec_depth: Literal[1]


class _Continuation(StrictModel):
    version: Literal[1] = 1
    old_commit: str
    new_commit: str
    old_version: str
    new_version: str
    depth: int


class GeasUpdater:
    """Update only a verified local checkout of the fixed Geas upstream."""

    def __init__(
        self,
        *,
        receipt_path: Path | None | object = _AUTO_RECEIPT,
        source_directory: Path | None = None,
        executable: Path | None = None,
        runner: CommandRunner = subprocess.run,
        reexec: Callable[[tuple[str, ...], Mapping[str, str]], object] | None = None,
        installed_version: Callable[[], str] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.executable = executable or _current_executable()
        if receipt_path is _AUTO_RECEIPT:
            candidate = Path(sys.executable).absolute().parent.parent / "uv-receipt.toml"
            self.receipt_path = candidate if candidate.is_file() else None
        else:
            self.receipt_path = cast(Path | None, receipt_path)
        self.source_directory = (
            source_directory.expanduser().resolve()
            if source_directory is not None
            else Path(__file__).resolve().parent
        )
        self.runner = runner
        self.reexec = reexec or _exec
        self.installed_version = installed_version or _installed_version
        self.environment = dict(os.environ if environment is None else environment)

    def inspect(self) -> GeasInstallProvenance:
        """Verify a uv directory receipt or an explicit Git development checkout."""
        if self.receipt_path is not None:
            directory = self._directory_from_uv_receipt(self.receipt_path)
            installer: Literal["uv-tool-directory", "git-development"] = "uv-tool-directory"
        else:
            if self.source_directory is None:
                raise GeasUpdateError(
                    "Geas installer provenance is unknown; update Geas manually with uv"
                )
            directory = self._git_root(self.source_directory)
            installer = "git-development"
        branch = self._git(directory, "branch", "--show-current").stdout.strip()
        if branch != TRUSTED_GEAS_BRANCH:
            raise GeasUpdateError(
                f"Geas checkout is on {branch!r}; expected trusted branch {TRUSTED_GEAS_BRANCH!r}"
            )
        remote = self._git(directory, "remote", "get-url", "origin").stdout.strip()
        if _normalized_url(remote) != _normalized_url(TRUSTED_GEAS_URL):
            raise GeasUpdateError("Geas origin does not match the trusted Geas URL")
        status = self._git(directory, "status", "--porcelain", "--untracked-files=all").stdout
        if status:
            raise GeasUpdateError("Geas checkout has local changes; commit or restore them first")
        commit = self._git(directory, "rev-parse", "--verify", "HEAD").stdout.strip()
        if not _GIT_ID.fullmatch(commit):
            raise GeasUpdateError("Geas checkout HEAD is not a full Git object ID")
        installed = self.installed_version()
        if not installed:
            raise GeasUpdateError("installed Geas version is unavailable")
        return GeasInstallProvenance(
            installer=installer,
            directory=directory,
            repository_url=TRUSTED_GEAS_URL,
            branch=TRUSTED_GEAS_BRANCH,
            commit=commit,
            version=installed,
        )

    def update_and_reexec(
        self,
        argv: Sequence[str],
        *,
        continuation: str | None,
    ) -> GeasUpdateReceipt | NoReturn:
        """Fast-forward, reinstall, and cross at most one verified re-exec boundary."""
        if not argv:
            raise GeasUpdateError("Geas re-exec requires a non-empty argument vector")
        if continuation is not None:
            return self._complete_continuation(continuation)
        if CONTINUATION_ENV in self.environment or "--geas-update-continuation" in argv:
            raise GeasUpdateError("repeated Geas update continuation marker")

        provenance = self.inspect()
        old_commit = provenance.commit
        old_version = provenance.version
        self._git(provenance.directory, "fetch", "origin", TRUSTED_GEAS_BRANCH)
        ancestor = self._git(
            provenance.directory,
            "merge-base",
            "--is-ancestor",
            "HEAD",
            f"origin/{TRUSTED_GEAS_BRANCH}",
            check=False,
        )
        if ancestor.returncode != 0:
            raise GeasUpdateError("Geas history cannot be updated by trusted fast-forward")
        self._git(
            provenance.directory,
            "merge",
            "--ff-only",
            f"origin/{TRUSTED_GEAS_BRANCH}",
        )
        new_commit = self._git(
            provenance.directory, "rev-parse", "--verify", "HEAD"
        ).stdout.strip()
        if not _GIT_ID.fullmatch(new_commit):
            raise GeasUpdateError("updated Geas HEAD is not a full Git object ID")
        new_version = _project_version(provenance.directory)
        reinstall = self._run(
            ("uv", "tool", "install", "--force", str(provenance.directory)),
            cwd=provenance.directory,
            check=False,
        )
        if reinstall.returncode != 0:
            raise GeasUpdateError(_command_error("Geas uv reinstall failed", reinstall))
        token = self.continuation_token(
            old_commit=old_commit,
            new_commit=new_commit,
            old_version=old_version,
            new_version=new_version,
        )
        command = (
            str(self.executable),
            *tuple(argv)[1:],
            "--geas-update-continuation",
            token,
        )
        environment = {**self.environment, CONTINUATION_ENV: token}
        self.reexec(command, environment)
        return GeasUpdateReceipt(
            installer=provenance.installer,
            directory=provenance.directory,
            old_commit=old_commit,
            new_commit=new_commit,
            old_version=old_version,
            new_version=new_version,
            reinstalled=True,
            reexec_depth=1,
        )

    def continuation_token(
        self,
        *,
        old_commit: str,
        new_commit: str,
        old_version: str,
        new_version: str,
        depth: int = 1,
    ) -> str:
        """Encode a non-secret, strictly validated one-hop update identity."""
        marker = _Continuation(
            old_commit=old_commit,
            new_commit=new_commit,
            old_version=old_version,
            new_version=new_version,
            depth=depth,
        )
        payload = json.dumps(
            marker.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=")

    def _complete_continuation(self, token: str) -> GeasUpdateReceipt:
        configured = self.environment.get(CONTINUATION_ENV)
        if configured != token:
            raise GeasUpdateError("Geas update continuation environment does not match the CLI")
        marker = _decode_continuation(token)
        if marker.depth != 1:
            raise GeasUpdateError("repeated Geas update continuation marker")
        if not _GIT_ID.fullmatch(marker.old_commit) or not _GIT_ID.fullmatch(marker.new_commit):
            raise GeasUpdateError("Geas update continuation contains an invalid commit")
        provenance = self.inspect()
        if provenance.commit != marker.new_commit:
            raise GeasUpdateError("post-reexec Geas commit does not match the update receipt")
        if provenance.version != marker.new_version:
            raise GeasUpdateError("post-reexec Geas version does not match the update receipt")
        return GeasUpdateReceipt(
            installer=provenance.installer,
            directory=provenance.directory,
            old_commit=marker.old_commit,
            new_commit=marker.new_commit,
            old_version=marker.old_version,
            new_version=marker.new_version,
            reinstalled=True,
            reexec_depth=1,
        )

    def _directory_from_uv_receipt(self, receipt_path: Path) -> Path:
        path = receipt_path.expanduser()
        if path.is_symlink() or not path.is_file():
            raise GeasUpdateError("Geas uv receipt is missing or unsafe")
        try:
            data = tomllib.loads(path.read_text())
            tool = data["tool"]
            requirements = tool["requirements"]
        except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
            raise GeasUpdateError("Geas uv receipt is malformed") from error
        if not isinstance(requirements, list) or len(requirements) != 1:
            raise GeasUpdateError("Geas uv receipt must contain one directory requirement")
        requirement = requirements[0]
        if (
            not isinstance(requirement, dict)
            or set(requirement) != {"name", "directory"}
            or requirement.get("name") != "geas"
            or not isinstance(requirement.get("directory"), str)
            or not requirement["directory"]
        ):
            raise GeasUpdateError("Geas uv receipt must contain one directory requirement")
        raw_directory = Path(requirement["directory"]).expanduser()
        if not raw_directory.is_absolute() or _has_symlink_ancestry(raw_directory):
            raise GeasUpdateError("Geas uv receipt directory must be an absolute safe path")
        directory = raw_directory.resolve()
        if not directory.is_dir():
            raise GeasUpdateError("Geas uv receipt directory does not exist")
        return directory

    def _git_root(self, source: Path) -> Path:
        result = self._run(
            ("git", "rev-parse", "--show-toplevel"), cwd=source, check=False
        )
        if result.returncode != 0:
            raise GeasUpdateError("Geas installer provenance is unknown; update Geas manually")
        root = Path(result.stdout.strip()).resolve()
        if not root.is_dir():
            raise GeasUpdateError("Geas development checkout is missing")
        return root

    def _git(
        self,
        directory: Path,
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = self._run(("git", *arguments), cwd=directory, check=False)
        if check and result.returncode != 0:
            label = (
                "Geas Git fetch failed"
                if arguments[:1] == ("fetch",)
                else "Geas Git check failed"
            )
            raise GeasUpdateError(_command_error(label, result))
        return result

    def _run(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        environment = {**self.environment, "GIT_TERMINAL_PROMPT": "0"}
        try:
            result = self.runner(
                command,
                cwd=cwd,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
        except (FileNotFoundError, OSError) as error:
            raise GeasUpdateError(f"required executable is unavailable: {command[0]}") from error
        if check and result.returncode != 0:
            raise GeasUpdateError(_command_error("Geas update command failed", result))
        return result


def _decode_continuation(token: str) -> _Continuation:
    try:
        padding = "=" * (-len(token) % 4)
        payload = base64.b64decode(token + padding, altchars=b"-_", validate=True)
        marker = _Continuation.model_validate_json(payload)
    except Exception as error:
        raise GeasUpdateError("Geas update continuation marker is invalid") from error
    return marker


def _normalized_url(value: str) -> str:
    return value.rstrip("/").removesuffix(".git")


def _has_symlink_ancestry(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False


def _project_version(directory: Path) -> str:
    path = directory / "pyproject.toml"
    if path.is_symlink() or not path.is_file():
        raise GeasUpdateError("updated Geas checkout has no safe pyproject.toml")
    try:
        project = tomllib.loads(path.read_text())["project"]
        name = project["name"]
        value = project["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise GeasUpdateError("updated Geas project metadata is invalid") from error
    if name != "geas" or not isinstance(value, str) or not value:
        raise GeasUpdateError("updated Geas project metadata is invalid")
    return value


def _command_error(prefix: str, result: subprocess.CompletedProcess[str]) -> str:
    detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    return f"{prefix}: {detail}"


def _installed_version() -> str:
    try:
        return version("geas")
    except PackageNotFoundError as error:
        raise GeasUpdateError("installed Geas version is unavailable") from error


def _current_executable() -> Path:
    candidate = shutil.which("geas")
    if candidate is None:
        raise GeasUpdateError("the Geas executable cannot be resolved for re-exec")
    return Path(candidate).resolve()


def _exec(command: tuple[str, ...], environment: Mapping[str, str]) -> NoReturn:
    os.execvpe(command[0], command, dict(environment))
