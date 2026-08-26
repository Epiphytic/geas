"""End-to-end contracts for portable Agent Skill lifecycle operations."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

import research_agent.cli as cli
from research_agent.agent_skills import validate_snapshot
from research_agent.bundles import KnowledgeBundleImporter
from research_agent.geas_update import GeasUpdateReceipt
from research_agent.library import SourceLibraryManifest
from research_agent.ontology_artifacts import OntologyArtifact, OntologyArtifactManager
from research_agent.ontology_build import OntologyBuildConfig
from research_agent.ontology_sync import OntologyRepositoryManager
from research_agent.projection import SQLiteKnowledgeProjection
from research_agent.store import ImmutableStore
from research_agent.truth import TruthManager, TruthPolicy
from research_agent.user_config import GeasProfile, GeasUserConfig, OntologyGitConfig

TRUSTED_URL = "https://example.test/lifecycle-ontology.git"
GEAS_OLD_COMMIT = "a" * 40
GEAS_NEW_COMMIT = "b" * 40
INSTANT = datetime(2026, 8, 26, 12, tzinfo=UTC)
FULL_DOCUMENT_SENTINEL = "FULL-ACQUIRED-DOCUMENT-MUST-NOT-BE-EXPORTED"


class MemoryArtifactStore:
    """Offline release-store substitute retaining the real artifact bytes."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir()
        self.values: dict[tuple[str, str], Path] = {}

    def ensure(self, artifact: OntologyArtifact, source: Path) -> bool:
        target = self.root / artifact.asset_name
        shutil.copyfile(source, target)
        self.values[(artifact.release_tag, artifact.asset_name)] = target
        return True

    def available(self, artifact: OntologyArtifact) -> bool:
        return (artifact.release_tag, artifact.asset_name) in self.values

    def download(self, artifact: OntologyArtifact, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.values[(artifact.release_tag, artifact.asset_name)], destination)


class FakeGeasUpdater:
    """Trusted local update receipt; no installer or network process is started."""

    def update_and_reexec(
        self, _argv: tuple[str, ...], *, continuation: str | None
    ) -> GeasUpdateReceipt:
        assert continuation == "lifecycle"
        return GeasUpdateReceipt(
            installer="git-development",
            directory=Path("/trusted/geas"),
            executable=Path("/trusted/geas/bin/geas"),
            old_commit=GEAS_OLD_COMMIT,
            new_commit=GEAS_NEW_COMMIT,
            old_version="0.1.0",
            new_version="0.1.0",
            reinstalled=True,
            reexec_depth=1,
        )


def _git(cwd: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Geas Lifecycle Test",
            "GIT_AUTHOR_EMAIL": "geas-lifecycle@example.invalid",
            "GIT_COMMITTER_NAME": "Geas Lifecycle Test",
            "GIT_COMMITTER_EMAIL": "geas-lifecycle@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _run(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> None:
    monkeypatch.setattr(sys, "argv", ["geas", *arguments])
    cli.main()


def _snapshot_bytes(snapshot: Path) -> dict[str, bytes]:
    return {
        path.relative_to(snapshot).as_posix(): path.read_bytes()
        for path in sorted(snapshot.rglob("*"))
        if path.is_file()
    }


def _portable_digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _projection(tmp_path: Path) -> Path:
    root = tmp_path / "projection-data"
    store = ImmutableStore(root)
    KnowledgeBundleImporter(store=store).import_bundle(
        Path("ontology/open-source-research-agents/bundle.yaml"),
        imported_by="operator:lifecycle-test",
    )
    # This models acquired material that must stay outside portable snapshots.
    acquired = root / "blobs" / "sha256" / hashlib.sha256(
        FULL_DOCUMENT_SENTINEL.encode("utf-8")
    ).hexdigest()
    acquired.parent.mkdir(parents=True, exist_ok=True)
    acquired.write_text(FULL_DOCUMENT_SENTINEL)
    truth = TruthManager(
        workspace_root=Path("."),
        store_root=root,
        policy=TruthPolicy.from_yaml(Path("config/truth-policy.yaml")),
        clock=lambda: INSTANT,
    )
    snapshot = truth.capture(created_by="operator:lifecycle-test")
    database = tmp_path / "knowledge.sqlite"
    SQLiteKnowledgeProjection(store=store, workspace_root=Path(".")).build(
        database,
        snapshot=snapshot,
        truth_manager=truth,
    )
    return database


def _trusted_ontology(
    tmp_path: Path, database: Path, artifacts: MemoryArtifactStore
) -> tuple[Path, Path, str]:
    remote = tmp_path / "remote.git"
    _git(remote.parent, "init", "--bare", "--initial-branch=main", str(remote))
    upstream = tmp_path / "upstream"
    _git(tmp_path, "init", "-b", "main", str(upstream))
    _git(upstream, "remote", "add", "origin", str(remote))
    ontology = upstream / "research-agents"
    ontology.mkdir()
    (ontology / "build.yaml").write_text(
        OntologyBuildConfig(
            version=1,
            topic="Open-source research agents",
            topic_concept_id="concept:open-source-research-agents",
            provider="deepseek_local",
            output_directory=Path("data/generated"),
        ).explicit_yaml()
    )
    (ontology / "library.yaml").write_text(
        SourceLibraryManifest(
            version=1,
            id="library:lifecycle",
            title="Lifecycle projection fixture",
            description="An offline, verified projection fixture.",
            include_all_parsed_sources=True,
        ).explicit_yaml()
    )
    OntologyArtifactManager(ontology).publish(
        store=artifacts,
        published_by="operator:lifecycle-test",
        storage_rights_basis="offline test fixture",
        knowledge_projection=database,
    )
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "initial verified ontology")
    _git(upstream, "push", "-u", "origin", "main")
    return remote, upstream, _git(upstream, "rev-parse", "HEAD")


def _configure_trusted_profile(config: Path) -> None:
    config.write_text(
        GeasUserConfig(
            profiles={
                "default": GeasProfile(
                    ontology_directory=Path("ontologies"),
                    ontology_git=OntologyGitConfig(url=TRUSTED_URL, branch="main"),
                    secret_sources=(),
                )
            }
        ).explicit_yaml()
    )


def test_skill_lifecycle_is_portable_repeatable_and_reviewable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Catches lifecycle paths that skip provenance, mutate snapshots, or export source blobs."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    config = tmp_path / "user-config" / "config.yaml"

    # User setup installs the generic skill before an ontology profile exists.
    _run(monkeypatch, "--geas-config", str(config), "config-init")
    setup = json.loads(capsys.readouterr().out)
    generic = Path(setup["config_root"]) / "skills" / "geas"
    assert generic.is_dir()
    assert validate_snapshot(generic).skill.name == "geas"

    database = _projection(tmp_path)
    artifacts = MemoryArtifactStore(tmp_path / "artifact-store")
    remote, upstream, old_commit = _trusted_ontology(tmp_path, database, artifacts)
    _configure_trusted_profile(config)

    original_execute = OntologyRepositoryManager._execute

    def offline_git(
        command: tuple[str, ...], *, cwd: Path, check: bool
    ) -> subprocess.CompletedProcess[str]:
        if command[:4] == ("git", "remote", "get-url", "origin"):
            return subprocess.CompletedProcess(command, 0, TRUSTED_URL + "\n", "")
        return original_execute(
            ("git", "-c", f"url.{remote}.insteadOf={TRUSTED_URL}", *command[1:]),
            cwd=cwd,
            check=check,
        )

    monkeypatch.setattr(OntologyRepositoryManager, "_execute", staticmethod(offline_git))
    monkeypatch.setattr(cli, "GitHubReleaseArtifactStore", lambda *_args, **_kwargs: artifacts)
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda executable: "/controlled/codex" if executable == "codex" else None,
    )

    export_args = (
        "--geas-config",
        str(config),
        "skill-export",
        "research-agents",
        "--link",
    )
    _run(monkeypatch, *export_args)
    first = json.loads(capsys.readouterr().out)
    snapshot = Path(first["path"])
    first_files = _snapshot_bytes(snapshot)
    assert first["unchanged"] is False
    assert first["ontology_commit"] == old_commit
    assert (home / ".agents" / "skills" / "research-agents").resolve() == snapshot
    assert validate_snapshot(snapshot).ontology.commit == old_commit

    # The same trusted Git/artifact/projection inputs must reproduce every portable byte.
    _run(monkeypatch, *export_args)
    second = json.loads(capsys.readouterr().out)
    assert second["unchanged"] is True
    assert second["snapshot_sha256"] == first["snapshot_sha256"]
    assert _snapshot_bytes(snapshot) == first_files
    assert _portable_digest(_snapshot_bytes(snapshot)) == _portable_digest(first_files)

    text = b"".join(first_files.values()).decode("utf-8")
    assert TRUSTED_URL in text
    assert old_commit in text
    assert "Original source:" in text
    assert FULL_DOCUMENT_SENTINEL not in text
    readable = subprocess.run(
        (
            sys.executable,
            "-c",
            "from pathlib import Path; import json, sys; root = Path(sys.argv[1]); "
            "assert (root / 'SKILL.md').is_file(); "
            "assert json.loads((root / 'geas-skill.json').read_text())['ontology']['repository_url'].startswith('https://')",
            str(snapshot),
        ),
        env={**os.environ, "PATH": ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert readable.returncode == 0, readable.stderr

    # A trusted fast-forward refreshes provenance before atomically replacing the snapshot.
    (upstream / "trusted-update.yaml").write_text("revision: two\n")
    _git(upstream, "add", "trusted-update.yaml")
    _git(upstream, "commit", "-m", "trusted update")
    _git(upstream, "push", "origin", "main")
    new_commit = _git(upstream, "rev-parse", "HEAD")
    monkeypatch.setattr(cli, "GeasUpdater", FakeGeasUpdater)
    _run(
        monkeypatch,
        "--geas-config",
        str(config),
        "skill-update",
        str(snapshot),
        "--geas-update-continuation",
        "lifecycle",
    )
    updated = json.loads(capsys.readouterr().out)
    assert updated["ontology_update"] == {"old_commit": old_commit, "new_commit": new_commit}
    assert validate_snapshot(snapshot).ontology.commit == new_commit

    _run(monkeypatch, "--geas-config", str(config), "skill-unlink", str(snapshot))
    unlinked = json.loads(capsys.readouterr().out)
    assert unlinked["removed_snapshot"] is False
    assert snapshot.is_dir()
    assert not (home / ".agents" / "skills" / "research-agents").exists()

    _run(monkeypatch, *export_args, "--force")
    relinked = json.loads(capsys.readouterr().out)
    assert Path(relinked["path"]) == snapshot
    assert (home / ".agents" / "skills" / "research-agents").resolve() == snapshot

    _run(monkeypatch, "--geas-config", str(config), "skill-remove", str(snapshot))
    removed = json.loads(capsys.readouterr().out)
    assert removed["removed_snapshot"] is True
    assert not snapshot.exists()
    _run(monkeypatch, *export_args)
    reinstalled = json.loads(capsys.readouterr().out)
    assert Path(reinstalled["path"]).is_dir()

    repository = tmp_path / "consumer-repository"
    _git(tmp_path, "init", "-b", "main", str(repository))
    _git(repository, "config", "user.name", "Geas Lifecycle Test")
    _git(repository, "config", "user.email", "geas-lifecycle@example.invalid")
    _git(repository, "commit", "--allow-empty", "-m", "initial")
    repository_args = (*export_args, "--repo", str(repository))
    _run(monkeypatch, *repository_args)
    repository_first = json.loads(capsys.readouterr().out)
    repository_snapshot = Path(repository_first["path"])
    repository_files = _snapshot_bytes(repository_snapshot)
    _run(monkeypatch, *repository_args)
    repository_second = json.loads(capsys.readouterr().out)
    assert repository_second["unchanged"] is True
    assert _snapshot_bytes(repository_snapshot) == repository_files
    assert _portable_digest(_snapshot_bytes(repository_snapshot)) == _portable_digest(
        repository_files
    )
    assert "?? .agents/" in _git(repository, "status", "--short")

    _run(monkeypatch, "--geas-config", str(config), "skill-remove", str(repository_snapshot))
    assert json.loads(capsys.readouterr().out)["removed_snapshot"] is True
    assert "?? .agents/" not in _git(repository, "status", "--short")


def test_maintained_demo_exports_a_verified_repeatable_skill() -> None:
    """Catches demo regressions that omit the portable skill or fail to prove its determinism."""
    demo_root = Path(tempfile.mkdtemp(prefix="geas-demo-test.", dir="/tmp"))
    try:
        assert str(demo_root).startswith("/tmp/")
        completed = subprocess.run(
            ("./ontology/open-source-research-agents/demo.sh", str(demo_root)),
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr

        first = json.loads((demo_root / "skill-export-first.json").read_text())
        second = json.loads((demo_root / "skill-export-second.json").read_text())
        snapshot = Path(second["path"])
        hashes = json.loads((demo_root / "skill-export-files.json").read_text())
        assert first["unchanged"] is False
        assert second["unchanged"] is True
        assert first["snapshot_sha256"] == second["snapshot_sha256"]
        assert hashes == {
            path.relative_to(snapshot).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(snapshot.rglob("*"))
            if path.is_file()
        }
        assert validate_snapshot(snapshot).skill.name == "open-source-research-agents"
    finally:
        shutil.rmtree(demo_root)
