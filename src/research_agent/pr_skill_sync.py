"""Deterministic PR skill artifacts and path-confined protected write-back."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator

from research_agent.agent_skills import (
    install_builtin_geas_skill,
    install_snapshot,
    validate_snapshot,
)
from research_agent.catalog_skill_export import (
    export_catalog_skill,
    selection_from_repository_catalog,
)
from research_agent.models import StrictModel
from research_agent.ontology_artifacts import (
    OntologyArtifact,
    _sqlite_input_revision,
)
from research_agent.ontology_subscriptions import (
    OntologyFreshnessConfig,
    OntologySubscription,
    normalize_active_ref,
)

REPOSITORY = "Epiphytic/geas"
REPOSITORY_ID = "1320458746"
REGENERATION_WORKFLOW = "PR Skill Regeneration"
REGENERATION_WORKFLOW_PATH = ".github/workflows/pr-skill-regeneration.yml"
WRITEBACK_WORKFLOW_REF = (
    r"Epiphytic/geas/\.github/workflows/pr-skill-writeback\.yml@refs/heads/main"
)
ALLOWED_SKILL_ROOTS = (
    ".agents/skills/geas",
    ".agents/skills/open-source-research-agents",
)
_MANIFEST_NAME = "manifest.json"
_PAYLOAD_NAME = "payload"
_GIT_ID = re.compile(r"[0-9a-f]{40}")
_REPOSITORY_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_FILES = 1024
_MAX_DIRECTORIES = 2048
_MAX_FILE_BYTES = 10 * 1024 * 1024
_MAX_TOTAL_BYTES = 100 * 1024 * 1024


class ArtifactSource(StrictModel):
    repository: Literal["Epiphytic/geas"] = REPOSITORY
    repository_id: Literal["1320458746"] = REPOSITORY_ID
    workflow: Literal["PR Skill Regeneration"] = REGENERATION_WORKFLOW
    workflow_path: Literal[
        ".github/workflows/pr-skill-regeneration.yml"
    ] = REGENERATION_WORKFLOW_PATH
    run_id: int = Field(gt=0)
    pull_request: int = Field(gt=0)
    head_repository: str
    head_ref: str
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")

    @field_validator("head_repository")
    @classmethod
    def head_repository_is_safe(cls, value: str) -> str:
        if not _REPOSITORY_NAME.fullmatch(value):
            raise ValueError("head repository is invalid")
        return value

    @field_validator("head_ref")
    @classmethod
    def head_ref_is_safe(cls, value: str) -> str:
        if value.startswith("refs/"):
            raise ValueError("head branch must not include refs/heads")
        normalize_active_ref(f"refs/heads/{value}")
        return value


class ArtifactFile(StrictModel):
    path: str
    mode: Literal["100644"] = "100644"
    size_bytes: int = Field(ge=0, le=_MAX_FILE_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def path_is_safe_and_allowed(cls, value: str) -> str:
        _validate_payload_path(value)
        return value


class SnapshotIdentity(StrictModel):
    name: Literal["geas", "open-source-research-agents"]
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PullRequestSnapshotManifest(StrictModel):
    format_version: Literal[1] = 1
    source: ArtifactSource
    snapshots: tuple[SnapshotIdentity, ...]
    files: tuple[ArtifactFile, ...]

    @model_validator(mode="after")
    def inventory_is_closed_and_canonical(self) -> PullRequestSnapshotManifest:
        names = tuple(item.name for item in self.snapshots)
        if names != ("geas", "open-source-research-agents"):
            raise ValueError("snapshot identities must name the two exact managed skills")
        paths = tuple(item.path for item in self.files)
        if len(paths) > _MAX_FILES:
            raise ValueError("artifact contains too many files")
        if len(paths) != len(set(paths)):
            raise ValueError("artifact file inventory contains duplicate paths")
        if paths != tuple(sorted(paths, key=lambda value: value.encode("utf-8"))):
            raise ValueError("artifact file inventory must be sorted")
        if sum(item.size_bytes for item in self.files) > _MAX_TOTAL_BYTES:
            raise ValueError("artifact total size exceeds the limit")
        roots = {_root_for_path(path) for path in paths}
        if roots != set(ALLOWED_SKILL_ROOTS):
            raise ValueError("artifact inventory must contain both exact skill roots")
        return self

    @property
    def snapshot_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.snapshots)


class WorkflowRunDecision(StrictModel):
    source: ArtifactSource
    writeback: bool
    reason: Literal[
        "same-repository-success",
        "fork-pull-request",
        "source-run-failed",
    ]


class PullRequestVerification(StrictModel):
    number: int
    head_repository: str
    head_ref: str
    head_sha: str


class WritebackReceipt(StrictModel):
    changed: bool
    pushed: bool
    staged_roots: tuple[str, ...] = ()
    commit: str | None = None


class OrgStsPolicy(StrictModel):
    issuer: Literal["https://token.actions.githubusercontent.com"]
    subject: Literal[
        "repo:Epiphytic@228616596/geas@1320458746:ref:refs/heads/main"
    ]
    claim_patterns: dict[str, str]
    permissions: dict[str, str]
    repositories: tuple[str, ...]

    @model_validator(mode="after")
    def policy_is_exact(self) -> OrgStsPolicy:
        expected_claims = {
            "repository_id": REPOSITORY_ID,
            "event_name": "workflow_run",
            "workflow_ref": WRITEBACK_WORKFLOW_REF,
        }
        if self.claim_patterns != expected_claims:
            raise ValueError("Octo STS policy claim patterns are not exact")
        if self.permissions != {"contents": "write"}:
            raise ValueError("Octo STS policy permissions are not exact")
        if self.repositories != ("geas",):
            raise ValueError("Octo STS policy repository scope is not exact")
        return self


def build_skill_artifact(
    snapshots_root: Path,
    destination: Path,
    *,
    source: ArtifactSource,
) -> PullRequestSnapshotManifest:
    """Copy two validated snapshots into one canonical, manifest-bound artifact."""
    snapshots_root = snapshots_root.resolve()
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("artifact destination must not already exist")
    destination.mkdir(parents=True)
    payload = destination / _PAYLOAD_NAME
    payload.mkdir()
    identities: list[SnapshotIdentity] = []
    try:
        for root_value in ALLOWED_SKILL_ROOTS:
            source_root = snapshots_root / root_value
            _assert_regular_tree(source_root, label="generated skill snapshot")
            skill_manifest = validate_snapshot(source_root)
            expected_name = PurePosixPath(root_value).name
            if skill_manifest.skill.name != expected_name:
                raise ValueError("generated skill snapshot has the wrong identity")
            target_root = payload / root_value
            _copy_regular_tree(source_root, target_root)
            manifest_path = target_root / "geas-skill.json"
            identities.append(
                SnapshotIdentity(
                    name=expected_name,
                    snapshot_sha256=skill_manifest.snapshot_sha256,
                    manifest_sha256=_sha256_file(manifest_path),
                )
            )
        files = _artifact_inventory(payload)
        manifest = PullRequestSnapshotManifest(
            source=source,
            snapshots=tuple(identities),
            files=files,
        )
        (destination / _MANIFEST_NAME).write_bytes(_canonical_json(manifest))
        return manifest
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def generate_repository_skill_snapshots(
    repository: Path,
    destination: Path,
    *,
    source: ArtifactSource,
    projection: Path,
) -> tuple[SnapshotIdentity, ...]:
    """Render the exact generic and maintained catalog skill from verified inputs."""
    root = repository.resolve()
    if Path(_git(root, "rev-parse", "--show-toplevel").stdout.strip()).resolve() != root:
        raise ValueError("skill generation repository is not its Git worktree root")
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    if head != source.head_sha:
        raise ValueError("skill generation worktree is not at the source head SHA")
    if _git(root, "status", "--porcelain", "--untracked-files=all").stdout:
        raise ValueError("skill generation worktree has local changes")
    projection = projection.resolve()
    if projection.is_symlink() or not projection.is_file():
        raise ValueError("preseeded knowledge projection is missing or unsafe")
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("snapshot generation destination must not already exist")
    effective_commit = effective_source_commit(root, source.head_sha)
    ontology_name = PurePosixPath(ALLOWED_SKILL_ROOTS[1]).name
    subscription = OntologySubscription(
        url=f"https://github.com/{source.repository}.git",
        active_ref=f"refs/heads/{source.head_ref}",
        checkout=Path("ci-pr-skill-sync"),
        catalog=Path("geas.yaml"),
        freshness=OntologyFreshnessConfig(check_before_use=False),
    )
    selection = selection_from_repository_catalog(
        root / subscription.catalog,
        ontology_name=ontology_name,
        subscription_name="geas-pr-skill-sync",
        subscription=subscription,
        commit=effective_commit,
    )
    try:
        destination.mkdir(parents=True)
        with tempfile.TemporaryDirectory(
            prefix="geas-pr-skill-generic-",
            dir=destination.parent,
        ) as temporary:
            temporary_root = Path(temporary)
            receipt = install_builtin_geas_skill(
                config_root=temporary_root / "config",
                home=temporary_root / "home",
                which=lambda _name: None,
            )
            if len(receipt.installed) != 1:
                raise ValueError("packaged generic skill did not install exactly once")
            generic_target = destination / ALLOWED_SKILL_ROOTS[0]
            _copy_regular_tree(receipt.installed[0], generic_target)

        with tempfile.TemporaryDirectory(
            prefix="geas-pr-skill-artifact-",
            dir=destination.parent,
        ) as temporary:
            exported = export_catalog_skill(
                selection,
                artifact_store=_PreseededProjectionStore(projection),
                skill_name=ontology_name,
                geas_version=_installed_version(),
                geas_commit=effective_commit,
                artifact_workspace=Path(temporary) / "artifacts",
            )
            install_snapshot(exported.files, destination / ALLOWED_SKILL_ROOTS[1])
        return tuple(
            _snapshot_identity(destination / root_value) for root_value in ALLOWED_SKILL_ROOTS
        )
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


class _PreseededProjectionStore:
    """Hydrate from one verified local projection without network or publication."""

    def __init__(self, projection: Path) -> None:
        self.projection = projection

    def available(self, _artifact: OntologyArtifact) -> bool:
        return True

    def ensure(self, _artifact: OntologyArtifact, _source: Path) -> bool:
        raise AssertionError("PR skill generation must not publish ontology artifacts")

    def download(self, artifact: OntologyArtifact, destination: Path) -> None:
        source = self.projection
        if source.is_symlink() or not source.is_file():
            raise ValueError("preseeded knowledge projection is missing or unsafe")
        if source.stat().st_size != artifact.size_bytes:
            raise ValueError("preseeded knowledge projection has the wrong size")
        if _sha256_file(source) != artifact.content_sha256:
            raise ValueError("preseeded knowledge projection has the wrong content address")
        if _sqlite_input_revision(source, artifact.role) != artifact.input_revision:
            raise ValueError("preseeded knowledge projection has the wrong input revision")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(0o644)


def generate_pull_request_artifact(
    repository: Path,
    artifact_root: Path,
    *,
    source: ArtifactSource,
) -> PullRequestSnapshotManifest:
    """Build one preseeded projection and independently render the snapshots twice."""
    root = repository.resolve()
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    if head != source.head_sha:
        raise ValueError("artifact generation checkout does not match the PR head")
    if _git(root, "status", "--porcelain", "--untracked-files=all").stdout:
        raise ValueError("artifact generation checkout has local changes")
    artifact_root = artifact_root.absolute()
    if artifact_root.exists() or artifact_root.is_symlink():
        raise ValueError("artifact generation destination must not already exist")
    with tempfile.TemporaryDirectory(
        prefix="geas-pr-skill-generation-",
        dir=root.parent,
    ) as temporary:
        temporary_root = Path(temporary)
        demo_root = temporary_root / "demo"
        demo = root / "ontology" / "open-source-research-agents" / "demo.sh"
        try:
            subprocess.run(
                (str(demo), str(demo_root)),
                cwd=root,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                text=True,
                capture_output=True,
                check=True,
            )
        except subprocess.CalledProcessError as error:
            raise RuntimeError("offline maintained projection generation failed") from error
        projection = demo_root / "query.sqlite"
        first = temporary_root / "first"
        second = temporary_root / "second"
        first_identities = generate_repository_skill_snapshots(
            root,
            first,
            source=source,
            projection=projection,
        )
        second_identities = generate_repository_skill_snapshots(
            root,
            second,
            source=source,
            projection=projection,
        )
        if first_identities != second_identities:
            raise ValueError("independent skill generations produced different identities")
        if _snapshot_state(first) != _snapshot_state(second):
            raise ValueError("independent skill generations produced different bytes")
        return build_skill_artifact(first, artifact_root, source=source)


def verify_skill_artifact(
    artifact_root: Path,
    *,
    expected: ArtifactSource,
) -> PullRequestSnapshotManifest:
    """Verify bounded JSON and regular bytes without executing artifact content."""
    root = artifact_root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("skill artifact root is missing or unsafe")
    entries = {entry.name for entry in os.scandir(root)}
    if entries != {_MANIFEST_NAME, _PAYLOAD_NAME}:
        raise ValueError("skill artifact top-level inventory is not closed")
    manifest_path = root / _MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("skill artifact manifest is missing or unsafe")
    if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise ValueError("skill artifact manifest exceeds the size limit")
    encoded = manifest_path.read_bytes()
    try:
        manifest = PullRequestSnapshotManifest.model_validate_json(encoded)
    except Exception:
        raise ValueError("skill artifact manifest is invalid") from None
    if encoded != _canonical_json(manifest):
        raise ValueError("skill artifact manifest is not canonical JSON")
    if manifest.source != expected:
        raise ValueError("skill artifact source metadata does not match the workflow run")
    payload = root / _PAYLOAD_NAME
    actual, actual_directories = _artifact_state(payload)
    if actual != manifest.files:
        _raise_inventory_mismatch(actual, manifest.files)
    expected_directories = _expected_directories(manifest.files)
    if actual_directories != expected_directories:
        raise ValueError("skill artifact directory inventory is not closed")
    identities: list[SnapshotIdentity] = []
    for root_value in ALLOWED_SKILL_ROOTS:
        snapshot = payload / root_value
        parsed = validate_snapshot(snapshot)
        identities.append(
            SnapshotIdentity(
                name=PurePosixPath(root_value).name,
                snapshot_sha256=parsed.snapshot_sha256,
                manifest_sha256=_sha256_file(snapshot / "geas-skill.json"),
            )
        )
    if tuple(identities) != manifest.snapshots:
        raise ValueError("skill artifact snapshot identities do not match their bytes")
    return manifest


def evaluate_workflow_run(event: Mapping[str, object]) -> WorkflowRunDecision:
    """Authorize only one successful same-repository PR workflow run."""
    repository = _child_mapping(event, "repository")
    run = _child_mapping(event, "workflow_run")
    if repository.get("full_name") != REPOSITORY or str(repository.get("id")) != REPOSITORY_ID:
        raise ValueError("workflow run repository identity is invalid")
    if run.get("name") != REGENERATION_WORKFLOW:
        raise ValueError("originating workflow name is invalid")
    if run.get("path") != REGENERATION_WORKFLOW_PATH:
        raise ValueError("originating workflow path is invalid")
    if run.get("event") != "pull_request":
        raise ValueError("originating workflow event is invalid")
    pull_requests = run.get("pull_requests")
    if not isinstance(pull_requests, list) or len(pull_requests) != 1:
        raise ValueError("workflow run must identify exactly one pull request")
    pull_request = _mapping(pull_requests[0], "pull request")
    head_repository = _child_mapping(run, "head_repository").get("full_name")
    try:
        source = ArtifactSource(
            run_id=run.get("id"),
            pull_request=pull_request.get("number"),
            head_repository=head_repository,
            head_ref=run.get("head_branch"),
            head_sha=run.get("head_sha"),
        )
    except Exception:
        raise ValueError("workflow run source metadata is invalid") from None
    if run.get("conclusion") != "success":
        return WorkflowRunDecision(
            source=source,
            writeback=False,
            reason="source-run-failed",
        )
    if source.head_repository != REPOSITORY:
        return WorkflowRunDecision(
            source=source,
            writeback=False,
            reason="fork-pull-request",
        )
    return WorkflowRunDecision(
        source=source,
        writeback=True,
        reason="same-repository-success",
    )


def verify_pull_request(
    value: Mapping[str, object],
    *,
    source: ArtifactSource,
) -> PullRequestVerification:
    """Re-bind current PR state to the exact untrusted source-run identity."""
    base = _child_mapping(value, "base")
    base_repo = _child_mapping(base, "repo")
    head = _child_mapping(value, "head")
    head_repo = _child_mapping(head, "repo")
    if value.get("state") != "open":
        raise ValueError("pull request is not open")
    if value.get("number") != source.pull_request:
        raise ValueError("pull request number changed")
    if base_repo.get("full_name") != REPOSITORY:
        raise ValueError("pull request base repository changed")
    if head_repo.get("full_name") != source.head_repository:
        raise ValueError("pull request head repository changed")
    if bool(head_repo.get("fork")) or source.head_repository != REPOSITORY:
        raise ValueError("pull request is not same-repository")
    if head.get("ref") != source.head_ref:
        raise ValueError("pull request head branch changed")
    if head.get("sha") != source.head_sha:
        raise ValueError("pull request head SHA changed")
    return PullRequestVerification(
        number=source.pull_request,
        head_repository=source.head_repository,
        head_ref=source.head_ref,
        head_sha=source.head_sha,
    )


def effective_source_commit(repository: Path, head_sha: str) -> str:
    """Skip generated-only commits so a synchronize write-back converges."""
    if not _GIT_ID.fullmatch(head_sha):
        raise ValueError("source head must be one full Git commit ID")
    root = repository.resolve()
    current = _git(root, "rev-parse", "--verify", f"{head_sha}^{{commit}}").stdout.strip()
    if current != head_sha:
        raise ValueError("source head did not resolve exactly")
    while True:
        parents = _git(root, "rev-list", "--parents", "-n", "1", current).stdout.split()
        if len(parents) != 2:
            return current
        parent = parents[1]
        changed = tuple(
            path
            for path in _git(
                root,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                parent,
                current,
            ).stdout.splitlines()
            if path
        )
        if not changed or not all(_path_is_allowed(path) for path in changed):
            return current
        current = parent


def apply_verified_writeback(
    artifact_root: Path,
    *,
    repository: Path,
    source: ArtifactSource,
    pull_request: Mapping[str, object],
) -> WritebackReceipt:
    """Replace, stage, commit, and lease-push only the two verified skill trees."""
    manifest = verify_skill_artifact(artifact_root, expected=source)
    verify_pull_request(pull_request, source=source)
    root = repository.resolve()
    worktree = _git(root, "rev-parse", "--show-toplevel").stdout.strip()
    if Path(worktree).resolve() != root:
        raise ValueError("write-back repository is not its Git worktree root")
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    if head != source.head_sha:
        raise ValueError("write-back worktree is not at the verified head SHA")
    for root_value in ALLOWED_SKILL_ROOTS:
        _assert_no_symlink_path(root, Path(root_value))
        target = root / root_value
        if target.exists():
            _assert_regular_tree(target, label="existing skill snapshot")
    status = _git(root, "status", "--porcelain", "--untracked-files=all").stdout
    if status:
        raise ValueError("write-back worktree has local changes")
    payload = artifact_root.resolve() / _PAYLOAD_NAME
    if _worktree_matches(root, payload, manifest.files):
        return WritebackReceipt(changed=False, pushed=False)
    for root_value in ALLOWED_SKILL_ROOTS:
        target = root / root_value
        if target.exists():
            shutil.rmtree(target)
        _copy_regular_tree(payload / root_value, target)
    _write_manifest_index(root, payload, manifest)
    _verify_staged_index(root, payload, manifest)
    staged = tuple(
        path
        for path in _git(
            root,
            "diff",
            "--cached",
            "--name-only",
            "-z",
        ).stdout.split("\x00")
        if path
    )
    if not staged or not all(_path_is_allowed(path) for path in staged):
        raise RuntimeError("write-back staged paths escaped the two managed skill roots")
    _git(
        root,
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "user.name=Geas Skill Sync",
        "-c",
        "user.email=geas-skill-sync@users.noreply.github.com",
        "commit",
        "-m",
        "ci: refresh generated skill snapshots",
    )
    commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    if not _git_tree_matches(root, commit, payload, manifest):
        raise RuntimeError("committed skill tree does not match the verified manifest")
    lease = f"refs/heads/{source.head_ref}:{source.head_sha}"
    pushed = _git(
        root,
        "-c",
        "core.hooksPath=/dev/null",
        "push",
        f"--force-with-lease={lease}",
        "origin",
        f"HEAD:refs/heads/{source.head_ref}",
        check=False,
    )
    if pushed.returncode != 0:
        raise RuntimeError("write-back push failed under the exact head-SHA lease")
    return WritebackReceipt(
        changed=True,
        pushed=True,
        staged_roots=ALLOWED_SKILL_ROOTS,
        commit=commit,
    )


def artifact_changed_against_commit(
    artifact_root: Path,
    *,
    repository: Path,
    source: ArtifactSource,
) -> bool:
    """Verify inert artifact and Git bytes before any write token is requested."""
    manifest = verify_skill_artifact(artifact_root, expected=source)
    root = repository.resolve()
    commit = _git(root, "rev-parse", "--verify", f"{source.head_sha}^{{commit}}").stdout.strip()
    if commit != source.head_sha:
        raise ValueError("comparison repository does not contain the exact source head")
    return not _git_tree_matches(
        root,
        commit,
        artifact_root.resolve() / _PAYLOAD_NAME,
        manifest,
    )


def validate_org_sts_policy(path: Path) -> OrgStsPolicy:
    """Validate the exact org-level Octo STS authorization contract."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("Octo STS policy is missing or unsafe")
    try:
        return OrgStsPolicy.model_validate(yaml.safe_load(path.read_text()))
    except Exception as error:
        raise ValueError(f"Octo STS policy is invalid: {error}") from error


def _snapshot_identity(snapshot: Path) -> SnapshotIdentity:
    manifest = validate_snapshot(snapshot)
    return SnapshotIdentity(
        name=manifest.skill.name,
        snapshot_sha256=manifest.snapshot_sha256,
        manifest_sha256=_sha256_file(snapshot / "geas-skill.json"),
    )


def _snapshot_state(root: Path) -> tuple[tuple[str, bytes, int], ...]:
    return tuple(
        (
            path.relative_to(root).as_posix(),
            path.read_bytes(),
            stat.S_IMODE(path.stat().st_mode),
        )
        for path in _regular_files(root, label="generated skill snapshots")
    )


def _installed_version() -> str:
    try:
        return version("geas")
    except PackageNotFoundError:
        return "0.1.0"


def _artifact_inventory(
    root: Path,
    *,
    hash_file: Callable[[Path], str] | None = None,
) -> tuple[ArtifactFile, ...]:
    return _artifact_state(root, hash_file=hash_file)[0]


def _artifact_state(
    root: Path,
    *,
    hash_file: Callable[[Path], str] | None = None,
) -> tuple[tuple[ArtifactFile, ...], set[str]]:
    paths, directories = _bounded_artifact_tree(root)
    hasher = hash_file or _sha256_file
    files: list[ArtifactFile] = []
    for path, size_bytes in paths:
        relative = path.relative_to(root).as_posix()
        _validate_payload_path(relative)
        files.append(
            ArtifactFile(
                path=relative,
                size_bytes=size_bytes,
                sha256=hasher(path),
            )
        )
    return (
        tuple(sorted(files, key=lambda item: item.path.encode("utf-8"))),
        directories,
    )


def _bounded_artifact_tree(root: Path) -> tuple[tuple[tuple[Path, int], ...], set[str]]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("skill artifact payload root is missing or a symbolic link")
    files: list[tuple[Path, int]] = []
    directories: set[str] = set()
    total_bytes = 0
    pending = [root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink():
                    raise ValueError("skill artifact payload contains a symbolic link")
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    relative = path.relative_to(root).as_posix()
                    directories.add(relative)
                    if len(directories) > _MAX_DIRECTORIES:
                        raise ValueError("skill artifact payload contains too many directories")
                    pending.append(path)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("skill artifact payload contains a non-regular entry")
                if len(files) >= _MAX_FILES:
                    raise ValueError("skill artifact payload contains too many files")
                if metadata.st_size > _MAX_FILE_BYTES:
                    raise ValueError("skill artifact payload file size exceeds the limit")
                total_bytes += metadata.st_size
                if total_bytes > _MAX_TOTAL_BYTES:
                    raise ValueError("skill artifact payload total size exceeds the limit")
                if stat.S_IMODE(metadata.st_mode) != 0o644:
                    raise ValueError("skill artifact file mode must be 100644")
                files.append((path, metadata.st_size))
    return (
        tuple(sorted(files, key=lambda item: item[0].relative_to(root).as_posix().encode())),
        directories,
    )


def _regular_files(root: Path, *, label: str) -> tuple[Path, ...]:
    _assert_regular_tree(root, label=label)
    result: list[Path] = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories.sort()
        filenames.sort()
        current_path = Path(current)
        for filename in filenames:
            path = current_path / filename
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"{label} must contain regular files only")
            result.append(path)
    return tuple(result)


def _assert_regular_tree(root: Path, *, label: str) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"{label} root is missing or a symbolic link")
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in (*directories, *filenames):
            path = current_path / name
            if path.is_symlink():
                raise ValueError(f"{label} contains a symbolic link")
            mode = path.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError(f"{label} contains a non-regular entry")


def _copy_regular_tree(source: Path, destination: Path) -> None:
    _assert_regular_tree(source, label="skill snapshot")
    destination.mkdir(parents=True)
    for path in _regular_files(source, label="skill snapshot"):
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        target.chmod(0o644)


def _expected_directories(files: Sequence[ArtifactFile]) -> set[str]:
    result: set[str] = set()
    for item in files:
        path = PurePosixPath(item.path).parent
        while path.as_posix() != ".":
            result.add(path.as_posix())
            path = path.parent
    return result


def _raise_inventory_mismatch(
    actual: Sequence[ArtifactFile],
    expected: Sequence[ArtifactFile],
) -> None:
    actual_by_path = {item.path: item for item in actual}
    expected_by_path = {item.path: item for item in expected}
    if set(actual_by_path) != set(expected_by_path):
        raise ValueError("skill artifact file inventory does not match manifest")
    for path in sorted(actual_by_path):
        observed = actual_by_path[path]
        wanted = expected_by_path[path]
        if observed.size_bytes != wanted.size_bytes:
            raise ValueError("skill artifact file size does not match manifest")
        if observed.sha256 != wanted.sha256:
            raise ValueError("skill artifact file hash does not match manifest")
        if observed.mode != wanted.mode:
            raise ValueError("skill artifact file mode does not match manifest")
    raise ValueError("skill artifact inventory does not match manifest")


def _worktree_matches(
    repository: Path,
    payload: Path,
    files: Sequence[ArtifactFile],
) -> bool:
    expected_paths = {item.path for item in files}
    actual_paths: set[str] = set()
    for root_value in ALLOWED_SKILL_ROOTS:
        root = repository / root_value
        if not root.is_dir():
            return False
        for path in _regular_files(root, label="existing skill snapshot"):
            actual_paths.add(path.relative_to(repository).as_posix())
    if actual_paths != expected_paths:
        return False
    return all(
        stat.S_IMODE((repository / item.path).stat().st_mode) == 0o644
        and (repository / item.path).read_bytes() == (payload / item.path).read_bytes()
        for item in files
    )


def _write_manifest_index(
    repository: Path,
    payload: Path,
    manifest: PullRequestSnapshotManifest,
) -> None:
    """Write exact verified blobs to the index without consulting Git attributes."""
    tracked = tuple(
        path
        for path in _git(
            repository,
            "ls-files",
            "-z",
            "--",
            *ALLOWED_SKILL_ROOTS,
        ).stdout.split("\x00")
        if path
    )
    for path in tracked:
        if not _path_is_allowed(path):
            raise RuntimeError("tracked skill path escaped the two managed roots")
        _git(repository, "update-index", "--force-remove", "--", path)
    for item in manifest.files:
        source = payload / item.path
        data = source.read_bytes()
        if (
            len(data) != item.size_bytes
            or hashlib.sha256(data).hexdigest() != item.sha256
            or stat.S_IMODE(source.stat().st_mode) != 0o644
        ):
            raise RuntimeError("verified artifact changed before index construction")
        blob = _git_hash_object(repository, data, write=True)
        _git(
            repository,
            "update-index",
            "--add",
            "--cacheinfo",
            f"{item.mode},{blob},{item.path}",
        )


def _verify_staged_index(
    repository: Path,
    payload: Path,
    manifest: PullRequestSnapshotManifest,
) -> None:
    """Require the complete stage-zero index to equal manifest bytes and modes."""
    actual: dict[str, tuple[str, str]] = {}
    entries = _git(
        repository,
        "ls-files",
        "--stage",
        "-z",
        "--",
        *ALLOWED_SKILL_ROOTS,
    ).stdout
    for entry in entries.split("\x00"):
        if not entry:
            continue
        metadata, separator, path = entry.partition("\t")
        if not separator or not _path_is_allowed(path):
            raise RuntimeError("staged index contains an unsafe skill entry")
        mode, object_id, stage = metadata.split()
        if stage != "0" or path in actual:
            raise RuntimeError("staged index contains a non-canonical skill entry")
        actual[path] = (mode, object_id)
    expected_paths = {item.path for item in manifest.files}
    if set(actual) != expected_paths:
        raise RuntimeError("staged index inventory does not match the verified manifest")
    for item in manifest.files:
        expected_blob = _git_hash_object(
            repository,
            (payload / item.path).read_bytes(),
            write=False,
        )
        if actual[item.path] != (item.mode, expected_blob):
            raise RuntimeError("staged index bytes or mode do not match the verified manifest")


def _git_hash_object(repository: Path, data: bytes, *, write: bool) -> str:
    arguments = ["hash-object"]
    if write:
        arguments.append("-w")
    arguments.append("--stdin")
    try:
        result = subprocess.run(
            ("git", *arguments),
            cwd=repository,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            input=data,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError("bounded Git object operation failed") from error
    object_id = result.stdout.decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", object_id):
        raise RuntimeError("Git object operation returned an invalid identity")
    return object_id


def _assert_no_symlink_path(root: Path, relative: Path) -> None:
    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise ValueError("write-back target path contains a symbolic link")


def _validate_payload_path(value: str) -> None:
    if not value or "\\" in value or any(ord(character) < 32 for character in value):
        raise ValueError("artifact path is not normalized")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("artifact path is not normalized")
    _root_for_path(value)


def _root_for_path(value: str) -> str:
    matches = tuple(
        root for root in ALLOWED_SKILL_ROOTS if value.startswith(f"{root}/")
    )
    if len(matches) != 1:
        raise ValueError("artifact path is outside the two allowed skill roots")
    return matches[0]


def _path_is_allowed(value: str) -> bool:
    return any(value == root or value.startswith(f"{root}/") for root in ALLOWED_SKILL_ROOTS)


def _canonical_json(model: StrictModel) -> bytes:
    return (
        json.dumps(
            model.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} metadata is invalid")
    return value


def _child_mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    return _mapping(value.get(key), key)


def _git(
    repository: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ("git", *arguments),
            cwd=repository,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            text=True,
            capture_output=True,
            check=check,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError("bounded Git operation failed") from error


def _load_json(path: Path, *, label: str) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise ValueError(f"{label} JSON is missing, unsafe, or too large")
    try:
        value = json.loads(path.read_bytes())
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(f"{label} JSON is invalid") from error
    return _mapping(value, label)


def _write_outputs(path: Path, values: Mapping[str, str | int | bool]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        for key, value in sorted(values.items()):
            if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
                raise ValueError("GitHub output key is invalid")
            encoded = str(value).lower() if isinstance(value, bool) else str(value)
            if "\n" in encoded or "\r" in encoded:
                raise ValueError("GitHub output value contains a newline")
            stream.write(f"{key}={encoded}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m research_agent.pr_skill_sync")
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--repository", type=Path, required=True)
    generate.add_argument("--artifact", type=Path, required=True)
    event = subparsers.add_parser("evaluate-event")
    event.add_argument("--event", type=Path, required=True)
    event.add_argument("--context", type=Path, required=True)
    event.add_argument("--github-output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--artifact", type=Path, required=True)
    verify.add_argument("--context", type=Path, required=True)
    verify.add_argument("--pull-request", type=Path, required=True)
    verify.add_argument("--github-output", type=Path, required=True)
    verify.add_argument("--repository", type=Path, required=True)
    writeback = subparsers.add_parser("writeback")
    writeback.add_argument("--artifact", type=Path, required=True)
    writeback.add_argument("--context", type=Path, required=True)
    writeback.add_argument("--pull-request", type=Path, required=True)
    writeback.add_argument("--repository", type=Path, required=True)
    policy = subparsers.add_parser("validate-policy")
    policy.add_argument("path", type=Path)
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    if args.command == "generate":
        try:
            source = ArtifactSource(
                repository=os.environ.get("GEAS_SOURCE_REPOSITORY"),
                repository_id=os.environ.get("GEAS_SOURCE_REPOSITORY_ID"),
                run_id=os.environ.get("GEAS_SOURCE_RUN_ID"),
                pull_request=os.environ.get("GEAS_PULL_REQUEST"),
                head_repository=os.environ.get("GEAS_HEAD_REPOSITORY"),
                head_ref=os.environ.get("GEAS_HEAD_REF"),
                head_sha=os.environ.get("GEAS_HEAD_SHA"),
            )
        except Exception:
            raise ValueError("PR skill generation metadata is invalid") from None
        manifest = generate_pull_request_artifact(
            args.repository,
            args.artifact,
            source=source,
        )
        print(_canonical_json(manifest).decode("utf-8"), end="")
        return
    if args.command == "evaluate-event":
        decision = evaluate_workflow_run(_load_json(args.event, label="workflow event"))
        args.context.write_bytes(_canonical_json(decision))
        _write_outputs(
            args.github_output,
            {
                "head_sha": decision.source.head_sha,
                "pull_request": decision.source.pull_request,
                "run_id": decision.source.run_id,
                "writeback": decision.writeback,
            },
        )
        return
    if args.command == "verify":
        decision = WorkflowRunDecision.model_validate_json(args.context.read_bytes())
        if not decision.writeback:
            raise ValueError("workflow run is not eligible for write-back")
        verify_pull_request(
            _load_json(args.pull_request, label="pull request"),
            source=decision.source,
        )
        changed = artifact_changed_against_commit(
            args.artifact,
            repository=args.repository,
            source=decision.source,
        )
        _write_outputs(args.github_output, {"changed": changed})
        return
    if args.command == "writeback":
        decision = WorkflowRunDecision.model_validate_json(args.context.read_bytes())
        if not decision.writeback:
            raise ValueError("workflow run is not eligible for write-back")
        receipt = apply_verified_writeback(
            args.artifact,
            repository=args.repository,
            source=decision.source,
            pull_request=_load_json(args.pull_request, label="pull request"),
        )
        print(_canonical_json(receipt).decode("utf-8"), end="")
        return
    if args.command == "validate-policy":
        print(_canonical_json(validate_org_sts_policy(args.path)).decode("utf-8"), end="")
        return
    raise AssertionError("unhandled PR skill sync command")


def _git_tree_matches(
    repository: Path,
    commit: str,
    payload: Path,
    manifest: PullRequestSnapshotManifest,
) -> bool:
    """Compare blobs without checking out or executing the untrusted PR tree."""
    _assert_git_tree_roots_safe(repository, commit)
    listed: dict[str, str] = {}
    for root_value in ALLOWED_SKILL_ROOTS:
        result = _git(
            repository,
            "ls-tree",
            "-r",
            "-z",
            commit,
            "--",
            root_value,
        ).stdout
        for entry in result.split("\x00"):
            if not entry:
                continue
            metadata, separator, path = entry.partition("\t")
            if not separator:
                raise ValueError("Git tree entry is malformed")
            mode, object_type, _object_id = metadata.split()
            if object_type != "blob" or mode == "120000":
                raise ValueError("pull-request skill tree contains a symbolic link")
            if mode not in {"100644", "100755"} or not _path_is_allowed(path):
                raise ValueError("pull-request skill tree contains an unsafe entry")
            listed[path] = mode
    expected = {item.path for item in manifest.files}
    if set(listed) != expected:
        return False
    for item in manifest.files:
        if listed[item.path] != item.mode:
            return False
        blob = _git_bytes(repository, "show", f"{commit}:{item.path}")
        if blob != (payload / item.path).read_bytes():
            return False
    return True


def _assert_git_tree_roots_safe(repository: Path, commit: str) -> None:
    checked: set[str] = set()
    for root_value in ALLOWED_SKILL_ROOTS:
        parts = PurePosixPath(root_value).parts
        for index in range(1, len(parts) + 1):
            prefix = PurePosixPath(*parts[:index]).as_posix()
            if prefix in checked:
                continue
            checked.add(prefix)
            result = _git(repository, "ls-tree", "-z", commit, "--", prefix).stdout
            for entry in result.split("\x00"):
                if not entry:
                    continue
                metadata, separator, path = entry.partition("\t")
                if not separator or path != prefix:
                    raise ValueError("pull-request skill tree entry is malformed")
                mode, object_type, _object_id = metadata.split()
                if mode == "120000":
                    raise ValueError("pull-request skill tree contains a symbolic link")
                if index < len(parts) and object_type != "tree":
                    raise ValueError("pull-request skill tree ancestor is not a directory")


def _git_bytes(repository: Path, *arguments: str) -> bytes:
    try:
        return subprocess.run(
            ("git", *arguments),
            cwd=repository,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            capture_output=True,
            check=True,
        ).stdout
    except subprocess.CalledProcessError as error:
        raise RuntimeError("bounded Git blob read failed") from error


if __name__ == "__main__":
    main()
