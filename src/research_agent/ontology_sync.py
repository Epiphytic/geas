from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field

from research_agent.models import StrictModel, utc_now
from research_agent.ontology_subscriptions import OntologySubscription
from research_agent.user_config import OntologyGitConfig

if os.name == "nt":
    import msvcrt
else:
    import fcntl

DEFAULT_ONTOLOGY_GITIGNORE = """# Geas runtime and credential material
.env
.env.*
!.env.example
*.key
*.pem
*.p12
*.pfx
*.sqlite
*.sqlite-*
*credentials*
*secret*
data/
.geas-artifacts/
model-prompts.jsonl
model-reasoning-debug.jsonl
ontology-build.log.jsonl
ontology-build-state.json
"""

_SENSITIVE_NAME = re.compile(
    r"(?i)(^|/)(?:\.env(?:\..*)?|.*(?:credential|secret).*)$|\.(?:key|pem|p12|pfx)$"
)
_SENSITIVE_CONTENT = (
    re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(rb"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}\b"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        rb"(?im)^\s*[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)\s*[:=]"
        rb"\s*['\"]?[^\s'\"]{12,}"
    ),
)
_GIT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class OntologySyncError(RuntimeError):
    pass


class OntologyFreshnessState(StrictModel):
    version: Literal[1] = 1
    repository: str
    branch: str
    checked_at: datetime
    remote_commit: str | None = None
    local_commit: str | None = None


class OntologyFreshnessReceipt(StrictModel):
    state: str
    checked: bool
    fresh: bool
    checked_at: datetime
    next_check_at: datetime
    max_age_seconds: int = Field(ge=60)
    local_commit: str | None = None
    remote_commit: str | None = None
    pull: dict[str, object] | None = None


class OntologyRepositoryManager:
    def __init__(self, *, checkout: Path, config: OntologyGitConfig | OntologySubscription) -> None:
        self.checkout = checkout.expanduser().resolve()
        self.config = config

    def pull(self) -> dict[str, object]:
        cloned = self._ensure_checkout()
        self._assert_remote()
        old_commit = self._head()
        if old_commit is not None and not cloned:
            self._assert_active_checkout()
        if self._status(ignore_generated_gitignore=True):
            raise OntologySyncError(
                "ontology checkout has local changes; commit/push or restore them before pull"
            )
        fetched_ref = self._fetched_ref()
        expected_object = (
            self._active_ref()
            if _GIT_ID.fullmatch(self._active_ref())
            else self._advertised_object()
        )
        if expected_object is None and not self._is_branch_ref():
            raise OntologySyncError(
                f"configured ontology ref {self._active_ref()!r} does not exist on the remote"
            )
        if expected_object is None and old_commit is not None:
            raise OntologySyncError(
                f"configured ontology branch {self._branch_name()!r} does not exist on the remote"
            )
        exists = expected_object is not None
        if exists:
            source = self._active_ref()
            self._run(
                (
                    "git",
                    "fetch",
                    "--no-tags",
                    self.config.remote,
                    f"+{source}:{fetched_ref}",
                )
            )
            fetched_object = self._run(("git", "rev-parse", "--verify", fetched_ref)).stdout.strip()
            if fetched_object != expected_object:
                raise OntologySyncError("fetched ontology ref does not match the advertised object")
            fetched_commit = self._run(
                ("git", "rev-parse", "--verify", f"{fetched_ref}^{{commit}}")
            ).stdout.strip()
            if not _GIT_ID.fullmatch(fetched_commit):
                raise OntologySyncError("fetched ontology ref is not a full Git commit ID")
            if self._is_branch_ref():
                branch = self._branch_name()
                if self._has_head():
                    current = self._run(("git", "branch", "--show-current")).stdout.strip()
                    if cloned and current != branch:
                        self._run(("git", "checkout", "-B", branch, fetched_commit))
                    else:
                        self._run(("git", "merge", "--ff-only", fetched_commit))
                else:
                    self._run(("git", "checkout", "-B", branch, fetched_commit))
            else:
                self._run(("git", "checkout", "--detach", fetched_commit))
            integrated_commit = self._head()
            if integrated_commit != fetched_commit:
                raise OntologySyncError(
                    "ontology checkout HEAD does not match the exact fetched commit"
                )
            if self._status(ignore_generated_gitignore=True):
                raise OntologySyncError(
                    "ontology checkout changed after synchronization; refusing downstream work"
                )
        else:
            self._set_unborn_branch()
        self.ensure_gitignore()
        new_commit = self._head()
        return {
            "checkout": str(self.checkout),
            "repository": self.config.url,
            "branch": self._branch_name() if self._is_branch_ref() else self._active_ref(),
            "active_ref": self._active_ref(),
            "cloned": cloned,
            "pulled": exists,
            "old_commit": old_commit,
            "new_commit": new_commit,
            "commit": new_commit,
        }

    def freshen(
        self,
        *,
        state_path: Path,
        max_age_seconds: int = 3600,
        force: bool = False,
        clock: Callable[[], datetime] = utc_now,
    ) -> OntologyFreshnessReceipt:
        """Check and fast-forward at most once per freshness window.

        The state is operational cache metadata only. It never changes ontology
        authority and is written outside the ontology checkout by callers.
        """
        if not 60 <= max_age_seconds <= 604_800:
            raise OntologySyncError(
                "ontology freshness max_age_seconds must be between 60 and 604800"
            )
        state_path = state_path.expanduser()
        if state_path.is_symlink():
            raise OntologySyncError("ontology freshness state cannot be a symbolic link")
        state_path = state_path.resolve()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = state_path.with_suffix(f"{state_path.suffix}.lock")
        if lock_path.is_symlink():
            raise OntologySyncError("ontology freshness lock cannot be a symbolic link")
        with _exclusive_file_lock(lock_path):
            now = clock()
            if now.tzinfo is None:
                raise OntologySyncError("ontology freshness clock must be timezone-aware")
            now = now.astimezone(UTC)
            state = self._load_freshness_state(state_path)
            if not force and self._state_is_fresh(
                state,
                now=now,
                max_age_seconds=max_age_seconds,
            ):
                assert state is not None
                return OntologyFreshnessReceipt(
                    state=str(state_path),
                    checked=False,
                    fresh=True,
                    checked_at=state.checked_at,
                    next_check_at=datetime.fromtimestamp(
                        state.checked_at.timestamp() + max_age_seconds,
                        tz=UTC,
                    ),
                    max_age_seconds=max_age_seconds,
                    local_commit=self._head() if self.checkout.exists() else None,
                    remote_commit=state.remote_commit,
                )
            pull = self.pull()
            remote_commit = self._remote_head()
            local_commit = self._head()
            next_state = OntologyFreshnessState(
                repository=self.config.url,
                branch=self._active_ref(),
                checked_at=now,
                remote_commit=remote_commit,
                local_commit=local_commit,
            )
            self._write_freshness_state(state_path, next_state)
            return OntologyFreshnessReceipt(
                state=str(state_path),
                checked=True,
                fresh=True,
                checked_at=now,
                next_check_at=datetime.fromtimestamp(
                    now.timestamp() + max_age_seconds,
                    tz=UTC,
                ),
                max_age_seconds=max_age_seconds,
                local_commit=local_commit,
                remote_commit=remote_commit,
                pull=pull,
            )

    def push(
        self,
        *,
        relative_paths: tuple[Path, ...] = (),
        message: str = "geas: update ontologies",
        freshness_state_path: Path | None = None,
    ) -> dict[str, object]:
        if not self._is_branch_ref():
            raise OntologySyncError(
                "tag and commit ontology refs are read-only; push requires a branch"
            )
        self._ensure_checkout()
        self._assert_remote()
        self._set_unborn_branch()
        self.ensure_gitignore()
        targets = (Path(".gitignore"), *relative_paths)
        for target in targets:
            if target.is_absolute() or ".." in target.parts:
                raise OntologySyncError("ontology push path must remain checkout-relative")
        self._run(("git", "add", "-A", "--", *(item.as_posix() for item in targets)))
        all_staged = tuple(
            Path(line)
            for line in self._run(("git", "diff", "--cached", "--name-only")).stdout.splitlines()
            if line
        )
        unexpected = tuple(path for path in all_staged if not self._within_targets(path, targets))
        if unexpected:
            names = ", ".join(path.as_posix() for path in unexpected)
            raise OntologySyncError(f"refusing to include previously staged paths: {names}")
        scanned = tuple(
            Path(line)
            for line in self._run(
                ("git", "diff", "--cached", "--name-only", "--diff-filter=ACMR")
            ).stdout.splitlines()
            if line
        )
        self._scan_staged(scanned)
        changed = bool(self._run(("git", "diff", "--cached", "--quiet"), check=False).returncode)
        if changed:
            self._run(("git", "commit", "-m", message))
            self._run(
                (
                    "git",
                    "push",
                    "--set-upstream",
                    self.config.remote,
                    f"HEAD:{self._active_ref()}",
                )
            )
            head = self._head()
            assert head is not None
            self._run(("git", "update-ref", self._fetched_ref(), head))
            if freshness_state_path is not None:
                self._record_successful_push(freshness_state_path)
        return {
            "checkout": str(self.checkout),
            "repository": self.config.url,
            "branch": self._branch_name(),
            "active_ref": self._active_ref(),
            "changed": changed,
            "pushed": changed,
            "commit": self._head(),
            "staged_paths": tuple(path.as_posix() for path in all_staged),
        }

    def assert_removable(self) -> None:
        """Validate exact identity and clean synchronized state before removal."""
        if not (self.checkout / ".git").is_dir():
            raise OntologySyncError("subscription checkout is not a Git repository")
        self._assert_checkout_root()
        self._assert_remote(create_missing=False)
        self._assert_active_checkout()
        changes = self._status(ignore_generated_gitignore=True)
        if changes:
            raise OntologySyncError(
                "ontology checkout has local changes; preserve it or restore them before removal"
            )
        head = self._head()
        synchronized = self._remote_head()
        if head is None or synchronized is None or head != synchronized:
            raise OntologySyncError(
                "ontology checkout does not match its exact last synchronized commit"
            )

    def ensure_gitignore(self) -> Path:
        path = self.checkout / ".gitignore"
        if path.is_symlink():
            raise OntologySyncError("ontology repository .gitignore cannot be a symbolic link")
        if not path.exists():
            path.write_text(DEFAULT_ONTOLOGY_GITIGNORE)
        return path

    def _ensure_checkout(self) -> bool:
        if (self.checkout / ".git").is_dir():
            self._assert_checkout_root()
            return False
        if self.checkout.exists() and any(self.checkout.iterdir()):
            raise OntologySyncError(
                "ontology directory is non-empty but is not a Git checkout; move it aside "
                "or configure a different profile ontology_directory before initial pull"
            )
        self.checkout.parent.mkdir(parents=True, exist_ok=True)
        self._run_external(("git", "clone", self.config.url, str(self.checkout)))
        self._assert_checkout_root()
        return True

    def _assert_checkout_root(self) -> None:
        top = Path(self._run(("git", "rev-parse", "--show-toplevel")).stdout.strip()).resolve()
        if top != self.checkout:
            raise OntologySyncError("configured ontology directory is not the Git checkout root")

    def _assert_remote(self, *, create_missing: bool = True) -> None:
        result = self._run(
            ("git", "remote", "get-url", self.config.remote),
            check=False,
        )
        if result.returncode != 0:
            if create_missing:
                self._run(("git", "remote", "add", self.config.remote, self.config.url))
                return
            raise OntologySyncError(f"ontology remote identity {self.config.remote!r} is missing")
        if _normalized_url(result.stdout.strip()) != _normalized_url(self.config.url):
            raise OntologySyncError(
                f"ontology remote {self.config.remote!r} does not match the configured URL"
            )

    def _set_unborn_branch(self) -> None:
        if self._has_head():
            self._assert_active_checkout()
            return
        self._run(("git", "symbolic-ref", "HEAD", self._active_ref()))

    def _has_head(self) -> bool:
        return self._run(("git", "rev-parse", "--verify", "HEAD"), check=False).returncode == 0

    def _head(self) -> str | None:
        result = self._run(("git", "rev-parse", "--verify", "HEAD"), check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def _remote_head(self) -> str | None:
        result = self._run(
            ("git", "rev-parse", "--verify", f"{self._fetched_ref()}^{{commit}}"),
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def _assert_active_checkout(self) -> None:
        current = self._run(("git", "branch", "--show-current")).stdout.strip()
        if self._is_branch_ref() and current != self._branch_name():
            raise OntologySyncError(
                f"ontology checkout is on branch {current!r}, expected {self._branch_name()!r}"
            )
        if not self._is_branch_ref() and current:
            raise OntologySyncError(
                f"ontology checkout is on branch {current!r}, expected a detached read-only ref"
            )

    def _fetched_ref(self) -> str:
        if self._is_branch_ref():
            return f"refs/geas-sync/{self._branch_name()}"
        digest = hashlib.sha256(self._active_ref().encode()).hexdigest()
        return f"refs/geas-sync/{digest}"

    def _active_ref(self) -> str:
        return self.config.active_ref

    def _is_branch_ref(self) -> bool:
        return self._active_ref().startswith("refs/heads/")

    def _branch_name(self) -> str:
        if not self._is_branch_ref():
            raise OntologySyncError("configured ontology ref is not a writable branch")
        return self._active_ref().removeprefix("refs/heads/")

    def _advertised_object(self) -> str | None:
        active_ref = self._active_ref()
        arguments = (
            (self.config.remote, active_ref)
            if not _GIT_ID.fullmatch(active_ref)
            else (self.config.remote,)
        )
        result = self._run(("git", "ls-remote", "--exit-code", *arguments), check=False)
        if result.returncode == 2:
            return None
        if result.returncode != 0:
            raise OntologySyncError(
                "Git ls-remote failed for the configured ontology remote; "
                "check network access and authentication"
            )
        matches: set[str] = set()
        for line in result.stdout.splitlines():
            object_id, _, ref = line.partition("\t")
            if (_GIT_ID.fullmatch(active_ref) and object_id == active_ref) or ref == active_ref:
                matches.add(object_id)
        if len(matches) != 1:
            return None
        match = next(iter(matches))
        return match if _GIT_ID.fullmatch(match) else None

    def _load_freshness_state(self, path: Path) -> OntologyFreshnessState | None:
        if not path.exists():
            return None
        if not path.is_file():
            raise OntologySyncError("ontology freshness state must be a regular file")
        try:
            return OntologyFreshnessState.model_validate_json(path.read_text())
        except ValueError as error:
            raise OntologySyncError(f"invalid ontology freshness state: {path}") from error

    def _record_successful_push(self, state_path: Path) -> None:
        expanded = state_path.expanduser()
        if expanded.is_symlink():
            raise OntologySyncError("ontology freshness state cannot be a symbolic link")
        path = expanded.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(f"{path.suffix}.lock")
        if lock_path.is_symlink():
            raise OntologySyncError("ontology freshness lock cannot be a symbolic link")
        with _exclusive_file_lock(lock_path):
            now = utc_now().astimezone(UTC)
            head = self._head()
            self._write_freshness_state(
                path,
                OntologyFreshnessState(
                    repository=self.config.url,
                    branch=self._active_ref(),
                    checked_at=now,
                    remote_commit=head,
                    local_commit=head,
                ),
            )

    def _state_is_fresh(
        self,
        state: OntologyFreshnessState | None,
        *,
        now: datetime,
        max_age_seconds: int,
    ) -> bool:
        if state is None:
            return False
        if not (self.checkout / ".git").is_dir():
            return False
        if _normalized_url(state.repository) != _normalized_url(
            self.config.url
        ) or state.branch not in {
            self._active_ref(),
            self._branch_name() if self._is_branch_ref() else "",
        }:
            return False
        age = now.timestamp() - state.checked_at.timestamp()
        return 0 <= age < max_age_seconds

    @staticmethod
    def _write_freshness_state(path: Path, state: OntologyFreshnessState) -> None:
        temporary = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
        try:
            temporary.write_text(
                json.dumps(state.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _status(self, *, ignore_generated_gitignore: bool = False) -> tuple[str, ...]:
        lines = tuple(
            line for line in self._run(("git", "status", "--porcelain")).stdout.splitlines() if line
        )
        if ignore_generated_gitignore:
            ignore = self.checkout / ".gitignore"
            generated = (
                not ignore.is_symlink()
                and ignore.is_file()
                and ignore.read_text() == DEFAULT_ONTOLOGY_GITIGNORE
            )
            return tuple(line for line in lines if line != "?? .gitignore" or not generated)
        return lines

    def _scan_staged(self, paths: tuple[Path, ...]) -> None:
        for relative in paths:
            name = relative.as_posix()
            if _SENSITIVE_NAME.search(name):
                raise OntologySyncError(f"refusing to push credential-like path: {name}")
            path = self.checkout / relative
            if path.is_symlink():
                raise OntologySyncError(f"refusing to push symbolic link: {name}")
            if not path.exists():
                continue
            if path.stat().st_size > 10_000_000:
                raise OntologySyncError(f"refusing to scan oversized ontology file: {name}")
            content = path.read_bytes()
            if b"\x00" in content:
                raise OntologySyncError(f"refusing to push binary ontology file: {name}")
            if any(pattern.search(content) for pattern in _SENSITIVE_CONTENT):
                raise OntologySyncError(f"possible credential detected; refusing to push: {name}")

    @staticmethod
    def _within_targets(path: Path, targets: tuple[Path, ...]) -> bool:
        return any(path == target or path.is_relative_to(target) for target in targets)

    def _run(
        self,
        command: tuple[str, ...],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return self._execute(command, cwd=self.checkout, check=check)

    def _run_external(self, command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return self._execute(command, cwd=self.checkout.parent, check=True)

    @staticmethod
    def _execute(
        command: tuple[str, ...],
        *,
        cwd: Path,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": os.devnull,
        }
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        if check and completed.returncode != 0:
            detail = completed.stderr.strip()[-2000:]
            raise OntologySyncError(f"Git command failed ({command[0]} {command[1]}): {detail}")
        return completed


def _normalized_url(value: str) -> str:
    return value.rstrip("/").removesuffix(".git")


@contextmanager
def _exclusive_file_lock(path: Path):
    with path.open("a+b") as lock:
        if os.name == "nt":
            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)
