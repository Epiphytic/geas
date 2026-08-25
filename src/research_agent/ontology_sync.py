from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from research_agent.user_config import OntologyGitConfig

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


class OntologySyncError(RuntimeError):
    pass


class OntologyRepositoryManager:
    def __init__(self, *, checkout: Path, config: OntologyGitConfig) -> None:
        self.checkout = checkout.expanduser().resolve()
        self.config = config

    def pull(self) -> dict[str, object]:
        cloned = self._ensure_checkout()
        self._assert_remote()
        if self._status(ignore_generated_gitignore=True):
            raise OntologySyncError(
                "ontology checkout has local changes; commit/push or restore them before pull"
            )
        remote_branch = f"{self.config.remote}/{self.config.branch}"
        exists = self._run(
            (
                "git",
                "ls-remote",
                "--exit-code",
                "--heads",
                self.config.remote,
                self.config.branch,
            ),
            check=False,
        ).returncode == 0
        if exists:
            self._run(("git", "fetch", self.config.remote, self.config.branch))
            if self._has_head():
                self._run(("git", "merge", "--ff-only", remote_branch))
            else:
                self._run(("git", "checkout", "-B", self.config.branch, remote_branch))
        else:
            self._set_unborn_branch()
        self.ensure_gitignore()
        return {
            "checkout": str(self.checkout),
            "repository": self.config.url,
            "branch": self.config.branch,
            "cloned": cloned,
            "pulled": exists,
            "commit": self._head(),
        }

    def push(
        self,
        *,
        relative_paths: tuple[Path, ...] = (),
        message: str = "geas: update ontologies",
    ) -> dict[str, object]:
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
            for line in self._run(
                ("git", "diff", "--cached", "--name-only")
            ).stdout.splitlines()
            if line
        )
        unexpected = tuple(
            path for path in all_staged if not self._within_targets(path, targets)
        )
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
        changed = bool(
            self._run(("git", "diff", "--cached", "--quiet"), check=False).returncode
        )
        if changed:
            self._run(("git", "commit", "-m", message))
            self._run(
                (
                    "git",
                    "push",
                    "--set-upstream",
                    self.config.remote,
                    self.config.branch,
                )
            )
        return {
            "checkout": str(self.checkout),
            "repository": self.config.url,
            "branch": self.config.branch,
            "changed": changed,
            "pushed": changed,
            "commit": self._head(),
            "staged_paths": tuple(path.as_posix() for path in all_staged),
        }

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

    def _assert_remote(self) -> None:
        result = self._run(
            ("git", "remote", "get-url", self.config.remote),
            check=False,
        )
        if result.returncode != 0:
            self._run(("git", "remote", "add", self.config.remote, self.config.url))
            return
        if _normalized_url(result.stdout.strip()) != _normalized_url(self.config.url):
            raise OntologySyncError(
                f"ontology remote {self.config.remote!r} does not match the configured URL"
            )

    def _set_unborn_branch(self) -> None:
        if self._has_head():
            current = self._run(("git", "branch", "--show-current")).stdout.strip()
            if current != self.config.branch:
                raise OntologySyncError(
                    f"ontology checkout is on {current!r}, expected {self.config.branch!r}"
                )
            return
        self._run(("git", "symbolic-ref", "HEAD", f"refs/heads/{self.config.branch}"))

    def _has_head(self) -> bool:
        return self._run(("git", "rev-parse", "--verify", "HEAD"), check=False).returncode == 0

    def _head(self) -> str | None:
        result = self._run(("git", "rev-parse", "--verify", "HEAD"), check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def _status(self, *, ignore_generated_gitignore: bool = False) -> tuple[str, ...]:
        lines = tuple(
            line
            for line in self._run(("git", "status", "--porcelain")).stdout.splitlines()
            if line
        )
        if ignore_generated_gitignore:
            return tuple(line for line in lines if line != "?? .gitignore")
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
        environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
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
            raise OntologySyncError(
                f"Git command failed ({command[0]} {command[1]}): {detail}"
            )
        return completed


def _normalized_url(value: str) -> str:
    return value.rstrip("/").removesuffix(".git")
