"""Role-classified, capability-gated Git repository publication."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Protocol

from research_agent.capabilities import (
    Capability,
    CapabilityDecision,
    CapabilityGrant,
    _https_url,
)
from research_agent.git_environment import (
    confined_git_environment,
    confined_github_environment,
    github_cli_config_directory,
)
from research_agent.publishing import (
    PathRole,
    ProducerReceiptVerifier,
    PublicationManifest,
    PublicationManifestPath,
    PublishMode,
    PublishRequest,
    PublishResult,
    capability_decision_set_sha256,
    classify_managed_path,
    required_capabilities,
)

_GIT_OBJECT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_GITHUB_SLUG_PART = re.compile(r"^[A-Za-z0-9._-]+$")


class PublicationError(PermissionError):
    """A publication boundary rejected the requested mutation."""


class ForgeClient(Protocol):
    def upsert_pull_request(
        self,
        *,
        repository: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> str: ...

    def enable_auto_merge(
        self,
        *,
        repository: str,
        head: str,
        pull_request_url: str,
    ) -> None: ...


class PromotionVerifier(Protocol):
    def verify(
        self,
        *,
        repository: Path,
        commit: str,
        canonical_ref: str,
        paths: tuple[str, ...],
    ) -> None: ...


class GitRemoteTransport(Protocol):
    """Transport an exact repository endpoint; injected fakes keep tests offline."""

    def ls_remote(self, *, endpoint: str, ref: str) -> str: ...

    def push(
        self,
        *,
        endpoint: str,
        commit: str,
        ref: str,
        expected: str | None,
    ) -> subprocess.CompletedProcess[str]: ...


class GitHubCliForgeClient:
    """Narrow noninteractive GitHub PR adapter over an exact ``gh`` executable."""

    def __init__(
        self,
        *,
        executable: str,
        runner: Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]] | None = None,
        config_directory: Path | None = None,
        auth_environment: Mapping[str, str] | None = None,
    ) -> None:
        path = Path(executable)
        if not path.is_absolute():
            raise ValueError("GitHub CLI executable must be an absolute path")
        self.executable = str(path)
        self.runner = runner
        self.config_directory = github_cli_config_directory(config_directory)
        self.auth_environment = dict(auth_environment or {})

    def assert_authenticated(self, *, repository: str) -> None:
        """Fail closed unless ``gh`` has one active GitHub authentication context."""
        slug = self._slug(repository)
        self._checked(
            (
                self.executable,
                "auth",
                "status",
                "--hostname",
                "github.com",
            ),
            repository=f"github.com/{slug}",
        )

    def upsert_pull_request(
        self,
        *,
        repository: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> str:
        slug = self._slug(repository)
        repository_identifier = f"github.com/{slug}"
        self._branch(head)
        self._branch(base)
        query = (
            self.executable,
            "pr",
            "list",
            "--repo",
            repository_identifier,
            "--head",
            head,
            "--base",
            base,
            "--state",
            "open",
            "--json",
            "url,headRefName,baseRefName,isCrossRepository",
            "--limit",
            "2",
        )
        listed = self._checked(query, repository=repository_identifier)
        try:
            values = json.loads(listed.stdout)
        except (json.JSONDecodeError, TypeError):
            raise PublicationError(
                "GitHub CLI returned an invalid pull-request inventory"
            ) from None
        if not isinstance(values, list) or len(values) > 1:
            raise PublicationError("GitHub CLI returned an ambiguous pull-request inventory")
        if values:
            item = values[0]
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("url"), str)
                or item.get("headRefName") != head
                or item.get("baseRefName") != base
                or item.get("isCrossRepository") is not False
            ):
                raise PublicationError("GitHub CLI returned an invalid pull-request identity")
            url = self._pull_request_url(slug, item["url"])
            self._checked(
                (
                    self.executable,
                    "pr",
                    "edit",
                    url,
                    "--repo",
                    repository_identifier,
                    "--title",
                    title,
                    "--body",
                    body,
                ),
                repository=repository_identifier,
            )
            return url
        created = self._checked(
            (
                self.executable,
                "pr",
                "create",
                "--repo",
                repository_identifier,
                "--head",
                head,
                "--base",
                base,
                "--title",
                title,
                "--body",
                body,
            ),
            repository=repository_identifier,
        )
        return self._pull_request_url(slug, created.stdout.strip())

    def enable_auto_merge(
        self,
        *,
        repository: str,
        head: str,
        pull_request_url: str,
    ) -> None:
        slug = self._slug(repository)
        repository_identifier = f"github.com/{slug}"
        self._branch(head)
        url = self._pull_request_url(slug, pull_request_url)
        self._checked(
            (
                self.executable,
                "pr",
                "merge",
                url,
                "--repo",
                repository_identifier,
                "--auto",
                "--merge",
            ),
            repository=repository_identifier,
        )

    def _checked(
        self,
        command: tuple[str, ...],
        *,
        repository: str,
    ) -> subprocess.CompletedProcess[str]:
        result = (
            self.runner(command)
            if self.runner is not None
            else self._run(command, repository=repository)
        )
        if result.returncode != 0:
            raise PublicationError("GitHub CLI operation failed")
        return result

    def _run(
        self,
        command: tuple[str, ...],
        *,
        repository: str,
    ) -> subprocess.CompletedProcess[str]:
        environment = confined_github_environment(
            repository=repository,
            config_directory=self.config_directory,
            auth_environment=self.auth_environment,
        )
        return subprocess.run(
            command,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    @staticmethod
    def _slug(repository: str) -> str:
        normalized = _https_url(repository, label="publication repository")
        parsed = urllib.parse.urlsplit(normalized)
        parts = tuple(item for item in parsed.path.split("/") if item)
        if (
            parsed.hostname != "github.com"
            or len(parts) != 2
            or any(
                item in {".", ".."} or _GITHUB_SLUG_PART.fullmatch(item) is None
                for item in parts
            )
        ):
            if parsed.hostname == "github.com" and len(parts) == 2:
                raise PublicationError("GitHub repository slug is invalid")
            raise PublicationError("GitHub CLI publication requires a GitHub repository")
        return "/".join(parts)

    @staticmethod
    def _branch(value: str) -> str:
        if (
            _BRANCH.fullmatch(value) is None
            or ".." in value
            or "//" in value
            or value.endswith(("/", ".lock"))
        ):
            raise PublicationError("GitHub pull-request branch is invalid")
        return value

    @staticmethod
    def _pull_request_url(slug: str, value: str) -> str:
        pattern = rf"^https://github\.com/{re.escape(slug)}/pull/[1-9][0-9]*$"
        if re.fullmatch(pattern, value) is None:
            raise PublicationError("GitHub CLI returned an unexpected pull-request URL")
        return value


class GitRepositoryPublisher:
    """Publish exact manifest-owned paths without using the operator's Git index."""

    def __init__(
        self,
        *,
        repository: Path,
        manifests: Sequence[PublicationManifest],
        capability_decision: CapabilityDecision | Sequence[CapabilityDecision],
        forge: ForgeClient | None,
        now: Callable[[], datetime],
        direct_push: bool = False,
        canonical_ref: str = "refs/heads/main",
        remote: str = "origin",
        grants: Mapping[str, CapabilityGrant] | None = None,
        promotion_verifier: PromotionVerifier | None = None,
        receipt_verifier: ProducerReceiptVerifier | None = None,
        remote_transport: GitRemoteTransport | None = None,
    ) -> None:
        self.repository = repository.resolve()
        self.manifests = tuple(manifests)
        self.capability_decisions = (
            (capability_decision,)
            if isinstance(capability_decision, CapabilityDecision)
            else tuple(capability_decision)
        )
        self.forge = forge
        self.now = now
        self.direct_push = direct_push
        self.canonical_ref = canonical_ref
        self.remote = remote
        self.grants = dict(grants or {})
        self.promotion_verifier = promotion_verifier
        self.receipt_verifier = receipt_verifier
        self.remote_transport = remote_transport

    def publish(self, request: PublishRequest) -> PublishResult:
        if request.mode is PublishMode.NONE:
            return PublishResult(
                request_id=request.id,
                published=False,
                reason="publication-disabled",
                completed_at=self.now(),
            )
        self._verify_producer_receipts()
        items = self._verified_manifest_items(request)
        canonical = request.target_ref == self.canonical_ref
        required = self._authorize(request, items, canonical=canonical)
        endpoint = self._resolve_remote_endpoint(request.repository)
        if request.mode is PublishMode.DIRECT_PUSH:
            return self._publish_direct(
                request,
                items,
                endpoint=endpoint,
                required=required,
            )
        return self._publish_pull_request(
            request,
            items,
            endpoint=endpoint,
            required=required,
            auto_merge=request.mode is PublishMode.AUTO_MERGE,
        )

    def _verify_producer_receipts(self) -> None:
        if self.receipt_verifier is None:
            raise PublicationError("publication requires a verified producer receipt verifier")
        validated: list[PublicationManifest] = []
        for supplied in self.manifests:
            try:
                manifest = PublicationManifest.model_validate(
                    supplied.model_dump(mode="json")
                )
            except Exception as error:
                raise PublicationError("publication manifest schema did not revalidate") from error
            try:
                self.receipt_verifier.verify(manifest)
            except Exception as error:
                raise PublicationError(
                    "producer receipt did not verify the exact manifest"
                ) from error
            validated.append(manifest)
        self.manifests = tuple(validated)

    def _verified_manifest_items(
        self, request: PublishRequest
    ) -> tuple[PublicationManifestPath, ...]:
        manifest_items = {item.path: item for manifest in self.manifests for item in manifest.paths}
        if sum(len(manifest.paths) for manifest in self.manifests) != len(manifest_items):
            raise PublicationError("publication manifests contain duplicate paths")
        result: list[PublicationManifestPath] = []
        for requested in request.paths:
            classified = classify_managed_path(requested.path, manifests=self.manifests)
            if classified in {PathRole.RUNTIME_STORE, PathRole.UNCLASSIFIED}:
                raise PublicationError("publish path is not classified as a managed artifact")
            if requested.role is not classified:
                raise PublicationError("publish path role does not match manifest classification")
            item = manifest_items.get(requested.path)
            if item is None:
                raise PublicationError("publish path is not owned by an exact artifact manifest")
            if item.role is not classified:
                raise PublicationError("manifest role does not match path classification")
            result.append(item)
        return tuple(result)

    def _authorize(
        self,
        request: PublishRequest,
        items: Sequence[PublicationManifestPath],
        *,
        canonical: bool,
    ) -> frozenset[Capability]:
        required: set[Capability] = set()
        for item in items:
            item_required = required_capabilities(
                item.role,
                request.mode,
                canonical_target=canonical,
            )
            if item_required is None:
                raise PublicationError("path role is forbidden for this publication mode")
            required.update(item_required)
        decisions = self.capability_decisions
        try:
            digest = capability_decision_set_sha256(decisions)
        except ValueError as error:
            raise PublicationError("capability decision set is invalid") from error
        if request.capability_decision_sha256 != digest:
            raise PublicationError("publish request does not match its capability decision")
        by_path = {decision.request.path: decision for decision in decisions}
        if set(by_path) != {item.path for item in items}:
            raise PublicationError("capability decision does not authorize exact publication")
        for item in items:
            decision = by_path[item.path]
            decided = decision.request
            item_required = required_capabilities(
                item.role,
                request.mode,
                canonical_target=canonical,
            )
            if (
                item_required is None
                or decision.decision != "allow"
                or decided.target_repository != request.repository
                or decided.ref != request.target_ref
                or decided.path != item.path
                or not item_required.issubset(decision.effective_capabilities)
            ):
                raise PublicationError("capability decision does not authorize exact publication")
            if Capability.GIT_DIRECT_PUSH in item_required and decision.delegation_chain:
                self._verify_delegated_direct_push(decision)
        return frozenset(required)

    def _verify_delegated_direct_push(self, decision: CapabilityDecision) -> None:
        if not decision.grant_ids:
            raise PublicationError("delegated direct push has no verified grant chain")
        for grant_id in decision.grant_ids:
            grant = self.grants.get(grant_id)
            if grant is None or Capability.GIT_DIRECT_PUSH not in grant.capabilities:
                raise PublicationError("delegated direct push is absent from a capability grant")
            if Capability.GIT_DIRECT_PUSH not in grant.delegable_capabilities:
                raise PublicationError(
                    "delegated direct push is absent from delegable capabilities"
                )

    def _publish_pull_request(
        self,
        request: PublishRequest,
        items: Sequence[PublicationManifestPath],
        *,
        endpoint: str,
        required: frozenset[Capability],
        auto_merge: bool,
    ) -> PublishResult:
        if self.forge is None:
            raise PublicationError("pull-request publication requires an injected forge client")
        base = self._verified_local_commit(request.target_ref)
        identity = self._publication_identity(request, items)
        commit = self._build_commit(request, items, parent=base, identity=identity)
        if Capability.KNOWLEDGE_AUTO_PROMOTE in required:
            self._verify_promotion(commit, request, items)
        branch = f"geas/publish/{identity[:20]}"
        branch_ref = f"refs/heads/{branch}"
        expected = self._remote_object(endpoint, branch_ref)
        if expected != commit:
            self._push(commit, branch_ref, expected, endpoint=endpoint)
        title = f"geas: publish managed changes {identity[:12]}"
        body = (
            f"Deterministic Geas publication `{request.id}`.\n\n"
            f"Publication identity: `{identity}`.\n\n"
            f"Capability decision: `{request.capability_decision_sha256}`."
        )
        url = self.forge.upsert_pull_request(
            repository=request.repository,
            head=branch,
            base=request.target_ref.removeprefix("refs/heads/"),
            title=title,
            body=body,
        )
        if auto_merge:
            self.forge.enable_auto_merge(
                repository=request.repository,
                head=branch,
                pull_request_url=url,
            )
        return PublishResult(
            request_id=request.id,
            published=True,
            branch=branch,
            commit_sha256=commit,
            pull_request_url=url,
            reason="auto-merge-requested" if auto_merge else "pull-request-upserted",
            completed_at=self.now(),
        )

    def _publish_direct(
        self,
        request: PublishRequest,
        items: Sequence[PublicationManifestPath],
        *,
        endpoint: str,
        required: frozenset[Capability],
    ) -> PublishResult:
        if not self.direct_push:
            raise PublicationError("direct push requires the explicit direct-push flag")
        if not request.target_ref.startswith("refs/heads/"):
            raise PublicationError("direct push requires a writable branch ref")
        active = self._git_result("symbolic-ref", "-q", "HEAD", check=False)
        if active.returncode != 0 or active.stdout.strip() != request.target_ref:
            raise PublicationError("direct push requires HEAD on the exact target branch")
        self._require_only_owned_changes(items)
        expected = self._remote_object(endpoint, request.target_ref)
        local = self._verified_local_commit(request.target_ref)
        head = self._verified_local_commit("HEAD")
        if expected is None or local != expected or head != local:
            raise PublicationError("direct-push target moved or is not freshly verified")
        identity = self._publication_identity(request, items)
        commit = self._build_commit(request, items, parent=local, identity=identity)
        if Capability.KNOWLEDGE_AUTO_PROMOTE in required:
            self._verify_promotion(commit, request, items)
        self._push(commit, request.target_ref, expected, endpoint=endpoint)
        return PublishResult(
            request_id=request.id,
            published=True,
            branch=request.target_ref.removeprefix("refs/heads/"),
            commit_sha256=commit,
            reason="direct-push-completed",
            completed_at=self.now(),
        )

    def _verify_promotion(
        self,
        commit: str,
        request: PublishRequest,
        items: Sequence[PublicationManifestPath],
    ) -> None:
        if self.promotion_verifier is None:
            raise PublicationError(
                "canonical semantic publication requires existing promotion verification"
            )
        self.promotion_verifier.verify(
            repository=self.repository,
            commit=commit,
            canonical_ref=request.target_ref,
            paths=tuple(item.path for item in items),
        )

    def _require_only_owned_changes(self, items: Sequence[PublicationManifestPath]) -> None:
        allowed = {item.path for item in items}
        status = self._git("status", "--porcelain=v1", "-z", "--untracked-files=all")
        entries = tuple(entry for entry in status.split("\x00") if entry)
        changed: set[str] = set()
        for entry in entries:
            if len(entry) < 4:
                raise PublicationError("Git status returned an invalid path")
            changed.add(entry[3:])
        if not changed or not changed.issubset(allowed):
            raise PublicationError("direct push requires only manifest-owned local changes")

    def _build_commit(
        self,
        request: PublishRequest,
        items: Sequence[PublicationManifestPath],
        *,
        parent: str,
        identity: str,
    ) -> str:
        root = self._verified_root()
        with tempfile.TemporaryDirectory(prefix="geas-publish-") as temporary:
            index = Path(temporary) / "index"
            environment = {"GIT_INDEX_FILE": str(index)}
            self._git("read-tree", parent, extra_env=environment)
            for item in items:
                self._assert_safe_regular_path(root, item.path)
                data = (root / item.path).read_bytes()
                if hashlib.sha256(data).hexdigest() != item.sha256:
                    raise PublicationError("managed path bytes do not match the exact manifest")
                blob = (
                    self._git_bytes(
                        "hash-object",
                        "-w",
                        "--no-filters",
                        "--stdin",
                        input_bytes=data,
                    )
                    .decode("ascii")
                    .strip()
                )
                self._git(
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"100644,{blob},{item.path}",
                    extra_env=environment,
                )
            tree = self._git("write-tree", extra_env=environment)
        message = f"{request.message or f'geas: publish managed changes {identity[:12]}'}\n"
        date = request.created_at.isoformat()
        commit = (
            self._git_bytes(
                "commit-tree",
                tree,
                "-p",
                parent,
                input_bytes=message.encode("utf-8"),
                extra_env={
                    "GIT_AUTHOR_NAME": "Geas Publisher",
                    "GIT_AUTHOR_EMAIL": "geas-publisher@users.noreply.github.com",
                    "GIT_COMMITTER_NAME": "Geas Publisher",
                    "GIT_COMMITTER_EMAIL": "geas-publisher@users.noreply.github.com",
                    "GIT_AUTHOR_DATE": date,
                    "GIT_COMMITTER_DATE": date,
                },
            )
            .decode("ascii")
            .strip()
        )
        return self._object_id(commit, label="publication commit")

    def _publication_identity(
        self,
        request: PublishRequest,
        items: Sequence[PublicationManifestPath],
    ) -> str:
        selected = {item.path for item in items}
        receipts = sorted(
            {
                manifest.receipt_sha256
                for manifest in self.manifests
                if any(item.path in selected for item in manifest.paths)
            }
        )
        payload = {
            "request_id": request.id,
            "receipt_sha256": receipts,
            "paths": [
                {"path": item.path, "role": item.role.value, "sha256": item.sha256}
                for item in items
            ],
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _verified_root(self) -> Path:
        root = Path(self._git("rev-parse", "--show-toplevel")).resolve()
        if root != self.repository:
            raise PublicationError("publication repository is not its Git worktree root")
        return root

    def _verified_local_commit(self, ref: str) -> str:
        value = self._git("rev-parse", "--verify", f"{ref}^{{commit}}")
        return self._object_id(value, label="local target")

    def _resolve_remote_endpoint(self, repository: str) -> str:
        self._reject_url_rewrites()
        configured = self._configured_remote_url()
        if self._canonical_endpoint(configured) != self._canonical_endpoint(repository):
            raise PublicationError("configured repository endpoint does not match authority")
        return configured

    def _reject_url_rewrites(self) -> None:
        rewrites = self._git_result(
            "config",
            "--name-only",
            "--get-regexp",
            ".*",
            check=False,
        )
        names = tuple(name.casefold() for name in rewrites.stdout.splitlines())
        if rewrites.returncode not in {0, 1} or any(
            name.startswith("url.")
            and name.endswith((".insteadof", ".pushinsteadof"))
            for name in names
        ):
            raise PublicationError("publication repository URL rewrites are forbidden")

    def _configured_remote_url(self) -> str:
        values = self._git("config", "--get-all", f"remote.{self.remote}.url").splitlines()
        if len(values) != 1:
            raise PublicationError("publication remote must have one exact repository endpoint")
        return values[0]

    @staticmethod
    def _canonical_endpoint(value: str) -> str:
        normalized = _https_url(value, label="publication repository endpoint")
        return normalized[:-4] if normalized.endswith(".git") else normalized

    def _remote_object(self, endpoint: str, ref: str) -> str | None:
        if self.remote_transport is None:
            output = self._git("ls-remote", "--refs", endpoint, ref)
        else:
            output = self.remote_transport.ls_remote(endpoint=endpoint, ref=ref)
        lines = output.splitlines()
        if not lines:
            return None
        if len(lines) != 1:
            raise PublicationError("remote ref did not resolve exactly once")
        object_id, resolved_ref = lines[0].split("\t", 1)
        if resolved_ref != ref:
            raise PublicationError("remote ref resolution changed")
        return self._object_id(object_id, label="remote target")

    def _push(self, commit: str, ref: str, expected: str | None, *, endpoint: str) -> None:
        self._reject_url_rewrites()
        if self._configured_remote_url() != endpoint:
            raise PublicationError("publication remote configuration changed before mutation")
        lease_expected = expected if expected is not None else ""
        if self.remote_transport is None:
            result = self._git_result(
                "-c",
                "core.hooksPath=/dev/null",
                "push",
                f"--force-with-lease={ref}:{lease_expected}",
                endpoint,
                f"{commit}:{ref}",
                check=False,
            )
        else:
            result = self.remote_transport.push(
                endpoint=endpoint,
                commit=commit,
                ref=ref,
                expected=expected,
            )
        if result.returncode != 0:
            recovery = (
                f"git push --force-with-lease={ref}:{expected or ''} "
                f"{endpoint} {commit}:{ref}"
            )
            raise PublicationError(
                f"publication push failed under the exact lease; local commit {commit}; "
                f"recovery: {recovery}"
            )

    def _assert_safe_regular_path(self, root: Path, relative: str) -> None:
        current = root
        for part in PurePosixPath(relative).parts:
            current = current / part
            if current.is_symlink():
                raise PublicationError("managed publication path contains a symbolic link")
        if not current.is_file():
            raise PublicationError("managed publication path is not a regular file")
        if not current.resolve().is_relative_to(root):
            raise PublicationError("managed publication path escaped the repository")

    @staticmethod
    def _object_id(value: str, *, label: str) -> str:
        if not _GIT_OBJECT.fullmatch(value):
            raise PublicationError(f"{label} is not a full Git object ID")
        return value

    def _git(
        self,
        *arguments: str,
        extra_env: Mapping[str, str] | None = None,
    ) -> str:
        return self._git_result(*arguments, extra_env=extra_env).stdout.strip()

    def _git_bytes(
        self,
        *arguments: str,
        input_bytes: bytes,
        extra_env: Mapping[str, str] | None = None,
    ) -> bytes:
        result = subprocess.run(
            ("git", *arguments),
            cwd=self.repository,
            env=confined_git_environment(extra_env),
            input=input_bytes,
            capture_output=True,
            check=True,
        )
        return result.stdout

    def _git_result(
        self,
        *arguments: str,
        check: bool = True,
        extra_env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ("git", *arguments),
                cwd=self.repository,
                env=confined_git_environment(extra_env),
                text=True,
                capture_output=True,
                check=check,
            )
        except subprocess.CalledProcessError as error:
            raise PublicationError("bounded Git publication operation failed") from error
