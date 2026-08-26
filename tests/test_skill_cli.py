from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

import research_agent.cli as cli
from research_agent.agent_skills import export_skill, refresh_skill
from research_agent.bundles import KnowledgeBundleImporter
from research_agent.geas_update import GeasUpdateReceipt
from research_agent.library import SourceLibraryManifest
from research_agent.ontology_artifacts import OntologyArtifact, OntologyArtifactManager
from research_agent.ontology_build import OntologyBuildConfig
from research_agent.ontology_sync import OntologyRepositoryManager
from research_agent.projection import KnowledgeQueryEngine, SQLiteKnowledgeProjection, TopicView
from research_agent.render import render_ontology_skill
from research_agent.store import ImmutableStore
from research_agent.truth import TruthManager, TruthPolicy
from research_agent.user_config import GeasProfile, GeasUserConfig, OntologyGitConfig

ONTOLOGY_URL = "https://example.test/ontologies.git"
OLD_COMMIT = "a" * 40
NEW_COMMIT = "b" * 40
INSTANT = datetime(2026, 8, 26, 12, tzinfo=UTC)


def _topic(snapshot: str = "truth:old") -> TopicView:
    return TopicView(
        topic_concept_id="concept:test",
        descendant_concept_ids=("concept:test",),
        concepts=(
            {
                "id": "concept:test",
                "label": "Test",
                "description": "Accepted test knowledge.",
                "broader": "",
                "synonyms": "",
            },
        ),
        sources=(),
        claims=(),
        controversies=(),
        gaps=(),
        threats=(),
        references=(),
        projection_snapshot_id=snapshot,
    )


def _skill_files(*, commit: str, snapshot: str) -> dict[Path, bytes]:
    return render_ontology_skill(
        _topic(snapshot),
        skill_name="test-ontology",
        ontology_name="test-ontology",
        repository_url=ONTOLOGY_URL,
        branch="main",
        ontology_commit=commit,
        geas_version="0.1.0",
        geas_commit=None,
    )


def _snapshot_bytes(snapshot: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(snapshot): path.read_bytes()
        for path in snapshot.rglob("*")
        if path.is_file()
    }


def _config(tmp_path: Path, *, url: str = ONTOLOGY_URL) -> Path:
    config = tmp_path / "config" / "config.yaml"
    config.parent.mkdir()
    config.write_text(
        GeasUserConfig(
            profiles={
                "default": GeasProfile(
                    ontology_directory=Path("ontologies"),
                    ontology_git=OntologyGitConfig(url=url, branch="main"),
                    secret_sources=(),
                )
            }
        ).explicit_yaml()
    )
    ontology = config.parent / "ontologies" / "test-ontology"
    ontology.mkdir(parents=True)
    (ontology / "build.yaml").write_text(
        OntologyBuildConfig(
            version=1,
            topic="Test ontology",
            topic_concept_id="concept:test",
            provider="deepseek_local",
            output_directory=Path("data/generated"),
        ).explicit_yaml()
    )
    (ontology / "library.yaml").write_text(
        SourceLibraryManifest(
            version=1,
            id="library:test",
            title="Test",
            description="Test ontology sources.",
            include_all_parsed_sources=True,
        ).explicit_yaml()
    )
    return config


class FakeOntologyRepository:
    commit = OLD_COMMIT
    pulls = 0

    def __init__(self, *, checkout: Path, config: OntologyGitConfig) -> None:
        self.checkout = checkout
        self.config = config

    def pull(self) -> dict[str, object]:
        type(self).pulls += 1
        return {
            "checkout": str(self.checkout),
            "repository": self.config.url,
            "branch": self.config.branch,
            "cloned": False,
            "pulled": True,
            "commit": type(self).commit,
        }


class FakeGeasUpdater:
    def update_and_reexec(
        self, argv: tuple[str, ...], *, continuation: str | None
    ) -> GeasUpdateReceipt:
        assert argv == tuple(sys.argv)
        assert continuation == "continued"
        return GeasUpdateReceipt(
            installer="git-development",
            directory=Path("/trusted/geas"),
            executable=Path("/trusted/geas/bin/geas"),
            old_commit=OLD_COMMIT,
            new_commit=NEW_COMMIT,
            old_version="0.1.0",
            new_version="0.1.0",
            reinstalled=True,
            reexec_depth=1,
        )


class MemoryArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir()
        self.values: dict[tuple[str, str], Path] = {}

    def ensure(self, artifact: OntologyArtifact, source: Path) -> bool:
        destination = self.root / artifact.asset_name
        shutil.copyfile(source, destination)
        self.values[(artifact.release_tag, artifact.asset_name)] = destination
        return True

    def available(self, artifact: OntologyArtifact) -> bool:
        return (artifact.release_tag, artifact.asset_name) in self.values

    def download(self, artifact: OntologyArtifact, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.values[(artifact.release_tag, artifact.asset_name)], destination)


def _git(cwd: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Geas Test",
            "GIT_AUTHOR_EMAIL": "geas-test@example.invalid",
            "GIT_COMMITTER_NAME": "Geas Test",
            "GIT_COMMITTER_EMAIL": "geas-test@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        },
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def _run_main(monkeypatch: pytest.MonkeyPatch, arguments: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["geas", *arguments])
    cli.main()


def _install_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    repository: Path | None = None,
) -> dict[str, object]:
    config = _config(tmp_path)
    FakeOntologyRepository.commit = OLD_COMMIT
    FakeOntologyRepository.pulls = 0
    monkeypatch.setattr(cli, "OntologyRepositoryManager", FakeOntologyRepository)
    monkeypatch.setattr(
        cli,
        "_load_portable_topic",
        lambda *_args, **_kwargs: (_topic(), {"verified": True, "path": "/projection"}),
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    arguments = [
        "--geas-config",
        str(config),
        "skill-export",
        "test-ontology",
    ]
    if repository is not None:
        arguments.extend(("--repo", str(repository)))
    _run_main(monkeypatch, arguments)
    return {"config": str(config)}


def test_skill_lifecycle_parser_accepts_only_the_documented_paths_and_flags() -> None:
    parser = cli._build_parser()

    exported = parser.parse_args(
        ["skill-export", "ontology", "--name", "expert", "--link", "--repo", ".", "--force"]
    )
    updated = parser.parse_args(
        ["skill-update", "snapshot/geas-skill.json", "--force", "--geas-update-continuation", "x"]
    )
    unlinked = parser.parse_args(["skill-unlink", "snapshot", "--force"])
    removed = parser.parse_args(["skill-remove", "snapshot/geas-skill.json", "--force"])

    assert (exported.ontology, exported.name, exported.link, exported.repo, exported.force) == (
        "ontology",
        "expert",
        True,
        Path("."),
        True,
    )
    assert (updated.skill_path, updated.force, updated.geas_update_continuation) == (
        Path("snapshot/geas-skill.json"),
        True,
        "x",
    )
    assert (unlinked.skill_path, unlinked.force) == (Path("snapshot"), True)
    assert (removed.skill_path, removed.force) == (
        Path("snapshot/geas-skill.json"),
        True,
    )


def test_user_export_is_repeatable_and_keeps_stdout_json_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    context = _install_export(tmp_path, monkeypatch)
    first = capsys.readouterr()
    first_receipt = json.loads(first.out)

    _run_main(
        monkeypatch,
        [
            "--geas-config",
            context["config"],
            "skill-export",
            "test-ontology",
        ],
    )
    second = capsys.readouterr()
    second_receipt = json.loads(second.out)

    assert first_receipt["unchanged"] is False
    assert second_receipt["unchanged"] is True
    assert first_receipt["snapshot_sha256"] == second_receipt["snapshot_sha256"]
    assert first.err and "skill" in first.err.casefold()
    assert second.err and "skill" in second.err.casefold()
    assert FakeOntologyRepository.pulls == 2


def test_repository_export_installs_at_the_git_scoped_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(("git", "init", "-b", "main"), cwd=repository, check=True, capture_output=True)

    _install_export(tmp_path, monkeypatch, repository=repository)
    receipt = json.loads(capsys.readouterr().out)

    assert Path(receipt["path"]) == repository / ".agents" / "skills" / "test-ontology"
    assert (repository / ".agents" / "skills" / "test-ontology" / "geas-skill.json").is_file()


def test_update_rejects_profile_locator_mismatch_before_ontology_pull(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    context = _install_export(tmp_path, monkeypatch)
    receipt = json.loads(capsys.readouterr().out)
    config = Path(context["config"])
    config.write_text(
        GeasUserConfig(
            profiles={
                "default": GeasProfile(
                    ontology_directory=Path("ontologies"),
                    ontology_git=OntologyGitConfig(
                        url="https://attacker.invalid/ontology.git", branch="main"
                    ),
                    secret_sources=(),
                )
            }
        ).explicit_yaml()
    )
    FakeOntologyRepository.pulls = 0
    monkeypatch.setattr(cli, "GeasUpdater", FakeGeasUpdater)

    with pytest.raises(ValueError, match="does not match"):
        _run_main(
            monkeypatch,
            [
                "--geas-config",
                str(config),
                "skill-update",
                receipt["path"],
                "--geas-update-continuation",
                "continued",
            ],
        )

    assert FakeOntologyRepository.pulls == 0


def test_later_artifact_failure_preserves_previous_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    context = _install_export(tmp_path, monkeypatch)
    receipt = json.loads(capsys.readouterr().out)
    snapshot = Path(receipt["path"])
    before = {
        path.relative_to(snapshot): path.read_bytes()
        for path in snapshot.rglob("*")
        if path.is_file()
    }
    FakeOntologyRepository.commit = NEW_COMMIT
    monkeypatch.setattr(cli, "GeasUpdater", FakeGeasUpdater)

    def fail_artifact(*_args: object, **_kwargs: object) -> object:
        raise ValueError("artifact verification failed")

    monkeypatch.setattr(cli, "_load_portable_topic", fail_artifact)

    with pytest.raises(ValueError, match="artifact verification failed"):
        _run_main(
            monkeypatch,
            [
                "--geas-config",
                context["config"],
                "skill-update",
                str(snapshot),
                "--geas-update-continuation",
                "continued",
            ],
        )

    captured = capsys.readouterr()
    after = {
        path.relative_to(snapshot): path.read_bytes()
        for path in snapshot.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert captured.out == ""
    failure = json.loads(captured.err.splitlines()[-1])
    assert failure["error"] == "skill-update-failed"
    assert failure["completed_phases"]["geas"]["new_commit"] == NEW_COMMIT


def test_update_fast_forwards_and_reports_sorted_file_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    context = _install_export(tmp_path, monkeypatch)
    original = json.loads(capsys.readouterr().out)
    snapshot = Path(original["path"])
    FakeOntologyRepository.commit = NEW_COMMIT
    monkeypatch.setattr(cli, "GeasUpdater", FakeGeasUpdater)
    monkeypatch.setattr(
        cli,
        "_load_portable_topic",
        lambda *_args, **_kwargs: (
            _topic("truth:new"),
            {"verified": True, "path": "/new-projection"},
        ),
    )

    _run_main(
        monkeypatch,
        [
            "--geas-config",
            context["config"],
            "skill-update",
            str(snapshot / "geas-skill.json"),
            "--geas-update-continuation",
            "continued",
        ],
    )
    receipt = json.loads(capsys.readouterr().out)
    manifest = json.loads((snapshot / "geas-skill.json").read_text())

    assert manifest["ontology"]["commit"] == NEW_COMMIT
    assert manifest["projection"]["snapshot_id"] == "truth:new"
    assert receipt["changed_paths"] == sorted(receipt["changed_paths"])
    assert "geas-skill.json" in receipt["changed_paths"]
    assert receipt["unchanged_paths"] == sorted(receipt["unchanged_paths"])
    assert receipt["conflicts"] == []
    assert receipt["ontology_update"] == {
        "old_commit": OLD_COMMIT,
        "new_commit": NEW_COMMIT,
    }
    assert [phase["phase"] for phase in receipt["phases"]] == sorted(
        phase["phase"] for phase in receipt["phases"]
    )


def test_unlink_remove_and_force_apply_only_to_the_exact_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    context = _install_export(tmp_path, monkeypatch)
    receipt = json.loads(capsys.readouterr().out)
    snapshot = Path(receipt["path"])
    unrelated = snapshot.parent / "unrelated"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("keep\n")

    _run_main(
        monkeypatch,
        ["--geas-config", context["config"], "skill-unlink", str(snapshot / "geas-skill.json")],
    )
    unlinked = json.loads(capsys.readouterr().out)
    assert unlinked["removed_snapshot"] is False
    assert snapshot.is_dir()

    (snapshot / "SKILL.md").write_text("modified\n")
    with pytest.raises(ValueError, match="unmanaged or modified"):
        _run_main(
            monkeypatch,
            ["--geas-config", context["config"], "skill-remove", str(snapshot)],
        )
    _run_main(
        monkeypatch,
        ["--geas-config", context["config"], "skill-remove", str(snapshot), "--force"],
    )
    removed = json.loads(capsys.readouterr().out)

    assert removed["removed_snapshot"] is True
    assert not snapshot.exists()
    assert (unrelated / "keep.txt").read_text() == "keep\n"


def test_update_rejects_invalid_manifest_path_before_geas_or_ontology_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    invalid = tmp_path / "not-a-skill"
    invalid.mkdir()
    FakeOntologyRepository.pulls = 0

    class MustNotUpdate:
        def update_and_reexec(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("Geas update must not run for an invalid skill path")

    monkeypatch.setattr(cli, "GeasUpdater", MustNotUpdate)
    monkeypatch.setattr(cli, "OntologyRepositoryManager", FakeOntologyRepository)

    with pytest.raises(ValueError, match="manifest|snapshot"):
        _run_main(
            monkeypatch,
            ["--geas-config", str(config), "skill-update", str(invalid)],
        )
    assert FakeOntologyRepository.pulls == 0


def test_refresh_known_link_conflict_preserves_snapshot_and_conflict(
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "config"
    home = tmp_path / "home"
    old_files = _skill_files(commit=OLD_COMMIT, snapshot="truth:old")
    installed = export_skill(
        old_files,
        config_root=config_root,
        home=home,
        repository=None,
        link=False,
        force=False,
        which=lambda _name: None,
    )
    before = _snapshot_bytes(installed.path)
    conflict = home / ".claude" / "skills" / "test-ontology"
    conflict.parent.mkdir(parents=True)
    conflict.write_bytes(b"operator-owned\n")

    with pytest.raises(ValueError, match="conflict"):
        refresh_skill(
            _skill_files(commit=NEW_COMMIT, snapshot="truth:new"),
            installed.path,
            config_root=config_root,
            home=home,
            force=False,
            which=lambda name: "/bin/claude" if name == "claude" else None,
        )

    assert _snapshot_bytes(installed.path) == before
    assert conflict.read_bytes() == b"operator-owned\n"


def test_refresh_rolls_back_snapshot_and_prior_link_on_mid_link_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_root = tmp_path / "config"
    home = tmp_path / "home"
    installed = export_skill(
        _skill_files(commit=OLD_COMMIT, snapshot="truth:old"),
        config_root=config_root,
        home=home,
        repository=None,
        link=False,
        force=False,
        which=lambda _name: None,
    )
    before = _snapshot_bytes(installed.path)
    original = Path.symlink_to
    calls = 0

    def fail_second_link(path: Path, target: Path, target_is_directory: bool = False) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second-link failure")
        original(path, target, target_is_directory=target_is_directory)

    monkeypatch.setattr(Path, "symlink_to", fail_second_link)

    with pytest.raises(OSError, match="second-link"):
        refresh_skill(
            _skill_files(commit=NEW_COMMIT, snapshot="truth:new"),
            installed.path,
            config_root=config_root,
            home=home,
            force=False,
            which=lambda name: "/bin/agent" if name in {"codex", "claude"} else None,
        )

    assert _snapshot_bytes(installed.path) == before
    assert not (home / ".agents" / "skills" / "test-ontology").exists()
    assert not (home / ".claude" / "skills" / "test-ontology").exists()


def test_skill_update_uses_real_exact_fetch_and_verified_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data = ImmutableStore(tmp_path / "projection-data")
    KnowledgeBundleImporter(store=data).import_bundle(
        Path("ontology/open-source-research-agents/bundle.yaml"),
        imported_by="operator:test",
    )
    truth = TruthManager(
        workspace_root=Path("."),
        store_root=data.root,
        policy=TruthPolicy.from_yaml(Path("config/truth-policy.yaml")),
        clock=lambda: INSTANT,
    )
    truth_snapshot = truth.capture(created_by="operator:test")
    database = tmp_path / "query.sqlite"
    SQLiteKnowledgeProjection(store=data, workspace_root=Path(".")).build(
        database,
        snapshot=truth_snapshot,
        truth_manager=truth,
    )
    artifact_store = MemoryArtifactStore(tmp_path / "artifact-store")

    remote = tmp_path / "ontology-remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-b", "main")
    _git(upstream, "remote", "add", "origin", str(remote))
    ontology = upstream / "test-ontology"
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
            id="library:test",
            title="Test",
            description="Verified projection integration fixture.",
            include_all_parsed_sources=True,
        ).explicit_yaml()
    )
    OntologyArtifactManager(ontology).publish(
        store=artifact_store,
        published_by="operator:test",
        storage_rights_basis="offline test fixture",
        knowledge_projection=database,
    )
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "initial verified ontology")
    _git(upstream, "push", "-u", "origin", "main")
    old_commit = _git(upstream, "rev-parse", "HEAD")

    original_execute = OntologyRepositoryManager._execute
    ontology_commands: list[tuple[str, ...]] = []

    def offline_git(
        command: tuple[str, ...], *, cwd: Path, check: bool
    ) -> subprocess.CompletedProcess[str]:
        ontology_commands.append(command)
        if command[:4] == ("git", "remote", "get-url", "origin"):
            return subprocess.CompletedProcess(command, 0, ONTOLOGY_URL + "\n", "")
        mapped = (
            "git",
            "-c",
            f"url.{remote}.insteadOf={ONTOLOGY_URL}",
            *command[1:],
        )
        return original_execute(mapped, cwd=cwd, check=check)

    monkeypatch.setattr(
        OntologyRepositoryManager,
        "_execute",
        staticmethod(offline_git),
    )
    config = tmp_path / "config" / "config.yaml"
    config.parent.mkdir()
    config.write_text(
        GeasUserConfig(
            profiles={
                "default": GeasProfile(
                    ontology_directory=Path("ontologies"),
                    ontology_git=OntologyGitConfig(url=ONTOLOGY_URL, branch="main"),
                    secret_sources=(),
                )
            }
        ).explicit_yaml()
    )
    checkout = config.parent / "ontologies"
    checkout.mkdir()
    _git(checkout, "init", "-b", "main")
    _git(checkout, "remote", "add", "origin", ONTOLOGY_URL)
    OntologyRepositoryManager(
        checkout=checkout,
        config=OntologyGitConfig(url=ONTOLOGY_URL, branch="main"),
    ).pull()

    topic = KnowledgeQueryEngine(database).topic("concept:open-source-research-agents")
    files = render_ontology_skill(
        topic,
        skill_name="test-ontology",
        ontology_name="test-ontology",
        repository_url=ONTOLOGY_URL,
        branch="main",
        ontology_commit=old_commit,
        geas_version="0.1.0",
        geas_commit=OLD_COMMIT,
    )
    installed = export_skill(
        files,
        config_root=config.parent,
        home=tmp_path / "home",
        repository=None,
        link=False,
        force=False,
        which=lambda _name: None,
    )

    (upstream / "legitimate.yaml").write_text("trusted: true\n")
    _git(upstream, "add", "legitimate.yaml")
    _git(upstream, "commit", "-m", "trusted fast forward")
    _git(upstream, "push", "origin", "main")
    new_commit = _git(upstream, "rev-parse", "HEAD")
    _git(checkout, "switch", "-c", "forged")
    (checkout / "malicious.yaml").write_text("trusted: false\n")
    _git(checkout, "add", "malicious.yaml")
    _git(checkout, "commit", "-m", "forged local tracking target")
    forged = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "switch", "main")
    _git(checkout, "update-ref", "refs/remotes/origin/main", forged)
    _git(
        checkout,
        "config",
        "remote.origin.fetch",
        "+refs/heads/evil:refs/remotes/origin/main",
    )

    monkeypatch.setattr(cli, "GeasUpdater", FakeGeasUpdater)
    monkeypatch.setattr(
        cli,
        "GitHubReleaseArtifactStore",
        lambda *_args, **_kwargs: artifact_store,
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    _run_main(
        monkeypatch,
        [
            "--geas-config",
            str(config),
            "skill-update",
            str(installed.path),
            "--geas-update-continuation",
            "continued",
        ],
    )
    receipt = json.loads(capsys.readouterr().out)
    manifest = json.loads((installed.path / "geas-skill.json").read_text())

    assert _git(checkout, "rev-parse", "HEAD") == new_commit
    assert not (checkout / "malicious.yaml").exists()
    assert (
        "git",
        "fetch",
        "--no-tags",
        "origin",
        "+refs/heads/main:refs/geas-sync/main",
    ) in ontology_commands
    assert receipt["ontology_update"] == {
        "old_commit": old_commit,
        "new_commit": new_commit,
    }
    assert manifest["ontology"]["commit"] == new_commit
    assert manifest["projection"]["snapshot_id"] == truth_snapshot.id
