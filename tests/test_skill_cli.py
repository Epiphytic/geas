from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import research_agent.cli as cli
from research_agent.geas_update import GeasUpdateReceipt
from research_agent.library import SourceLibraryManifest
from research_agent.ontology_build import OntologyBuildConfig
from research_agent.projection import TopicView
from research_agent.user_config import GeasProfile, GeasUserConfig, OntologyGitConfig

ONTOLOGY_URL = "https://example.test/ontologies.git"
OLD_COMMIT = "a" * 40
NEW_COMMIT = "b" * 40


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
            old_commit=OLD_COMMIT,
            new_commit=NEW_COMMIT,
            old_version="0.1.0",
            new_version="0.1.0",
            reinstalled=True,
            reexec_depth=1,
        )


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

    after = {
        path.relative_to(snapshot): path.read_bytes()
        for path in snapshot.rglob("*")
        if path.is_file()
    }
    assert after == before


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
