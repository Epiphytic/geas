"""Deterministic, privilege-separated pull-request skill synchronization."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from research_agent.agent_skills import (
    GeasIdentity,
    OntologyIdentity,
    ProjectionIdentity,
    SkillFile,
    SkillIdentity,
    SkillManifest,
    canonical_manifest_bytes,
    snapshot_digest,
)
from research_agent.pr_skill_sync import (
    ALLOWED_SKILL_ROOTS,
    ArtifactSource,
    PullRequestSnapshotManifest,
    apply_verified_writeback,
    artifact_changed_against_commit,
    build_skill_artifact,
    effective_source_commit,
    evaluate_workflow_run,
    generate_repository_skill_snapshots,
    validate_org_sts_policy,
    verify_pull_request,
    verify_skill_artifact,
)

REPOSITORY = "Epiphytic/geas"
REPOSITORY_ID = "1320458746"
HEAD_SHA = "1" * 40
RUN_ID = 731
PR_NUMBER = 42
ACTION_PINS = {
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
    "astral-sh/setup-uv": "d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
    "octo-sts/action": "f603d3be9d8dd9871a265776e625a27b00effe05",
}


def _git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *arguments),
        cwd=repository,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Geas Skill Sync Test",
            "GIT_AUTHOR_EMAIL": "geas-skill-sync@example.invalid",
            "GIT_COMMITTER_NAME": "Geas Skill Sync Test",
            "GIT_COMMITTER_EMAIL": "geas-skill-sync@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        },
        text=True,
        capture_output=True,
        check=check,
    )


def _source(*, head_repository: str = REPOSITORY, head_sha: str = HEAD_SHA) -> ArtifactSource:
    return ArtifactSource(
        repository=REPOSITORY,
        repository_id=REPOSITORY_ID,
        workflow="PR Skill Regeneration",
        workflow_path=".github/workflows/pr-skill-regeneration.yml",
        run_id=RUN_ID,
        pull_request=PR_NUMBER,
        head_repository=head_repository,
        head_ref="feature/skill-sync",
        head_sha=head_sha,
    )


def _workflow_event(
    *,
    conclusion: str = "success",
    head_repository: str = REPOSITORY,
    head_sha: str = HEAD_SHA,
) -> dict[str, object]:
    return {
        "repository": {"id": int(REPOSITORY_ID), "full_name": REPOSITORY},
        "workflow_run": {
            "id": RUN_ID,
            "name": "PR Skill Regeneration",
            "path": ".github/workflows/pr-skill-regeneration.yml",
            "event": "pull_request",
            "conclusion": conclusion,
            "head_branch": "feature/skill-sync",
            "head_sha": head_sha,
            "head_repository": {"full_name": head_repository},
            "pull_requests": [{"number": PR_NUMBER}],
        },
    }


def _pull_request(
    *,
    head_repository: str = REPOSITORY,
    head_sha: str = HEAD_SHA,
) -> dict[str, object]:
    return {
        "number": PR_NUMBER,
        "state": "open",
        "base": {"repo": {"full_name": REPOSITORY}},
        "head": {
            "ref": "feature/skill-sync",
            "sha": head_sha,
            "repo": {"full_name": head_repository, "fork": head_repository != REPOSITORY},
        },
    }


def _snapshot(root: Path, name: str, body: bytes) -> None:
    directory = root / ".agents" / "skills" / name
    directory.mkdir(parents=True)
    skill = b"---\nname: " + name.encode() + b"\n---\n\n" + body
    inventory = (SkillFile(path="SKILL.md", sha256=hashlib.sha256(skill).hexdigest()),)
    manifest = SkillManifest(
        format_version=1,
        skill=SkillIdentity(name=name),
        ontology=OntologyIdentity(
            name=name,
            repository_url="https://github.com/Epiphytic/geas.git",
            branch="main",
            commit="a" * 40,
        ),
        geas=GeasIdentity(
            project_url="https://github.com/Epiphytic/geas",
            version="0.1.0",
            commit="a" * 40,
        ),
        projection=ProjectionIdentity(
            snapshot_id=f"fixture:{name}",
            topic_concept_id=f"fixture:{name}",
        ),
        files=inventory,
        snapshot_sha256=snapshot_digest(inventory),
    )
    (directory / "SKILL.md").write_bytes(skill)
    (directory / "geas-skill.json").write_bytes(canonical_manifest_bytes(manifest))


def _snapshots(root: Path) -> Path:
    _snapshot(root, "geas", b"Generic Geas operations.\n")
    _snapshot(
        root,
        "open-source-research-agents",
        b"Accepted research-agent knowledge.\n",
    )
    return root


def _artifact(tmp_path: Path) -> tuple[Path, PullRequestSnapshotManifest]:
    snapshots = _snapshots(tmp_path / "snapshots")
    destination = tmp_path / "artifact"
    manifest = build_skill_artifact(snapshots, destination, source=_source())
    return destination, manifest


def test_artifact_generation_is_canonical_and_independently_repeatable(tmp_path: Path) -> None:
    """Catches timestamps, traversal-order dependence, or incomplete snapshot identities."""
    first_source = _snapshots(tmp_path / "first-source")
    second_source = _snapshots(tmp_path / "second-source")
    first = tmp_path / "first-artifact"
    second = tmp_path / "second-artifact"

    first_manifest = build_skill_artifact(first_source, first, source=_source())
    second_manifest = build_skill_artifact(second_source, second, source=_source())

    assert first_manifest == second_manifest
    assert (first / "manifest.json").read_bytes() == (second / "manifest.json").read_bytes()
    assert first_manifest.snapshot_names == ("geas", "open-source-research-agents")
    assert tuple(item.path for item in first_manifest.files) == tuple(
        sorted(item.path for item in first_manifest.files)
    )
    for item in first_manifest.files:
        assert (first / "payload" / item.path).read_bytes() == (
            second / "payload" / item.path
        ).read_bytes()


def test_workflow_run_decision_separates_success_same_repo_fork_and_failure() -> None:
    """Catches granting write authority to forks or failed untrusted runs."""
    same = evaluate_workflow_run(_workflow_event())
    fork = evaluate_workflow_run(
        _workflow_event(head_repository="contributor/geas")
    )
    failed = evaluate_workflow_run(_workflow_event(conclusion="failure"))

    assert same.writeback is True
    assert same.reason == "same-repository-success"
    assert fork.writeback is False
    assert fork.reason == "fork-pull-request"
    assert failed.writeback is False
    assert failed.reason == "source-run-failed"


def test_pull_request_must_remain_open_and_match_the_exact_head() -> None:
    """Catches stale metadata authorizing an advanced or retargeted PR."""
    source = _source()
    assert verify_pull_request(_pull_request(), source=source).head_sha == HEAD_SHA

    advanced = _pull_request(head_sha="2" * 40)
    with pytest.raises(ValueError, match="head SHA"):
        verify_pull_request(advanced, source=source)

    closed = _pull_request()
    closed["state"] = "closed"
    with pytest.raises(ValueError, match="open"):
        verify_pull_request(closed, source=source)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("absolute", "invalid"),
        ("traversal", "invalid"),
        ("duplicate", "invalid"),
        ("extra-field", "invalid"),
        ("extra-file", "inventory"),
        ("size", "size"),
        ("hash", "hash"),
        ("mode", "mode"),
    ],
)
def test_artifact_verification_fails_closed_for_manifest_and_inventory_tampering(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    """Catches trusting attacker-controlled manifest paths, metadata, or undeclared bytes."""
    artifact, manifest = _artifact(tmp_path)
    manifest_path = artifact / "manifest.json"
    payload = json.loads(manifest_path.read_text())
    if mutation == "absolute":
        payload["files"][0]["path"] = "/tmp/escape"
    elif mutation == "traversal":
        payload["files"][0]["path"] = ".agents/skills/geas/../escape"
    elif mutation == "duplicate":
        payload["files"].append(dict(payload["files"][0]))
    elif mutation == "extra-field":
        payload["authority"] = "write"
    elif mutation == "extra-file":
        extra = artifact / "payload" / ".agents" / "skills" / "geas" / "extra.md"
        extra.write_text("undeclared\n")
    elif mutation == "size":
        payload["files"][0]["size_bytes"] += 1
    elif mutation == "hash":
        payload["files"][0]["sha256"] = "f" * 64
    elif mutation == "mode":
        path = artifact / "payload" / manifest.files[0].path
        path.chmod(0o600)
    if mutation not in {"extra-file", "mode"}:
        manifest_path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        )

    with pytest.raises(ValueError, match=message):
        verify_skill_artifact(artifact, expected=_source())


def test_artifact_verification_rejects_symlinks_without_reading_their_target(
    tmp_path: Path,
) -> None:
    """Catches artifact traversal through a symlink supplied by untrusted PR code."""
    artifact, manifest = _artifact(tmp_path)
    target = artifact / "payload" / manifest.files[0].path
    outside = tmp_path / "outside-secret"
    outside.write_text("must not be read\n")
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(ValueError, match="symbolic link"):
        verify_skill_artifact(artifact, expected=_source())


def test_manifest_rejects_noncanonical_json_and_source_mismatch(tmp_path: Path) -> None:
    """Catches malleable encodings and replay against another run or head."""
    artifact, _manifest = _artifact(tmp_path)
    path = artifact / "manifest.json"
    path.write_text(json.dumps(json.loads(path.read_text()), indent=2) + "\n")
    with pytest.raises(ValueError, match="canonical"):
        verify_skill_artifact(artifact, expected=_source())

    artifact, _manifest = _artifact(tmp_path / "replay")
    with pytest.raises(ValueError, match="source metadata"):
        verify_skill_artifact(
            artifact,
            expected=_source(head_sha="2" * 40),
        )


def test_untrusted_artifact_values_are_not_reflected_in_errors(tmp_path: Path) -> None:
    """Catches attacker-controlled artifact values leaking credentials into workflow logs."""
    artifact, _manifest = _artifact(tmp_path)
    manifest_path = artifact / "manifest.json"
    payload = json.loads(manifest_path.read_text())
    secret = "ghp_untrusted_artifact_secret_value"
    payload["unexpected_token"] = secret
    manifest_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    )

    with pytest.raises(ValueError) as failure:
        verify_skill_artifact(artifact, expected=_source())
    assert secret not in str(failure.value)


def test_effective_source_commit_skips_only_generated_only_commits(tmp_path: Path) -> None:
    """Catches infinite synchronize loops or ignoring a substantive source change."""
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    (repository / "source.py").write_text("version = 1\n")
    _git(repository, "add", "source.py")
    _git(repository, "commit", "-m", "source")
    source = _git(repository, "rev-parse", "HEAD").stdout.strip()

    generated = repository / ".agents" / "skills" / "geas"
    generated.mkdir(parents=True)
    (generated / "SKILL.md").write_text("generated\n")
    _git(repository, "add", ".agents/skills/geas")
    _git(repository, "commit", "-m", "ci: refresh generated skill snapshots")
    writeback = _git(repository, "rev-parse", "HEAD").stdout.strip()

    assert effective_source_commit(repository, writeback) == source

    (repository / "source.py").write_text("version = 2\n")
    _git(repository, "add", "source.py")
    _git(repository, "commit", "-m", "substantive")
    substantive = _git(repository, "rev-parse", "HEAD").stdout.strip()
    assert effective_source_commit(repository, substantive) == substantive


def _bare_remote(tmp_path: Path) -> tuple[Path, Path, str]:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    worktree = tmp_path / "worktree"
    _git(tmp_path, "clone", str(remote), str(worktree))
    _git(worktree, "checkout", "-b", "feature/skill-sync")
    (worktree / "README.md").write_text("fixture\n")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "fixture")
    _git(worktree, "push", "-u", "origin", "feature/skill-sync")
    return remote, worktree, _git(worktree, "rev-parse", "HEAD").stdout.strip()


def test_writeback_stages_and_pushes_only_the_two_exact_skill_roots(tmp_path: Path) -> None:
    """Catches broad staging, accidental source mutation, or unconstrained pushes."""
    _remote, worktree, head = _bare_remote(tmp_path)
    snapshots = _snapshots(tmp_path / "snapshots")
    artifact = tmp_path / "artifact"
    source = _source(head_sha=head)
    build_skill_artifact(snapshots, artifact, source=source)
    before = (worktree / "README.md").read_bytes()

    receipt = apply_verified_writeback(
        artifact,
        repository=worktree,
        source=source,
        pull_request=_pull_request(head_sha=head),
    )

    assert receipt.changed is True
    assert receipt.pushed is True
    assert receipt.staged_roots == ALLOWED_SKILL_ROOTS
    assert (worktree / "README.md").read_bytes() == before
    changed = tuple(
        _git(worktree, "diff", "--name-only", f"{head}..HEAD").stdout.splitlines()
    )
    assert changed
    assert all(
        any(path == root or path.startswith(f"{root}/") for root in ALLOWED_SKILL_ROOTS)
        for path in changed
    )


def test_writeback_unchanged_converges_without_a_commit_or_push(tmp_path: Path) -> None:
    """Catches synchronize loops after the generated-only write-back commit."""
    _remote, worktree, head = _bare_remote(tmp_path)
    snapshots = _snapshots(worktree)
    _git(worktree, "add", *ALLOWED_SKILL_ROOTS)
    _git(worktree, "commit", "-m", "ci: refresh generated skill snapshots")
    _git(worktree, "push")
    current = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    artifact = tmp_path / "artifact"
    source = _source(head_sha=current)
    build_skill_artifact(snapshots, artifact, source=source)

    receipt = apply_verified_writeback(
        artifact,
        repository=worktree,
        source=source,
        pull_request=_pull_request(head_sha=current),
    )

    assert receipt.changed is False
    assert receipt.pushed is False
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == current


def test_writeback_rejects_symlinked_existing_snapshot_and_push_lease_failure(
    tmp_path: Path,
) -> None:
    """Catches copying through PR symlinks and overwriting an advanced remote head."""
    remote, worktree, head = _bare_remote(tmp_path)
    snapshots = _snapshots(tmp_path / "snapshots")
    artifact = tmp_path / "artifact"
    source = _source(head_sha=head)
    build_skill_artifact(snapshots, artifact, source=source)

    unsafe = worktree / ".agents"
    unsafe.symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        apply_verified_writeback(
            artifact,
            repository=worktree,
            source=source,
            pull_request=_pull_request(head_sha=head),
        )
    unsafe.unlink()

    advancing = tmp_path / "advancing"
    _git(tmp_path, "clone", str(remote), str(advancing))
    _git(advancing, "checkout", "feature/skill-sync")
    (advancing / "advanced.txt").write_text("advanced\n")
    _git(advancing, "add", "advanced.txt")
    _git(advancing, "commit", "-m", "advanced")
    _git(advancing, "push")

    with pytest.raises(RuntimeError, match="lease"):
        apply_verified_writeback(
            artifact,
            repository=worktree,
            source=source,
            pull_request=_pull_request(head_sha=head),
        )


def test_pre_token_git_comparison_rejects_symlinks_and_detects_changed_bytes(
    tmp_path: Path,
) -> None:
    """Catches requesting a token before inert PR-tree safety and change gates pass."""
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _snapshots(repository)
    _git(repository, "add", *ALLOWED_SKILL_ROOTS)
    _git(repository, "commit", "-m", "snapshots")
    head = _git(repository, "rev-parse", "HEAD").stdout.strip()
    artifact = tmp_path / "artifact"
    source = _source(head_sha=head)
    build_skill_artifact(repository, artifact, source=source)

    assert artifact_changed_against_commit(artifact, repository=repository, source=source) is False

    changed_source = tmp_path / "changed-source"
    _snapshot(changed_source, "geas", b"Changed Geas operations.\n")
    _snapshot(
        changed_source,
        "open-source-research-agents",
        b"Accepted research-agent knowledge.\n",
    )
    changed_artifact = tmp_path / "changed-artifact"
    build_skill_artifact(changed_source, changed_artifact, source=source)
    assert (
        artifact_changed_against_commit(
            changed_artifact,
            repository=repository,
            source=source,
        )
        is True
    )

    symlink_repository = tmp_path / "symlink-repository"
    symlink_repository.mkdir()
    _git(symlink_repository, "init", "--initial-branch=main")
    (symlink_repository / ".agents").symlink_to("outside")
    _git(symlink_repository, "add", ".agents")
    _git(symlink_repository, "commit", "-m", "unsafe generated root")
    symlink_head = _git(symlink_repository, "rev-parse", "HEAD").stdout.strip()
    symlink_source = _source(head_sha=symlink_head)
    symlink_artifact = tmp_path / "symlink-artifact"
    build_skill_artifact(repository, symlink_artifact, source=symlink_source)
    with pytest.raises(ValueError, match="symbolic link"):
        artifact_changed_against_commit(
            symlink_artifact,
            repository=symlink_repository,
            source=symlink_source,
        )


def test_workflows_pin_actions_and_keep_token_exchange_after_all_gates() -> None:
    """Catches action-tag drift, early OIDC exchange, or privileged PR execution."""
    root = Path(__file__).resolve().parents[1]
    regeneration_path = root / ".github" / "workflows" / "pr-skill-regeneration.yml"
    writeback_path = root / ".github" / "workflows" / "pr-skill-writeback.yml"
    regeneration = yaml.load(regeneration_path.read_text(), Loader=yaml.BaseLoader)
    writeback = yaml.load(writeback_path.read_text(), Loader=yaml.BaseLoader)

    assert regeneration["permissions"] == {"contents": "read"}
    assert set(writeback["permissions"]) == {
        "actions",
        "contents",
        "id-token",
        "pull-requests",
    }
    assert writeback["permissions"]["contents"] == "read"
    assert writeback["permissions"]["id-token"] == "write"
    job_gate = writeback["jobs"]["writeback"]["if"]
    assert "head_repository.full_name == github.repository" in job_gate

    def uses(document: dict[str, object]) -> list[str]:
        jobs = document["jobs"]
        assert isinstance(jobs, dict)
        return [
            step["uses"]
            for job in jobs.values()
            for step in job["steps"]
            if "uses" in step
        ]

    all_uses = uses(regeneration) + uses(writeback)
    for value in all_uses:
        action, separator, sha = value.partition("@")
        assert separator == "@"
        assert len(sha) == 40 and all(character in "0123456789abcdef" for character in sha)
        assert ACTION_PINS[action] == sha

    regeneration_steps = regeneration["jobs"]["generate"]["steps"]
    checkout = next(
        step
        for step in regeneration_steps
        if step.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["with"]["persist-credentials"] == "false"
    assert "id-token" not in regeneration["permissions"]
    assert all("secrets." not in str(step) for step in regeneration_steps)

    steps = writeback["jobs"]["writeback"]["steps"]
    token_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("uses", "").startswith("octo-sts/action@")
    )
    verification_index = next(
        index for index, step in enumerate(steps) if step.get("id") == "verify"
    )
    assert token_index > verification_index
    assert steps[token_index]["if"] == "steps.verify.outputs.changed == 'true'"
    assert steps[token_index]["with"] == {
        "domain": "sts.epiphytic.org",
        "scope": "Epiphytic/.github",
        "identity": "geas-pr-skill-sync",
    }
    for document in (regeneration, writeback):
        for job in document["jobs"].values():
            for step in job["steps"]:
                assert "${{ github.event" not in step.get("run", "")


def test_org_policy_contract_is_exact_and_rejects_broader_permissions(tmp_path: Path) -> None:
    """Catches repository, workflow, event, subject, or permission broadening."""
    policy = tmp_path / "geas-pr-skill-sync.sts.yaml"
    policy.write_text(
        "issuer: https://token.actions.githubusercontent.com\n"
        "subject: repo:Epiphytic/geas:ref:refs/heads/main\n"
        "claim_patterns:\n"
        "  repository_id: '1320458746'\n"
        "  event_name: workflow_run\n"
        "  workflow_ref: 'Epiphytic/geas/\\.github/workflows/"
        "pr-skill-writeback\\.yml@refs/heads/main'\n"
        "permissions:\n"
        "  contents: write\n"
        "repositories:\n"
        "  - geas\n"
    )

    parsed = validate_org_sts_policy(policy)
    assert parsed.permissions == {"contents": "write"}
    assert parsed.repositories == ("geas",)

    raw = yaml.safe_load(policy.read_text())
    raw["permissions"]["actions"] = "write"
    policy.write_text(yaml.safe_dump(raw, sort_keys=False))
    with pytest.raises(ValueError, match="policy"):
        validate_org_sts_policy(policy)


def test_generated_artifact_files_are_regular_and_normalized_to_git_mode(tmp_path: Path) -> None:
    """Catches upload behavior preserving unsafe executable or platform-specific modes."""
    source = _snapshots(tmp_path / "source")
    for path in source.rglob("*"):
        if path.is_file():
            path.chmod(0o600)
    artifact = tmp_path / "artifact"
    manifest = build_skill_artifact(source, artifact, source=_source())

    for item in manifest.files:
        path = artifact / "payload" / item.path
        assert item.mode == "100644"
        assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_production_generation_uses_catalog_and_preseeded_verified_projection(
    tmp_path: Path,
) -> None:
    """Catches hand-authored snapshots or generation that downloads the pinned artifact."""
    source_repository = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "--quiet", "--no-hardlinks", str(source_repository), str(checkout))
    head = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    demo = tmp_path / "demo"
    subprocess.run(
        (
            str(checkout / "ontology" / "open-source-research-agents" / "demo.sh"),
            str(demo),
        ),
        cwd=checkout,
        env={
            **os.environ,
            "PATH": str(source_repository / ".venv" / "bin") + os.pathsep + os.environ["PATH"],
            "PYTHONPATH": str(source_repository / "src"),
            "UV_PROJECT_ENVIRONMENT": str(source_repository / ".venv"),
            "UV_NO_SYNC": "1",
        },
        text=True,
        capture_output=True,
        check=True,
    )
    source = _source(head_sha=head)
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_identities = generate_repository_skill_snapshots(
        checkout,
        first,
        source=source,
        projection=demo / "query.sqlite",
    )
    second_identities = generate_repository_skill_snapshots(
        checkout,
        second,
        source=source,
        projection=demo / "query.sqlite",
    )

    assert first_identities == second_identities
    first_files = {
        path.relative_to(first).as_posix(): path.read_bytes()
        for path in first.rglob("*")
        if path.is_file()
    }
    second_files = {
        path.relative_to(second).as_posix(): path.read_bytes()
        for path in second.rglob("*")
        if path.is_file()
    }
    assert first_files == second_files
    ontology = SkillManifest.model_validate_json(
        (
            first
            / ".agents"
            / "skills"
            / "open-source-research-agents"
            / "geas-skill.json"
        ).read_bytes()
    )
    assert ontology.ontology.catalog_path == "geas.yaml"
    assert ontology.ontology.ontology_path == "ontology/open-source-research-agents"
    assert ontology.ontology.subscription_name == "geas-pr-skill-sync"
    assert ontology.ontology.ontology_commit == effective_source_commit(checkout, head)
    assert ontology.artifact is not None
    assert ontology.artifact.content_sha256 == hashlib.sha256(
        (demo / "query.sqlite").read_bytes()
    ).hexdigest()
