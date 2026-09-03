from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from research_agent import cli
from research_agent.agent_skills import (
    GeasIdentity,
    OntologyIdentity,
    PortableArtifactIdentity,
    ProjectionIdentity,
    SkillFile,
    SkillIdentity,
    SkillManifest,
    canonical_manifest_bytes,
    snapshot_digest,
)
from research_agent.bootstrap_models import (
    BootstrapPhase,
    ManagedPath,
    RepositoryBootstrapReceipt,
    RepositoryBootstrapRequest,
    VerifiedRepositoryBootstrap,
)
from research_agent.capabilities import (
    Capability,
    CapabilityDecision,
    CapabilityGrant,
    CapabilityRequest,
    CapabilityResources,
    CapabilitySubject,
    DelegationManifest,
)
from research_agent.library import SourceLibraryManifest
from research_agent.ontology_build import OntologyBuildConfig, OntologyUpdateService
from research_agent.ontology_resolution import OntologySelection
from research_agent.operator_policy import ResearchPolicy
from research_agent.publishing import PublishMode, PublishResult
from research_agent.remote_acquisition import SourceFetchResult
from research_agent.repository_catalog import (
    CatalogFile,
    CatalogOntology,
    RepositoryCatalog,
    ResolvedRepositoryCatalog,
    load_catalog,
    ontology_bundle_sha256,
)
from research_agent.source_intent import (
    DiscoveryKind,
    SourceAssociations,
    SourceCandidate,
    SourceDiscovery,
    SourceIntent,
    SourceRefreshPolicy,
    SourceTemporalPolicy,
)
from research_agent.source_work import SourceAuthorityContext, SourceUpdateReceipt
from research_agent.user_config import (
    GeasProfile,
    GeasUserConfig,
    OntologyGitConfig,
    UserConfigManager,
)

NOW = datetime(2026, 9, 3, 12, tzinfo=UTC)
REPOSITORY = "https://github.com/example/gold"


def test_repository_install_parser_defaults_to_pull_request_publication() -> None:
    args = cli._build_parser().parse_args(
        [
            "repository-install",
            "gold",
            "https://github.com/example/gold.git",
            "--trust-repository",
            "--link",
        ]
    )

    assert args.name == "gold"
    assert args.url == "https://github.com/example/gold.git"
    assert args.trust_repository is True
    assert args.read_only is False
    assert args.delegate_depth == 1
    assert args.link is True
    assert args.publish == "pull-request"
    assert args.direct_push is False


def test_repository_install_parser_supports_remote_and_current_repository_forms() -> None:
    parser = cli._build_parser()

    remote = parser.parse_args(
        [
            "--geas-profile",
            "research",
            "repository-install",
            "gold",
            "https://github.com/example/gold.git",
            "--ref",
            "refs/tags/v2",
            "--catalog",
            "catalog/geas.yaml",
            "--read-only",
            "--publish",
            "none",
        ]
    )
    current = parser.parse_args(
        [
            "--geas-profile",
            "research",
            "repository-install",
            "--current-repository",
            "--trust-repository",
            "--delegate-depth",
            "2",
            "--direct-push",
        ]
    )

    assert (remote.name, remote.url, remote.active_ref, remote.catalog) == (
        "gold",
        "https://github.com/example/gold.git",
        "refs/tags/v2",
        Path("catalog/geas.yaml"),
    )
    assert remote.geas_profile == "research"
    assert remote.read_only is True
    assert remote.publish == "none"
    assert current.current_repository is True
    assert current.name is None
    assert current.url is None
    assert current.trust_repository is True
    assert current.delegate_depth == 2
    assert current.direct_push is True


@pytest.mark.parametrize(
    "arguments",
    (
        (
            "repository-install",
            "gold",
            "https://github.com/example/gold.git",
            "--current-repository",
        ),
        ("repository-install", "gold"),
        ("repository-install", "--trust-repository", "--read-only", "--current-repository"),
        ("repository-install", "--current-repository", "--delegate-depth", "2"),
        ("repository-install", "--current-repository", "--publish", "none", "--direct-push"),
    ),
)
def test_repository_install_parser_rejects_ambiguous_or_incomplete_forms(
    arguments: tuple[str, ...],
) -> None:
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(arguments)


@pytest.mark.parametrize("ref", ("main", "heads/main", "refs/heads/main/", "HEAD"))
def test_repository_commands_require_full_safe_refs(ref: str) -> None:
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(
            [
                "repository-install",
                "gold",
                "https://github.com/example/gold.git",
                "--ref",
                ref,
            ]
        )


@pytest.mark.parametrize("depth", ("-1", "33", "not-an-integer"))
def test_repository_install_rejects_invalid_delegation_depth_at_parse_time(
    depth: str,
) -> None:
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(
            [
                "repository-install",
                "--current-repository",
                "--trust-repository",
                "--delegate-depth",
                depth,
            ]
        )


def test_update_remove_and_ontology_update_parser_surfaces_are_exact() -> None:
    parser = cli._build_parser()

    ontology = parser.parse_args(
        ["--geas-profile", "research", "ontology-update", "gold", "--root", "runtime"]
    )
    update = parser.parse_args(
        [
            "--geas-profile",
            "research",
            "repository-update",
            "gold",
            "--ref",
            "refs/heads/review",
            "--link",
            "--publish",
            "none",
            "--message",
            "refresh gold",
        ]
    )
    remove = parser.parse_args(
        ["--geas-profile", "research", "repository-remove", "gold"]
    )
    legacy_sync = parser.parse_args(
        ["ontology-sync", "gold", "--push", "--direct-push", "--message", "sync gold"]
    )

    assert (ontology.name, ontology.root, ontology.geas_profile) == (
        "gold",
        Path("runtime"),
        "research",
    )
    assert (update.name, update.active_ref, update.link, update.publish, update.message) == (
        "gold",
        "refs/heads/review",
        True,
        "none",
        "refresh gold",
    )
    assert (remove.name, remove.geas_profile) == ("gold", "research")
    assert legacy_sync.direct_push is True
    assert legacy_sync.message == "sync gold"


def _run_main(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> None:
    monkeypatch.setattr(sys, "argv", ["geas", *arguments])
    cli.main()


def test_ontology_update_handler_invokes_one_composed_service_and_separates_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, datetime]] = []
    receipt = SourceUpdateReceipt(
        source_intent_id="source-update-batch",
        source_intent_ids=("alpha", "zeta"),
        complete=True,
        finalized_at=NOW,
    )

    class Service:
        def update(self, name: str, *, now: datetime) -> SourceUpdateReceipt:
            calls.append((name, now))
            return receipt

    built: list[object] = []

    def build(args: object) -> Service:
        built.append(args)
        return Service()

    monkeypatch.setattr(cli, "_ontology_update_service", build)
    monkeypatch.setattr(cli, "utc_now", lambda: NOW)

    _run_main(
        monkeypatch,
        "--geas-config",
        str(tmp_path / "config.yaml"),
        "--geas-profile",
        "research",
        "ontology-update",
        "gold",
        "--root",
        str(tmp_path / "runtime"),
    )

    output = capsys.readouterr()
    assert len(built) == 1
    assert calls == [("gold", NOW)]
    assert json.loads(output.out)["source_intent_ids"] == ["alpha", "zeta"]
    assert output.out.count("\n{") == 0
    assert "Updating ontology 'gold'" in output.err


@pytest.mark.parametrize(
    ("command", "method"),
    (
        (("repository-install", "gold", "https://github.com/example/gold.git"), "install"),
        (("repository-update", "gold", "--publish", "none"), "update"),
        (("repository-remove", "gold"), "remove"),
    ),
)
def test_repository_handlers_invoke_one_domain_method_and_emit_one_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: tuple[str, ...],
    method: str,
) -> None:
    events: list[str] = []
    request = SimpleNamespace(
        name="gold",
        repository="https://github.com/example/gold",
        ref="refs/heads/main",
    )
    publication_requested = method == "install"

    class Service:
        def install(self, supplied: object) -> dict[str, object]:
            events.append("install")
            assert supplied is request
            return {"managed_paths": ["b", "a"], "action": "install"}

        def update(self, supplied: object) -> dict[str, object]:
            events.append("update")
            assert supplied is request
            return {"managed_paths": ["b", "a"], "action": "update"}

        def remove(self, supplied: object) -> dict[str, object]:
            events.append("remove")
            assert supplied is request
            return {"managed_paths": [], "action": "remove"}

    monkeypatch.setattr(
        cli,
        "_initial_repository_publication_scope",
        lambda _args, *, action: events.append("scope")
        or (request if publication_requested else None),
    )
    monkeypatch.setattr(
        cli,
        "_repository_bootstrap_request",
        lambda _args, *, action: events.append(f"request:{action}") or request,
    )
    monkeypatch.setattr(
        cli,
        "_repository_bootstrap_service",
        lambda _args: events.append("service") or Service(),
    )
    monkeypatch.setattr(
        cli,
        "_preauthorize_repository_publication",
        lambda _args, _request: events.append("preauthorize"),
    )
    monkeypatch.setattr(
        cli,
        "_publish_repository_receipt",
        lambda _args, _request, receipt: events.append("publish") or receipt,
    )

    _run_main(
        monkeypatch,
        "--geas-config",
        str(tmp_path / "config.yaml"),
        *command,
    )

    output = capsys.readouterr()
    payload = json.loads(output.out)
    expected = ["scope"]
    if publication_requested:
        expected.append("preauthorize")
    expected.append(f"request:{method}")
    expected.extend(("service", method, "publish"))
    assert events == expected
    assert payload["action"] == method
    assert output.out.count("\n{") == 0
    assert "repository" in output.err.casefold()


def test_repository_direct_push_denial_precedes_lifecycle_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    scope = SimpleNamespace(
        repository="https://github.com/example/gold",
        ref="refs/heads/main",
    )
    monkeypatch.setattr(
        cli,
        "_initial_repository_publication_scope",
        lambda _args, *, action: events.append("scope") or scope,
        raising=False,
    )
    monkeypatch.setattr(
        cli,
        "_preauthorize_repository_publication",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )
    monkeypatch.setattr(
        cli,
        "_repository_bootstrap_request",
        lambda _args, *, action: (_ for _ in ()).throw(
            AssertionError("remote verification ran after initial publication denial")
        ),
    )
    monkeypatch.setattr(
        cli,
        "_repository_bootstrap_service",
        lambda _args: (_ for _ in ()).throw(AssertionError("lifecycle constructed after denial")),
    )
    monkeypatch.setattr(
        cli,
        "recover_managed_removals",
        lambda _manager: (_ for _ in ()).throw(
            AssertionError("filesystem recovery ran after initial publication denial")
        ),
    )

    with pytest.raises(PermissionError, match="denied"):
        _run_main(
            monkeypatch,
            "--geas-config",
            str(tmp_path / "config.yaml"),
            "repository-install",
            "gold",
            "https://github.com/example/gold.git",
            "--direct-push",
        )

    assert events == ["scope"]


def test_remote_install_initial_publication_scope_uses_only_cli_and_config_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    UserConfigManager(config_path).replace(
        GeasUserConfig(profiles={"default": GeasProfile(ontology_git=None)})
    )
    args = cli._build_parser().parse_args(
        [
            "--geas-config",
            str(config_path),
            "repository-install",
            "gold",
            "https://github.com/example/gold.git",
            "--ref",
            "refs/heads/review",
        ]
    )
    monkeypatch.setattr(
        cli,
        "_inspect_repository_bootstrap",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("initial publication scope performed remote inspection")
        ),
    )

    scope = cli._initial_repository_publication_scope(args, action="install")

    assert scope is not None
    assert scope.repository == "https://github.com/example/gold"
    assert scope.ref == "refs/heads/review"


def test_current_install_initial_publication_scope_uses_only_read_only_local_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    _bootstrap_skill_receipt(repository)
    monkeypatch.chdir(repository)
    monkeypatch.setattr(
        cli,
        "_inspect_repository_bootstrap",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("initial current-worktree scope performed catalog inspection")
        ),
    )
    args = cli._build_parser().parse_args(
        [
            "repository-install",
            "--current-repository",
            "--ref",
            "refs/heads/main",
        ]
    )

    scope = cli._initial_repository_publication_scope(args, action="install")

    assert scope is not None
    assert scope.repository == REPOSITORY
    assert scope.ref == "refs/heads/main"


def test_current_repository_identity_ignores_another_repository_from_git_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def repository_fixture(path: Path, *, name: str, remote: str, content: bytes) -> str:
        ontology = path / "ontology" / name
        ontology.mkdir(parents=True)
        source = ontology / "source.txt"
        source.write_bytes(content)
        catalog_file = CatalogFile(
            path=Path("source.txt"),
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )
        catalog_ontology = CatalogOntology(
            name=name,
            description=f"{name} ontology",
            path=Path("ontology") / name,
            files=(catalog_file,),
            bundle_sha256="0" * 64,
        )
        catalog_ontology = catalog_ontology.model_copy(
            update={"bundle_sha256": ontology_bundle_sha256(catalog_ontology)}
        )
        (path / "geas.yaml").write_text(
            RepositoryCatalog(ontologies=(catalog_ontology,)).model_dump_json(indent=2)
        )
        subprocess.run(
            ("git", "init", "--initial-branch=main", str(path)),
            text=True,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ("git", "-C", str(path), "remote", "add", "origin", remote),
            check=True,
        )
        subprocess.run(
            ("git", "-C", str(path), "add", "geas.yaml", f"ontology/{name}/source.txt"),
            check=True,
        )
        subprocess.run(
            (
                "git",
                "-C",
                str(path),
                "-c",
                "user.name=Geas Test",
                "-c",
                "user.email=geas@example.invalid",
                "commit",
                "-m",
                f"fixture {name}",
            ),
            text=True,
            capture_output=True,
            check=True,
        )
        return subprocess.run(
            ("git", "-C", str(path), "rev-parse", "HEAD"),
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    repository_a = tmp_path / "repository-a"
    repository_b = tmp_path / "repository-b"
    remote_a = "https://github.com/example/repository-a"
    remote_b = "https://github.com/example/repository-b"
    commit_a = repository_fixture(
        repository_a,
        name="alpha",
        remote=remote_a,
        content=b"repository A\n",
    )
    repository_fixture(
        repository_b,
        name="beta",
        remote=remote_b,
        content=b"repository B\n",
    )
    config_path = tmp_path / "config" / "config.yaml"
    UserConfigManager(config_path).replace(
        GeasUserConfig(profiles={"default": GeasProfile(ontology_git=None)})
    )
    monkeypatch.chdir(repository_a)
    monkeypatch.setenv("GIT_DIR", str(repository_b / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repository_b))
    monkeypatch.setenv("GIT_INDEX_FILE", str(repository_b / ".git" / "index"))
    scope_args = cli._build_parser().parse_args(
        [
            "--geas-config",
            str(config_path),
            "repository-install",
            "--current-repository",
        ]
    )
    request_args = cli._build_parser().parse_args(
        [
            "--geas-config",
            str(config_path),
            "repository-install",
            "--current-repository",
            "--publish",
            "none",
        ]
    )

    scope = cli._initial_repository_publication_scope(scope_args, action="install")
    request = cli._repository_bootstrap_request(request_args, action="install")

    assert scope is not None
    assert scope.repository == remote_a
    assert request.repository == remote_a
    assert request.commit_sha256 == commit_a
    assert request.current_worktree == repository_a.resolve()
    assert request.ontology_paths == ()


def test_update_publication_scope_uses_every_path_from_the_exact_prior_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    receipt = _bootstrap_skill_receipt(repository)
    assert (
        cli._prior_receipt_publication_targets(
            receipt,
            repository=repository,
        )
        is None
    )
    generic_root = repository / ".agents" / "skills" / "geas"
    generic_root.mkdir(parents=True)
    generic_content = b"# Generic Geas skill\n"
    (generic_root / "SKILL.md").write_bytes(generic_content)
    generic_inventory = (
        SkillFile(
            path="SKILL.md",
            sha256=hashlib.sha256(generic_content).hexdigest(),
        ),
    )
    generic_manifest = SkillManifest(
        format_version=1,
        skill=SkillIdentity(name="geas"),
        ontology=OntologyIdentity(
            name="geas",
            repository_url="https://github.com/Epiphytic/geas.git",
            branch="main",
            commit="0" * 40,
        ),
        geas=GeasIdentity(
            project_url="https://github.com/Epiphytic/geas",
            version="1.0.0",
            commit=None,
        ),
        projection=ProjectionIdentity(
            snapshot_id="builtin:geas",
            topic_concept_id="builtin:geas",
        ),
        files=generic_inventory,
        snapshot_sha256=snapshot_digest(generic_inventory),
    )
    (generic_root / "geas-skill.json").write_bytes(
        canonical_manifest_bytes(generic_manifest)
    )
    generic_paths = tuple(
        ManagedPath(
            path=path.relative_to(repository).as_posix(),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            role="manifest" if path.name == "geas-skill.json" else "skill",
        )
        for path in sorted(generic_root.rglob("*"))
        if path.is_file()
    )
    receipt = receipt.model_copy(
        update={"managed_paths": (*receipt.managed_paths, *generic_paths)}
    )
    config_path = tmp_path / "config" / "config.yaml"
    UserConfigManager(config_path).replace(
        GeasUserConfig(profiles={"default": GeasProfile(ontology_git=None)})
    )
    monkeypatch.setattr(
        cli,
        "_load_repository_bootstrap_receipt",
        lambda _manager, _name: receipt,
    )
    args = cli._build_parser().parse_args(
        [
            "--geas-config",
            str(config_path),
            "repository-update",
            "gold",
            "--direct-push",
        ]
    )

    scope = cli._initial_repository_publication_scope(args, action="update")

    assert scope is not None
    assert scope.publication_targets == (
        (".agents/skills/geas/SKILL.md", None),
        (".agents/skills/geas/geas-skill.json", None),
        (
            ".agents/skills/gold/SKILL.md",
            receipt.verified.bundle_sha256[0],
        ),
        (
            ".agents/skills/gold/geas-skill.json",
            receipt.verified.bundle_sha256[0],
        ),
    )


def test_verified_repository_request_must_match_initial_publication_scope_before_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = SimpleNamespace(
        repository="https://github.com/example/gold",
        ref="refs/heads/main",
    )
    changed = SimpleNamespace(
        name="gold",
        repository="https://github.com/example/other",
        ref="refs/heads/main",
    )
    monkeypatch.setattr(
        cli,
        "_initial_repository_publication_scope",
        lambda _args, *, action: initial,
        raising=False,
    )
    monkeypatch.setattr(cli, "_preauthorize_repository_publication", lambda *_args: None)
    monkeypatch.setattr(
        cli,
        "_repository_bootstrap_request",
        lambda _args, *, action: changed,
    )
    monkeypatch.setattr(
        cli,
        "_repository_bootstrap_service",
        lambda _args: (_ for _ in ()).throw(
            AssertionError("lifecycle constructed after verified identity changed")
        ),
    )

    with pytest.raises(ValueError, match="differs from the preauthorized scope"):
        _run_main(
            monkeypatch,
            "--geas-config",
            str(tmp_path / "config.yaml"),
            "repository-install",
            "gold",
            initial.repository,
        )


def test_repository_pull_request_authentication_failure_precedes_lifecycle_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    config_path = tmp_path / "config.yaml"
    UserConfigManager(config_path).replace(
        GeasUserConfig(
            version=2,
            profiles={
                "default": GeasProfile(
                    ontology_git=None,
                    capability_grants=(
                        _git_prepublication_grant(
                            capability=Capability.GIT_PULL_REQUEST
                        ),
                    ),
                )
            },
        ),
        upgrade_version=True,
    )
    request = SimpleNamespace(
        name="gold",
        repository="https://github.com/example/gold",
        ref="refs/heads/main",
    )

    class Forge:
        def __init__(self, *, executable: str) -> None:
            assert executable == "/usr/bin/gh"
            events.append("forge")

        def assert_authenticated(self, *, repository: str) -> None:
            assert repository == request.repository
            events.append("authenticate")
            raise PermissionError("not authenticated")

    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(cli, "GitHubCliForgeClient", Forge)
    monkeypatch.setattr(
        cli,
        "_initial_repository_publication_scope",
        lambda _args, *, action: events.append("scope") or request,
    )
    monkeypatch.setattr(
        cli,
        "_repository_bootstrap_request",
        lambda _args, *, action: (_ for _ in ()).throw(
            AssertionError("remote verification ran after forge authentication denial")
        ),
    )
    monkeypatch.setattr(
        cli,
        "_repository_bootstrap_service",
        lambda _args: (_ for _ in ()).throw(
            AssertionError("lifecycle constructed after forge authentication denial")
        ),
    )

    with pytest.raises(PermissionError, match="not authenticated"):
        _run_main(
            monkeypatch,
            "--geas-config",
            str(config_path),
            "repository-install",
            "gold",
            request.repository,
        )

    assert events == ["scope", "forge", "authenticate"]


def test_repository_pull_request_grant_denial_precedes_forge_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    UserConfigManager(config_path).replace(
        GeasUserConfig(profiles={"default": GeasProfile(ontology_git=None)})
    )
    request = RepositoryBootstrapRequest(
        name="gold",
        repository=REPOSITORY,
        ref="refs/heads/main",
        catalog="geas.yaml",
        commit_sha256="b" * 40,
    )
    args = SimpleNamespace(
        geas_config=config_path,
        geas_profile="default",
        yolo=False,
        direct_push=False,
        publish="pull-request",
    )
    monkeypatch.setattr(
        cli,
        "_github_forge_client",
        lambda _repository: (_ for _ in ()).throw(
            AssertionError("forge called after local grant denial")
        ),
    )

    with pytest.raises(PermissionError, match="exact root-local git.pull_request"):
        cli._preauthorize_repository_publication(args, request)


def test_remote_install_partial_path_grant_denies_before_any_downstream_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    UserConfigManager(config_path).replace(
        GeasUserConfig(
            version=2,
            profiles={
                "default": GeasProfile(
                    ontology_git=None,
                    capability_grants=(
                        _git_publication_grant(
                            capability=Capability.GIT_PULL_REQUEST
                        ),
                    ),
                )
            },
        ),
        upgrade_version=True,
    )
    forbidden = {
        "forge": lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("forge authentication ran after partial grant")
        ),
        "request": lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("remote verification ran after partial grant")
        ),
        "service": lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("lifecycle construction ran after partial grant")
        ),
        "recovery": lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("filesystem recovery ran after partial grant")
        ),
    }
    monkeypatch.setattr(cli, "_github_forge_client", forbidden["forge"])
    monkeypatch.setattr(cli, "_repository_bootstrap_request", forbidden["request"])
    monkeypatch.setattr(cli, "_repository_bootstrap_service", forbidden["service"])
    monkeypatch.setattr(cli, "recover_managed_removals", forbidden["recovery"])

    with pytest.raises(PermissionError, match="exact root-local git.pull_request"):
        _run_main(
            monkeypatch,
            "--geas-config",
            str(config_path),
            "repository-install",
            "gold",
            REPOSITORY,
        )


def test_repository_publication_rejects_tag_before_authority_or_forge_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = RepositoryBootstrapRequest(
        name="gold",
        repository=REPOSITORY,
        ref="refs/tags/v1",
        catalog="geas.yaml",
        commit_sha256="b" * 40,
    )
    args = SimpleNamespace(direct_push=False, publish="pull-request")
    monkeypatch.setattr(
        cli,
        "_user_config_manager",
        lambda _args: (_ for _ in ()).throw(
            AssertionError("authority configuration opened for a read-only ref")
        ),
    )
    monkeypatch.setattr(
        cli,
        "_github_forge_client",
        lambda _repository: (_ for _ in ()).throw(
            AssertionError("forge called for a read-only ref")
        ),
    )

    with pytest.raises(PermissionError, match="branch ref"):
        cli._preauthorize_repository_publication(args, request)


def test_repository_install_request_uses_only_the_verified_catalog_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config" / "config.yaml"
    UserConfigManager(config_path).replace(
        GeasUserConfig(profiles={"default": GeasProfile(ontology_git=None)})
    )
    verified = VerifiedRepositoryBootstrap(
        repository="https://github.com/example/gold",
        ref="refs/heads/main",
        catalog="catalog/geas.yaml",
        commit_sha256="b" * 40,
        ontology_paths=("ontology/gold",),
        bundle_sha256=("a" * 64,),
        source_hosts=("issuer.example",),
        source_path_prefixes=("/reports",),
        source_connectors=("source:direct-url",),
        delegated_repositories=("https://github.com/example/silver",),
    )
    inspected: list[dict[str, object]] = []

    def inspect(**values: object) -> VerifiedRepositoryBootstrap:
        inspected.append(values)
        return verified

    monkeypatch.setattr(cli, "_inspect_repository_bootstrap", inspect)
    args = cli._build_parser().parse_args(
        [
            "--geas-config",
            str(config_path),
            "repository-install",
            "gold",
            "https://github.com/example/gold.git",
            "--ref",
            "refs/heads/main",
            "--catalog",
            "catalog/geas.yaml",
            "--trust-repository",
            "--delegate-depth",
            "2",
            "--publish",
            "none",
        ]
    )

    request = cli._repository_bootstrap_request(args, action="install")

    assert len(inspected) == 1
    assert inspected[0]["repository"] == "https://github.com/example/gold"
    assert request.repository == verified.repository
    assert request.commit_sha256 == verified.commit_sha256
    assert request.trust == "trust_repository"
    assert request.delegate_depth == 2
    assert request.ontology_paths == verified.ontology_paths
    assert request.bundle_sha256 == verified.bundle_sha256
    assert request.source_hosts == verified.source_hosts
    assert request.source_path_prefixes == verified.source_path_prefixes
    assert request.source_connectors == verified.source_connectors
    assert request.delegated_repositories == verified.delegated_repositories


@pytest.mark.parametrize(("flag", "trust"), (("--read-only", "read_only"), (None, "none")))
def test_repository_install_request_does_not_adopt_catalog_scopes_without_explicit_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str | None,
    trust: str,
) -> None:
    config_path = tmp_path / "config" / "config.yaml"
    UserConfigManager(config_path).replace(
        GeasUserConfig(profiles={"default": GeasProfile(ontology_git=None)})
    )
    monkeypatch.setattr(
        cli,
        "_inspect_repository_bootstrap",
        lambda **_values: VerifiedRepositoryBootstrap(
            repository="https://github.com/example/gold",
            ref="refs/heads/main",
            catalog="geas.yaml",
            commit_sha256="b" * 40,
            ontology_paths=("ontology/gold",),
            bundle_sha256=("a" * 64,),
            source_hosts=("issuer.example",),
            source_path_prefixes=("/",),
            source_connectors=("source:direct-url",),
        ),
    )
    arguments = [
        "--geas-config",
        str(config_path),
        "repository-install",
        "gold",
        "https://github.com/example/gold",
        "--publish",
        "none",
    ]
    if flag is not None:
        arguments.append(flag)

    request = cli._repository_bootstrap_request(
        cli._build_parser().parse_args(arguments),
        action="install",
    )

    assert request.trust == trust
    assert request.ontology_paths == ()
    assert request.bundle_sha256 == ()
    assert request.source_hosts == ()
    assert request.source_path_prefixes == ()
    assert request.source_connectors == ()


def _bootstrap_skill_receipt(
    repository: Path,
    *,
    manifest_commit: str | None = None,
    manifest_bundle_sha256: str | None = None,
) -> RepositoryBootstrapReceipt:
    ontology_root = repository / "ontology" / "gold"
    ontology_root.mkdir(parents=True)
    ontology_content = b"topic: Gold\n"
    (ontology_root / "build.yaml").write_bytes(ontology_content)
    catalog_file = CatalogFile(
        path=Path("build.yaml"),
        sha256=hashlib.sha256(ontology_content).hexdigest(),
        size_bytes=len(ontology_content),
    )
    catalog_ontology = CatalogOntology(
        name="gold",
        description="Gold ontology",
        path=Path("ontology/gold"),
        files=(catalog_file,),
        bundle_sha256="0" * 64,
    )
    catalog_ontology = catalog_ontology.model_copy(
        update={"bundle_sha256": ontology_bundle_sha256(catalog_ontology)}
    )
    (repository / "geas.yaml").write_text(
        RepositoryCatalog(ontologies=(catalog_ontology,)).model_dump_json(indent=2)
    )
    subprocess.run(
        ("git", "init", "--initial-branch=main", str(repository)),
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.name", "Geas Test"),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "config",
            "user.email",
            "geas@example.invalid",
        ),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(repository), "remote", "add", "origin", REPOSITORY),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(repository), "add", "geas.yaml", "ontology/gold/build.yaml"),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(repository), "commit", "-m", "fixture catalog"),
        text=True,
        capture_output=True,
        check=True,
    )
    commit = subprocess.run(
        ("git", "-C", str(repository), "rev-parse", "HEAD"),
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    skill_root = repository / ".agents" / "skills" / "gold"
    skill_root.mkdir(parents=True)
    content = b"# Gold research skill\n"
    (skill_root / "SKILL.md").write_bytes(content)
    inventory = (
        SkillFile(path="SKILL.md", sha256=hashlib.sha256(content).hexdigest()),
    )
    bundle_sha256 = catalog_ontology.bundle_sha256
    exported_bundle_sha256 = manifest_bundle_sha256 or bundle_sha256
    exported_commit = manifest_commit or commit
    manifest = SkillManifest(
        format_version=2,
        skill=SkillIdentity(name="gold"),
        ontology=OntologyIdentity(
            name="gold",
            repository_url=REPOSITORY,
            branch="main",
            commit=exported_commit,
            active_ref="refs/heads/main",
            ontology_commit=exported_commit,
            subscription_name="gold",
            catalog_path="geas.yaml",
            ontology_path="ontology/gold",
            bundle_sha256=exported_bundle_sha256,
        ),
        geas=GeasIdentity(
            project_url="https://github.com/Epiphytic/geas",
            version="1.0.0",
            commit=None,
        ),
        projection=ProjectionIdentity(
            snapshot_id="truth:sha256:gold",
            topic_concept_id="concept:gold",
        ),
        artifact=PortableArtifactIdentity(
            role="knowledge-projection",
            content_sha256="c" * 64,
            input_revision="d" * 64,
        ),
        files=inventory,
        snapshot_sha256=snapshot_digest(inventory),
    )
    (skill_root / "geas-skill.json").write_bytes(canonical_manifest_bytes(manifest))
    managed_paths = tuple(
        ManagedPath(
            path=path.relative_to(repository).as_posix(),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            role="manifest" if path.name == "geas-skill.json" else "skill",
        )
        for path in sorted(skill_root.rglob("*"))
        if path.is_file()
    )
    request = RepositoryBootstrapRequest(
        name="gold",
        repository=REPOSITORY,
        ref="refs/heads/main",
        catalog="geas.yaml",
        commit_sha256=commit,
        current_worktree=repository.resolve(),
    )
    verified = VerifiedRepositoryBootstrap(
        repository=REPOSITORY,
        ref="refs/heads/main",
        catalog="geas.yaml",
        commit_sha256=commit,
        ontology_paths=("ontology/gold",),
        bundle_sha256=tuple(sorted({bundle_sha256, exported_bundle_sha256})),
        current_worktree=repository.resolve(),
    )
    return RepositoryBootstrapReceipt(
        request=request,
        verified=verified,
        completed_phases=(
            BootstrapPhase.VERIFIED,
            BootstrapPhase.TRUST_COMMITTED,
            BootstrapPhase.SUBSCRIBED,
            BootstrapPhase.SKILLS_INSTALLED,
            BootstrapPhase.COMPLETED,
        ),
        managed_paths=managed_paths,
        created_at=NOW,
        updated_at=NOW,
    )


def _multi_ontology_bootstrap_receipt(repository: Path) -> RepositoryBootstrapReceipt:
    receipt = _bootstrap_skill_receipt(repository)
    catalog = load_catalog(repository / "geas.yaml")
    gold = catalog.ontologies[0]
    silver_root = repository / "ontology" / "silver"
    silver_root.mkdir(parents=True)
    silver_content = b"topic: Silver\n"
    (silver_root / "build.yaml").write_bytes(silver_content)
    silver_file = CatalogFile(
        path=Path("build.yaml"),
        sha256=hashlib.sha256(silver_content).hexdigest(),
        size_bytes=len(silver_content),
    )
    silver = CatalogOntology(
        name="silver",
        description="Silver ontology",
        path=Path("ontology/silver"),
        files=(silver_file,),
        bundle_sha256="0" * 64,
    )
    silver = silver.model_copy(update={"bundle_sha256": ontology_bundle_sha256(silver)})
    (repository / "geas.yaml").write_text(
        RepositoryCatalog(ontologies=(gold, silver)).model_dump_json(indent=2)
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "add",
            "geas.yaml",
            "ontology/silver/build.yaml",
        ),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(repository), "commit", "--amend", "--no-edit"),
        text=True,
        capture_output=True,
        check=True,
    )
    commit = subprocess.run(
        ("git", "-C", str(repository), "rev-parse", "HEAD"),
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()

    gold_skill_root = repository / ".agents" / "skills" / "gold"
    gold_manifest = SkillManifest.model_validate_json(
        (gold_skill_root / "geas-skill.json").read_bytes()
    )
    gold_manifest = SkillManifest.model_validate(
        {
            **gold_manifest.model_dump(mode="python"),
            "ontology": gold_manifest.ontology.model_copy(
                update={"commit": commit, "ontology_commit": commit}
            ),
        }
    )
    (gold_skill_root / "geas-skill.json").write_bytes(
        canonical_manifest_bytes(gold_manifest)
    )

    silver_skill_root = repository / ".agents" / "skills" / "silver"
    silver_skill_root.mkdir(parents=True)
    silver_skill_content = b"# Silver research skill\n"
    (silver_skill_root / "SKILL.md").write_bytes(silver_skill_content)
    silver_inventory = (
        SkillFile(
            path="SKILL.md",
            sha256=hashlib.sha256(silver_skill_content).hexdigest(),
        ),
    )
    silver_manifest = SkillManifest(
        format_version=2,
        skill=SkillIdentity(name="silver"),
        ontology=OntologyIdentity(
            name="silver",
            repository_url=REPOSITORY,
            branch="main",
            commit=commit,
            active_ref="refs/heads/main",
            ontology_commit=commit,
            subscription_name="gold",
            catalog_path="geas.yaml",
            ontology_path="ontology/silver",
            bundle_sha256=silver.bundle_sha256,
        ),
        geas=GeasIdentity(
            project_url="https://github.com/Epiphytic/geas",
            version="1.0.0",
            commit=None,
        ),
        projection=ProjectionIdentity(
            snapshot_id="truth:sha256:silver",
            topic_concept_id="concept:silver",
        ),
        artifact=PortableArtifactIdentity(
            role="knowledge-projection",
            content_sha256="e" * 64,
            input_revision="f" * 64,
        ),
        files=silver_inventory,
        snapshot_sha256=snapshot_digest(silver_inventory),
    )
    (silver_skill_root / "geas-skill.json").write_bytes(
        canonical_manifest_bytes(silver_manifest)
    )

    generic_root = repository / ".agents" / "skills" / "geas"
    generic_root.mkdir(parents=True)
    generic_content = b"# Generic Geas skill\n"
    (generic_root / "SKILL.md").write_bytes(generic_content)
    generic_inventory = (
        SkillFile(
            path="SKILL.md",
            sha256=hashlib.sha256(generic_content).hexdigest(),
        ),
    )
    generic_manifest = SkillManifest(
        format_version=1,
        skill=SkillIdentity(name="geas"),
        ontology=OntologyIdentity(
            name="geas",
            repository_url="https://github.com/Epiphytic/geas.git",
            branch="main",
            commit="0" * 40,
        ),
        geas=GeasIdentity(
            project_url="https://github.com/Epiphytic/geas",
            version="1.0.0",
            commit=None,
        ),
        projection=ProjectionIdentity(
            snapshot_id="builtin:geas",
            topic_concept_id="builtin:geas",
        ),
        files=generic_inventory,
        snapshot_sha256=snapshot_digest(generic_inventory),
    )
    (generic_root / "geas-skill.json").write_bytes(
        canonical_manifest_bytes(generic_manifest)
    )

    managed_paths = tuple(
        ManagedPath(
            path=path.relative_to(repository).as_posix(),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            role="manifest" if path.name == "geas-skill.json" else "skill",
        )
        for path in sorted((repository / ".agents" / "skills").rglob("*"))
        if path.is_file()
    )
    request = receipt.request.model_copy(update={"commit_sha256": commit})
    assert receipt.verified is not None
    verified = receipt.verified.model_copy(
        update={
            "commit_sha256": commit,
            "ontology_paths": ("ontology/gold", "ontology/silver"),
            "bundle_sha256": tuple(sorted((gold.bundle_sha256, silver.bundle_sha256))),
        }
    )
    return receipt.model_copy(
        update={
            "request": request,
            "verified": verified,
            "managed_paths": managed_paths,
        }
    )


def test_bootstrap_publication_manifest_requires_complete_receipt_owned_snapshot(
    tmp_path: Path,
) -> None:
    receipt = _bootstrap_skill_receipt(tmp_path)

    manifests = cli._bootstrap_publication_manifests(tmp_path, receipt)

    assert len(manifests) == 1
    assert manifests[0].producer.value == "exported_skill"
    assert manifests[0].receipt_sha256 == receipt.id.rsplit(":", 1)[-1]
    assert tuple(item.path for item in manifests[0].paths) == (
        ".agents/skills/gold/SKILL.md",
        ".agents/skills/gold/geas-skill.json",
    )
    cli._BootstrapPublicationReceiptVerifier(tmp_path, receipt).verify(manifests[0])

    incomplete = receipt.model_copy(update={"managed_paths": receipt.managed_paths[:-1]})
    with pytest.raises(ValueError, match="complete skill snapshot"):
        cli._bootstrap_publication_manifests(tmp_path, incomplete)


def test_bootstrap_publication_manifest_rejects_wrong_catalog_producer_identity(
    tmp_path: Path,
) -> None:
    receipt = _bootstrap_skill_receipt(tmp_path, manifest_commit="c" * 40)

    with pytest.raises(ValueError, match="bootstrap verification identity"):
        cli._bootstrap_publication_manifests(tmp_path, receipt)


def test_bootstrap_publication_manifest_rejects_cross_paired_catalog_identity(
    tmp_path: Path,
) -> None:
    cross_paired = _bootstrap_skill_receipt(
        tmp_path,
        manifest_bundle_sha256="e" * 64,
    )

    with pytest.raises(ValueError, match="exact verified catalog entry"):
        cli._bootstrap_publication_manifests(tmp_path, cross_paired)


def test_bootstrap_publication_manifest_requires_ontology_path_bijection(
    tmp_path: Path,
) -> None:
    receipt = _multi_ontology_bootstrap_receipt(tmp_path)
    silver_manifest_path = (
        tmp_path / ".agents" / "skills" / "silver" / "geas-skill.json"
    )
    silver_manifest = SkillManifest.model_validate_json(silver_manifest_path.read_bytes())
    gold_manifest = SkillManifest.model_validate_json(
        (tmp_path / ".agents" / "skills" / "gold" / "geas-skill.json").read_bytes()
    )
    duplicate = SkillManifest.model_validate(
        {
            **silver_manifest.model_dump(mode="python"),
            "ontology": gold_manifest.ontology,
        }
    )
    silver_manifest_path.write_bytes(canonical_manifest_bytes(duplicate))
    duplicate_digest = hashlib.sha256(silver_manifest_path.read_bytes()).hexdigest()
    receipt = receipt.model_copy(
        update={
            "managed_paths": tuple(
                item.model_copy(update={"sha256": duplicate_digest})
                if item.path == ".agents/skills/silver/geas-skill.json"
                else item
                for item in receipt.managed_paths
            )
        }
    )

    with pytest.raises(ValueError, match="bijection"):
        cli._bootstrap_publication_manifests(tmp_path, receipt)
    assert cli._prior_receipt_publication_targets(receipt, repository=tmp_path) is None


def _git_publication_grant(
    *,
    ref: str = "refs/heads/main",
    capability: Capability = Capability.GIT_DIRECT_PUSH,
) -> CapabilityGrant:
    return CapabilityGrant(
        decision="allow",
        subject=CapabilitySubject(
            repository=REPOSITORY,
            refs=(ref,),
            paths=(
                ".agents/skills/gold/SKILL.md",
                ".agents/skills/gold/geas-skill.json",
            ),
            bundle_sha256="*",
        ),
        capabilities=(capability,),
        resources=CapabilityResources(git_refs=(ref,)),
        expires_at=None,
        created_at=NOW,
        created_via="manual",
    )


def _git_prepublication_grant(
    *,
    ref: str = "refs/heads/main",
    capability: Capability = Capability.GIT_DIRECT_PUSH,
) -> CapabilityGrant:
    return CapabilityGrant(
        decision="allow",
        subject=CapabilitySubject(
            repository=REPOSITORY,
            refs=(ref,),
            paths="*",
            bundle_sha256="*",
        ),
        capabilities=(capability,),
        resources=CapabilityResources(git_refs=(ref,)),
        expires_at=None,
        created_at=NOW,
        created_via="manual",
    )


def _publication_args(config_path: Path, *, message: str = "refresh gold") -> object:
    return SimpleNamespace(
        geas_config=config_path,
        geas_profile="default",
        yolo=False,
        direct_push=True,
        publish="pull-request",
        message=message,
    )


def test_final_publication_authority_uses_each_manifest_own_bundle_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    receipt = _multi_ontology_bootstrap_receipt(repository)
    config_path = tmp_path / "config.yaml"
    UserConfigManager(config_path).replace(
        GeasUserConfig(version=2, profiles={"default": GeasProfile(ontology_git=None)}),
        upgrade_version=True,
    )
    observed: list[CapabilityRequest] = []

    class Evaluator:
        def evaluate(self, request: CapabilityRequest) -> CapabilityDecision:
            observed.append(request)
            return CapabilityDecision(
                request=request,
                decision="allow",
                effective_capabilities=request.capabilities,
                reason="fixture allow",
                evaluator_version="fixture/1",
                decided_at=NOW,
            )

    publisher_arguments: dict[str, object] = {}

    class Publisher:
        def __init__(self, **kwargs: object) -> None:
            publisher_arguments.update(kwargs)

        def publish(self, request: object) -> PublishResult:
            return PublishResult(
                request_id=request.id,
                published=True,
                reason="fixture published",
                completed_at=NOW,
            )

    monkeypatch.setattr(
        cli,
        "_selected_capability_evaluator",
        lambda *_args, **_kwargs: Evaluator(),
    )
    monkeypatch.setattr(cli, "GitRepositoryPublisher", Publisher)

    cli._publish_repository_receipt(
        _publication_args(config_path),
        receipt.request,
        receipt,
    )

    gold = SkillManifest.model_validate_json(
        (repository / ".agents" / "skills" / "gold" / "geas-skill.json").read_bytes()
    )
    silver = SkillManifest.model_validate_json(
        (repository / ".agents" / "skills" / "silver" / "geas-skill.json").read_bytes()
    )
    by_path = {request.path: request.bundle_sha256 for request in observed}
    assert by_path
    assert {
        digest
        for path, digest in by_path.items()
        if path.startswith(".agents/skills/geas/")
    } == {None}
    assert {
        digest
        for path, digest in by_path.items()
        if path.startswith(".agents/skills/gold/")
    } == {gold.ontology.bundle_sha256}
    assert {
        digest
        for path, digest in by_path.items()
        if path.startswith(".agents/skills/silver/")
    } == {silver.ontology.bundle_sha256}
    assert publisher_arguments["capability_decision"]


def test_one_ontology_cannot_publish_under_a_sibling_bundle_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    receipt = _multi_ontology_bootstrap_receipt(repository)
    manifests = cli._bootstrap_publication_manifests(repository, receipt)
    gold = SkillManifest.model_validate_json(
        (repository / ".agents" / "skills" / "gold" / "geas-skill.json").read_bytes()
    )
    silver = SkillManifest.model_validate_json(
        (repository / ".agents" / "skills" / "silver" / "geas-skill.json").read_bytes()
    )
    grants = tuple(
        CapabilityGrant(
            decision="allow",
            subject=CapabilitySubject(
                repository=REPOSITORY,
                refs=("refs/heads/main",),
                paths=(item.path,),
                bundle_sha256=(
                    (silver.ontology.bundle_sha256,)
                    if item.path.startswith(".agents/skills/gold/")
                    else (gold.ontology.bundle_sha256,)
                    if item.path.startswith(".agents/skills/silver/")
                    else "*"
                ),
            ),
            capabilities=(Capability.GIT_DIRECT_PUSH,),
            resources=CapabilityResources(git_refs=("refs/heads/main",)),
            expires_at=None,
            created_at=NOW,
            created_via="manual",
        )
        for manifest in manifests
        for item in manifest.paths
    )
    config_path = tmp_path / "config.yaml"
    UserConfigManager(config_path).replace(
        GeasUserConfig(
            version=2,
            profiles={
                "default": GeasProfile(
                    ontology_git=None,
                    capability_grants=grants,
                )
            },
        ),
        upgrade_version=True,
    )
    monkeypatch.setattr(
        cli,
        "GitRepositoryPublisher",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("publisher constructed through a sibling bundle grant")
        ),
    )

    with pytest.raises(PermissionError, match=r"\.agents/skills/(?:gold|silver)/"):
        cli._publish_repository_receipt(
            _publication_args(config_path),
            receipt.request,
            receipt,
        )


def test_first_repository_publication_requires_exact_root_local_wildcard_scope(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    UserConfigManager(config_path).replace(
        GeasUserConfig(
            version=2,
            profiles={
                "default": GeasProfile(
                    ontology_git=None,
                    capability_grants=(_git_prepublication_grant(),),
                )
            },
        ),
        upgrade_version=True,
    )
    request = _bootstrap_skill_receipt(tmp_path / "repository").request

    cli._preauthorize_repository_publication(_publication_args(config_path), request)

    changed_ref = request.model_copy(update={"ref": "refs/heads/review"})
    with pytest.raises(PermissionError, match="exact root-local git.direct_push"):
        cli._preauthorize_repository_publication(
            _publication_args(config_path),
            changed_ref,
        )


def test_prior_receipt_publication_scope_requires_every_complete_path(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    generic = ".agents/skills/geas/SKILL.md"
    ontology = ".agents/skills/gold/SKILL.md"
    scope = cli._RepositoryPublicationScope(
        repository=REPOSITORY,
        ref="refs/heads/main",
        publication_targets=((generic, None), (ontology, "a" * 64)),
    )
    partial = CapabilityGrant(
        decision="allow",
        subject=CapabilitySubject(
            repository=REPOSITORY,
            refs=("refs/heads/main",),
            paths=(ontology,),
            bundle_sha256="*",
        ),
        capabilities=(Capability.GIT_DIRECT_PUSH,),
        resources=CapabilityResources(git_refs=("refs/heads/main",)),
        expires_at=None,
        created_at=NOW,
        created_via="manual",
    )
    UserConfigManager(config_path).replace(
        GeasUserConfig(
            version=2,
            profiles={
                "default": GeasProfile(
                    ontology_git=None,
                    capability_grants=(partial,),
                )
            },
        ),
        upgrade_version=True,
    )

    with pytest.raises(PermissionError, match=generic):
        cli._preauthorize_repository_publication(
            _publication_args(config_path),
            scope,
        )

    complete = partial.model_copy(
        update={
            "subject": partial.subject.model_copy(
                update={"paths": (generic, ontology)}
            )
        }
    )
    UserConfigManager(config_path).replace(
        GeasUserConfig(
            version=2,
            profiles={
                "default": GeasProfile(
                    ontology_git=None,
                    capability_grants=(complete,),
                )
            },
        ),
        upgrade_version=True,
    )

    cli._preauthorize_repository_publication(_publication_args(config_path), scope)


def test_repository_publication_invokes_one_publisher_with_operator_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    receipt = _bootstrap_skill_receipt(repository)
    subprocess.run(
        ("git", "init", "--initial-branch=main", str(repository)),
        text=True,
        capture_output=True,
        check=True,
    )
    config_path = tmp_path / "config.yaml"
    UserConfigManager(config_path).replace(
        GeasUserConfig(
            version=2,
            profiles={
                "default": GeasProfile(
                    ontology_git=None,
                    capability_grants=(_git_publication_grant(),),
                )
            },
        ),
        upgrade_version=True,
    )
    events: list[object] = []

    class Publisher:
        def __init__(self, **values: object) -> None:
            events.append(("construct", values))

        def publish(self, request: object) -> PublishResult:
            events.append(("publish", request))
            return PublishResult(
                request_id=request.id,
                published=True,
                branch="main",
                commit_sha256="e" * 40,
                reason="direct-push-completed",
                completed_at=NOW,
            )

    monkeypatch.setattr(cli, "GitRepositoryPublisher", Publisher)
    monkeypatch.setattr(cli, "utc_now", lambda: NOW)

    payload = cli._publish_repository_receipt(
        _publication_args(config_path),
        receipt.request,
        receipt,
    )

    assert len(events) == 2
    constructor = events[0][1]
    assert constructor["repository"] == repository.resolve()
    assert constructor["direct_push"] is True
    assert constructor["forge"] is None
    publish_request = events[1][1]
    assert publish_request.mode is PublishMode.DIRECT_PUSH
    assert publish_request.message == "refresh gold"
    assert payload["publication"]["published"] is True


def test_repository_publication_rejects_a_receipt_for_another_exact_request(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    receipt = _bootstrap_skill_receipt(repository)
    changed = receipt.request.model_copy(
        update={"repository": "https://github.com/example/other"}
    )
    args = SimpleNamespace(direct_push=False, publish="none")

    with pytest.raises(ValueError, match="does not match the exact bootstrap request"):
        cli._publish_repository_receipt(args, changed, receipt)


def test_repository_publication_accepts_an_exact_verified_linked_worktree(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    subprocess.run(
        ("git", "init", "--initial-branch=main", str(repository)),
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.name", "Geas Test"),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "config",
            "user.email",
            "geas@example.invalid",
        ),
        check=True,
    )
    (repository / "README.md").write_text("fixture\n")
    subprocess.run(("git", "-C", str(repository), "add", "README.md"), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "commit", "-m", "fixture"),
        text=True,
        capture_output=True,
        check=True,
    )
    linked = tmp_path / "linked"
    subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            "--detach",
            str(linked),
            "HEAD",
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    request = RepositoryBootstrapRequest(
        name="gold",
        repository=REPOSITORY,
        ref="refs/heads/main",
        catalog="geas.yaml",
        commit_sha256="b" * 40,
        current_worktree=linked.resolve(),
    )

    assert (linked / ".git").is_file()
    assert cli._repository_managed_root(SimpleNamespace(), request) == linked.resolve()


def _source_grant(repository: str) -> CapabilityGrant:
    return CapabilityGrant(
        decision="allow",
        subject=CapabilitySubject(
            repository=repository,
            refs=("refs/heads/main",),
            paths=("ontology/gold",),
            bundle_sha256=("a" * 64,),
        ),
        capabilities=(Capability.SOURCE_FETCH,),
        resources=CapabilityResources(
            hosts=("issuer.example",),
            path_prefixes=("/reports/",),
            connectors=("source:direct-url",),
        ),
        expires_at=None,
        created_at=NOW,
        created_via="manual",
    )


def test_selected_profile_evaluator_uses_v2_grants_and_verified_catalog_manifest(
    tmp_path: Path,
) -> None:
    selected_repository = "https://github.com/example/gold"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        GeasUserConfig(
            version=2,
            default_profile="default",
            profiles={
                "default": GeasProfile(
                    ontology_git=None,
                    secret_sources=(),
                    capability_grants=(_source_grant("https://github.com/example/other"),),
                ),
                "research": GeasProfile(
                    ontology_git=None,
                    secret_sources=(),
                    capability_grants=(_source_grant(selected_repository),),
                ),
            },
        ).explicit_yaml()
    )
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    catalog = ResolvedRepositoryCatalog(
        repository_root=repository_root,
        discovery_start=repository_root,
        repository_identity=selected_repository,
        identity_kind="remote",
        active_ref="refs/heads/main",
        commit="b" * 40,
        delegation_manifest_path=repository_root / "geas-delegations.yaml",
        delegation_manifest_sha256="c" * 64,
        delegation_manifest_size_bytes=0,
        delegation_manifest=DelegationManifest(),
    )
    args = SimpleNamespace(
        geas_config=config_path,
        geas_profile="research",
        yolo=False,
    )

    evaluator = cli._selected_capability_evaluator(
        args,
        catalogs=(catalog,),
        clock=lambda: NOW,
    )
    request = CapabilityRequest(
        authority_repository=selected_repository,
        target_repository=selected_repository,
        capabilities=(Capability.SOURCE_FETCH,),
        ref="refs/heads/main",
        path="ontology/gold",
        bundle_sha256="a" * 64,
        connector="source:direct-url",
        host="issuer.example",
        target="https://issuer.example/reports/latest.pdf",
        requested_at=NOW,
    )

    assert evaluator.evaluate(request).allowed
    assert tuple(evaluator.manifests) == (selected_repository,)


def _intent(kind: DiscoveryKind, locator: str) -> SourceIntent:
    from urllib.parse import urlsplit

    parsed = urlsplit(locator)
    assert parsed.hostname is not None
    return SourceIntent(
        id=f"source-{kind.value.replace('_', '-')}",
        role="test_source",
        discovery=SourceDiscovery(kind=kind, locator=locator),
        allowed_hosts=(parsed.hostname,),
        allowed_path_prefixes=("/",),
        accepted_media_types=("text/plain",),
        refresh=SourceRefreshPolicy(interval_seconds=60, max_items=2, max_depth=1),
        required=True,
        priority=1,
        associations=SourceAssociations(),
        temporal=SourceTemporalPolicy(field="observed_at", retention="append_only"),
        created_at=NOW,
    )


@pytest.mark.parametrize(
    ("kind", "locator", "connector"),
    (
        (DiscoveryKind.DIRECT_URL, "https://issuer.example/report.txt", "source:direct-url"),
        (DiscoveryKind.RSS_ATOM, "https://issuer.example/feed.xml", "source:feed"),
        (DiscoveryKind.SITEMAP, "https://issuer.example/sitemap.xml", "source:sitemap"),
        (DiscoveryKind.HTTPS_HTML, "https://issuer.example/news/", "source:https-html"),
        (DiscoveryKind.MOJEEK, "https://www.mojeek.com/search/test", "source:mojeek"),
        (
            DiscoveryKind.GITHUB_REPOSITORY,
            "https://github.com/example/gold",
            "source:github-repository",
        ),
    ),
)
def test_source_capability_factory_supplies_complete_canonical_selectors(
    kind: DiscoveryKind,
    locator: str,
    connector: str,
) -> None:
    repository = "https://github.com/example/gold"
    authority = SourceAuthorityContext(
        authority_repository=repository,
        target_repository=repository,
        ref="refs/heads/main",
        path="ontology/gold",
    )
    factory = cli._SourceCapabilityRequestFactory(
        authority=authority,
        ontology_bundle_sha256="a" * 64,
        clock=lambda: NOW,
    )
    intent = _intent(kind, locator)
    candidate = SourceCandidate(
        intent_id=intent.id,
        locator=locator,
        discovered_at=NOW,
    )

    coordinator_request = factory(
        intent,
        candidate,
        (Capability.SOURCE_FETCH,),
        NOW,
    )
    adapter_request = factory.for_adapter(intent, locator, Capability.SOURCE_FETCH)

    for request in (coordinator_request, adapter_request):
        assert request.connector == connector
        assert request.host == request.target.split("/", 3)[2]
        assert request.target == locator
        assert request.requested_at == NOW


def test_source_capability_factory_preserves_exact_github_commit_query() -> None:
    repository = "https://github.com/example/gold"
    intent = _intent(DiscoveryKind.GITHUB_REPOSITORY, repository)
    target = (
        "https://api.github.com/repos/example/gold/readme?ref=" + "b" * 40
    )
    factory = cli._SourceCapabilityRequestFactory(
        authority=SourceAuthorityContext(
            authority_repository=repository,
            target_repository=repository,
            ref="refs/heads/main",
            path="ontology/gold",
        ),
        ontology_bundle_sha256="a" * 64,
        clock=lambda: NOW,
    )

    request = factory.for_adapter(intent, target, Capability.SOURCE_FETCH)

    assert request.connector == "source:github-repository"
    assert request.host == "api.github.com"
    assert request.target == target


def test_source_capability_factory_binds_external_model_selectors() -> None:
    repository = "https://github.com/example/gold"
    intent = _intent(DiscoveryKind.DIRECT_URL, "https://issuer.example/report.txt")
    candidate = SourceCandidate(
        intent_id=intent.id,
        locator=intent.discovery.locator,
        discovered_at=NOW,
    )
    factory = cli._SourceCapabilityRequestFactory(
        authority=SourceAuthorityContext(
            authority_repository=repository,
            target_repository=repository,
            ref="refs/heads/main",
            path="ontology/gold",
        ),
        ontology_bundle_sha256="a" * 64,
        model_provider="trusted-provider",
        model_name="trusted-model",
        model_data_class="public",
        clock=lambda: NOW,
    )

    request = factory(intent, candidate, (Capability.MODEL_EXTERNAL,), NOW)

    assert (request.provider, request.model, request.data_class) == (
        "trusted-provider",
        "trusted-model",
        "public",
    )


def test_source_adapter_router_keeps_discovery_fetch_and_payload_on_exact_adapter() -> None:
    events: list[tuple[str, str]] = []
    fetch_receipt = SimpleNamespace(phase="fetched")
    payload = SimpleNamespace(content=b"body")

    class Adapter:
        version = "1"
        max_discovery_requests = 1
        max_fetch_requests = 2
        last_discovery_request_count = 1

        def __init__(self, kind: DiscoveryKind) -> None:
            self.kind = kind
            self.adapter_id = f"source:{kind.value}"

        def discover(self, intent: SourceIntent) -> tuple[SourceCandidate, ...]:
            events.append(("discover", self.kind.value))
            return (
                SourceCandidate(
                    intent_id=intent.id,
                    locator=intent.discovery.locator,
                    discovered_at=NOW,
                ),
            )

        def fetch(self, candidate: SourceCandidate, *, prior: object) -> object:
            del prior
            events.append(("fetch", self.kind.value))
            assert candidate.intent_id == f"source-{self.kind.value.replace('_', '-')}"
            return fetch_receipt

        def payload(self, candidate: SourceCandidate, checkpoint: object) -> object:
            assert checkpoint is fetch_receipt
            events.append(("payload", self.kind.value))
            return payload

    adapters = {kind: Adapter(kind) for kind in DiscoveryKind}
    router = cli._SourceAdapterRouter(adapters)
    intent = _intent(DiscoveryKind.RSS_ATOM, "https://issuer.example/feed.xml")

    router.select(intent)
    assert router.adapter_id == "source:rss_atom"
    assert router.version == "1"
    assert router.max_discovery_requests == 1
    assert router.max_fetch_requests == 2
    candidates = router.discover(intent)
    checkpoint = router.fetch(candidates[0], prior=None)
    assert router.payload(candidates[0], checkpoint) is payload
    assert events == [
        ("discover", "rss_atom"),
        ("fetch", "rss_atom"),
        ("payload", "rss_atom"),
    ]


def test_source_adapter_router_maps_real_fetch_requested_and_final_urls() -> None:
    final_url = "https://issuer.example/canonical/report.txt"

    class Adapter:
        adapter_id = "source:direct-url"
        version = "1"
        max_discovery_requests = 0
        max_fetch_requests = 2
        last_discovery_request_count = 0

        def __init__(self) -> None:
            self.last_fetch = {}

        def discover(self, intent: SourceIntent) -> tuple[SourceCandidate, ...]:
            return (
                SourceCandidate(
                    intent_id=intent.id,
                    locator=intent.discovery.locator,
                    discovered_at=NOW,
                ),
            )

        def fetch(self, candidate: SourceCandidate, *, prior: object) -> object:
            del prior
            self.last_fetch[candidate.id] = SourceFetchResult(
                requested_url=candidate.locator,
                final_url=final_url,
                redirect_chain=(final_url,),
                status=200,
                media_type="text/plain",
                content=b"report\n",
            )
            return SimpleNamespace(recorded_at=NOW)

    adapter = Adapter()
    router = cli._SourceAdapterRouter(dict.fromkeys(DiscoveryKind, adapter))
    intent = _intent(DiscoveryKind.DIRECT_URL, "https://issuer.example/report.txt")
    (candidate,) = router.discover(intent)
    checkpoint = router.fetch(candidate, prior=None)

    payload = router.payload(candidate, checkpoint)

    assert payload.source_uri == final_url
    assert payload.connector_id == "source:direct-url"
    assert payload.media_type == "text/plain"


def test_mojeek_search_failure_conservatively_charges_its_complete_request_bound() -> None:
    class Connector:
        manifest = SimpleNamespace(max_results=200, max_pages=5)

        def discover(self, _request: object) -> object:
            raise RuntimeError("transport failed before yielding a page")

    search = cli._MojeekIntentSearch(Connector(), max_requests_per_run=3)
    intent = _intent(
        DiscoveryKind.MOJEEK,
        "https://api.mojeek.com/search",
    ).model_copy(
        update={
            "refresh": SourceRefreshPolicy(
                interval_seconds=60,
                max_items=120,
                max_depth=1,
            )
        }
    )

    with pytest.raises(RuntimeError) as raised:
        search(intent)

    assert raised.value.request_count == 3
    assert search.last_request_count == 3


def test_legacy_ontology_sync_push_only_pulls_and_never_reaches_old_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[object] = []

    class Subscriptions:
        def sync(self, names: tuple[str, ...], *, pull: bool, push: bool) -> tuple[object, ...]:
            events.append(("sync", names, pull, push))
            return ()

    manager = SimpleNamespace(
        path=tmp_path / "config.yaml",
        root=tmp_path,
        load_or_create=lambda: GeasUserConfig.default(),
    )
    monkeypatch.setattr(cli, "recover_managed_removals", lambda _manager: None)
    monkeypatch.setattr(cli, "_resolve_cli_config_paths", lambda _args: None)
    monkeypatch.setattr(cli, "_user_config_manager", lambda _args: manager)
    monkeypatch.setattr(
        cli,
        "_subscription_service",
        lambda _args, *, manager, profile_name: Subscriptions(),
    )
    monkeypatch.setattr(
        cli,
        "_publish_ontology_sync",
        lambda *_args, **_kwargs: events.append("publisher"),
    )

    _run_main(
        monkeypatch,
        "--geas-config",
        str(tmp_path / "config.yaml"),
        "ontology-sync",
        "gold",
        "--push",
    )

    output = capsys.readouterr()
    assert events == [("sync", ("gold",), True, False)]
    assert json.loads(output.out)["publication"] is None
    assert "does not authorize a remote write" in output.err


def test_ontology_sync_direct_push_denial_precedes_subscription_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SimpleNamespace(
        path=tmp_path / "config.yaml",
        root=tmp_path,
        load_or_create=lambda: (_ for _ in ()).throw(
            AssertionError("configuration was created after publication denial")
        ),
    )
    monkeypatch.setattr(
        cli,
        "recover_managed_removals",
        lambda _manager: (_ for _ in ()).throw(
            AssertionError("filesystem recovery ran after publication denial")
        ),
    )
    monkeypatch.setattr(cli, "_resolve_cli_config_paths", lambda _args: None)
    monkeypatch.setattr(cli, "_user_config_manager", lambda _args: manager)
    monkeypatch.setattr(
        cli,
        "_preauthorize_ontology_sync_publication",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )
    monkeypatch.setattr(
        cli,
        "_subscription_service",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("subscription service constructed after publication denial")
        ),
    )

    with pytest.raises(PermissionError, match="denied"):
        _run_main(
            monkeypatch,
            "--geas-config",
            str(tmp_path / "config.yaml"),
            "ontology-sync",
            "gold",
            "--push",
            "--direct-push",
        )


def test_ontology_sync_forwards_message_only_through_publisher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    plan = object()
    sync_receipts = ({"name": "gold"},)

    class Subscriptions:
        def sync(self, names: tuple[str, ...], *, pull: bool, push: bool) -> tuple[object, ...]:
            events.append(("sync", names, pull, push))
            return sync_receipts

    manager = SimpleNamespace(
        path=tmp_path / "config.yaml",
        root=tmp_path,
        load_or_create=lambda: GeasUserConfig.default(),
    )
    monkeypatch.setattr(cli, "recover_managed_removals", lambda _manager: None)
    monkeypatch.setattr(cli, "_resolve_cli_config_paths", lambda _args: None)
    monkeypatch.setattr(cli, "_user_config_manager", lambda _args: manager)
    monkeypatch.setattr(
        cli,
        "_preauthorize_ontology_sync_publication",
        lambda _args, *, manager, profile_name, names: events.append(
            ("preauthorize", profile_name, names)
        )
        or plan,
    )
    monkeypatch.setattr(
        cli,
        "_subscription_service",
        lambda _args, *, manager, profile_name: Subscriptions(),
    )
    monkeypatch.setattr(
        cli,
        "_publish_ontology_sync",
        lambda supplied, receipts, *, message: events.append(
            ("publisher", supplied, receipts, message)
        )
        or {"published": True},
    )

    _run_main(
        monkeypatch,
        "--geas-config",
        str(tmp_path / "config.yaml"),
        "ontology-sync",
        "gold",
        "--push",
        "--direct-push",
        "--message",
        "refresh gold",
    )

    assert events == [
        ("preauthorize", "default", ("gold",)),
        ("sync", ("gold",), False, False),
        ("publisher", plan, sync_receipts, "refresh gold"),
    ]


def test_ontology_init_legacy_push_on_update_never_calls_old_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manager = UserConfigManager(tmp_path / "config" / "config.yaml")
    manager.root.mkdir(parents=True)
    manager.replace(
        GeasUserConfig(
            profiles={
                "default": GeasProfile(
                    ontology_git=OntologyGitConfig(
                        url="https://github.com/example/ontologies.git",
                        push_on_update=True,
                    ),
                    secret_sources=(),
                )
            }
        )
    )

    class Repository:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def push(self, **_kwargs: object) -> object:
            raise AssertionError("legacy push_on_update reached old Git push")

    monkeypatch.setattr(cli, "OntologyRepositoryManager", Repository)
    _run_main(
        monkeypatch,
        "--geas-config",
        str(manager.path),
        "ontology-init",
        "--topic",
        "Gold ontology",
        "--concept-id",
        "concept:gold",
        "--no-pull",
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["push"] is None
    assert (manager.root / "ontologies" / "gold" / "build.yaml").is_file()


def test_ontology_init_direct_push_denial_precedes_configuration_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = UserConfigManager(tmp_path / "config" / "config.yaml")
    manager.root.mkdir(parents=True)
    manager.replace(
        GeasUserConfig(
            profiles={
                "default": GeasProfile(
                    ontology_git=OntologyGitConfig(
                        url="https://github.com/example/ontologies.git"
                    ),
                    secret_sources=(),
                )
            }
        )
    )
    monkeypatch.setattr(
        cli,
        "_preauthorize_ontology_init_publication",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("denied")),
        raising=False,
    )
    monkeypatch.setattr(
        cli,
        "recover_managed_removals",
        lambda _manager: (_ for _ in ()).throw(
            AssertionError("filesystem recovery ran after publication denial")
        ),
    )

    with pytest.raises(PermissionError, match="denied"):
        _run_main(
            monkeypatch,
            "--geas-config",
            str(manager.path),
            "ontology-init",
            "--topic",
            "Gold ontology",
            "--concept-id",
            "concept:gold",
            "--no-pull",
            "--push",
            "--direct-push",
        )

    assert not (manager.root / "ontologies" / "gold").exists()


@pytest.mark.parametrize(
    ("discovery_kind", "locator", "connector"),
    (
        (
            DiscoveryKind.DIRECT_URL,
            "https://issuer.example/report.txt",
            "source:direct-url",
        ),
        (
            DiscoveryKind.MOJEEK,
            "https://api.mojeek.com/search",
            "source:mojeek",
        ),
    ),
)
def test_ontology_update_factory_only_loads_mojeek_for_selected_intents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    discovery_kind: DiscoveryKind,
    locator: str,
    connector: str,
) -> None:
    repository = tmp_path / "repository"
    ontology = repository / "ontology" / "gold"
    ontology.mkdir(parents=True)
    source_intent = _intent(
        discovery_kind,
        locator,
    )
    build = OntologyBuildConfig.from_defaults(
        GeasUserConfig.default().ontology_defaults,
        version=1,
        topic="Gold",
        topic_concept_id="concept:gold",
        output_directory=Path("ontology/gold/generated"),
        source_intent=(source_intent,),
    )
    (ontology / "build.yaml").write_text(build.explicit_yaml())
    (ontology / "library.yaml").write_text(
        SourceLibraryManifest(
            version=1,
            id="library:gold",
            title="Gold sources",
            include_all_parsed_sources=True,
        ).explicit_yaml()
    )
    repository_identity = "https://github.com/example/gold"
    commit = "b" * 40
    bundle = "a" * 64
    selection = OntologySelection(
        name="gold",
        source="subscription:gold",
        source_kind="subscription",
        ontology_directory=ontology.resolve(),
        verified_ontology_directory=ontology.resolve(),
        repository_identity=repository_identity,
        repository_root=repository.resolve(),
        verified_repository_root=repository.resolve(),
        identity_kind="remote",
        active_ref="refs/heads/main",
        commit=commit,
        catalog_path=repository / "geas.yaml",
        repository_path=Path("ontology/gold"),
        bundle_sha256=bundle,
        files=None,
        trust_status="trusted",
        authorization="rule",
        subscription_name="gold",
        subscription=None,
    )
    catalog = ResolvedRepositoryCatalog(
        repository_root=repository.resolve(),
        discovery_start=repository.resolve(),
        repository_identity=repository_identity,
        identity_kind="remote",
        active_ref="refs/heads/main",
        commit=commit,
    )
    grant = CapabilityGrant(
        decision="allow",
        subject=CapabilitySubject(
            repository=repository_identity,
            refs=("refs/heads/main",),
            paths=("ontology/gold",),
            bundle_sha256=(bundle,),
        ),
        capabilities=(
            Capability.SOURCE_ARCHIVE,
            Capability.SOURCE_DISCOVER,
            Capability.SOURCE_EXTRACT,
            Capability.SOURCE_FETCH,
        ),
        resources=CapabilityResources(
            hosts=source_intent.allowed_hosts,
            path_prefixes=("/",),
            connectors=(connector,),
        ),
        expires_at=None,
        created_at=NOW,
        created_via="manual",
    )
    config_path = tmp_path / "config" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        GeasUserConfig(
            version=2,
            profiles={
                "default": GeasProfile(
                    ontology_git=None,
                    secret_sources=(),
                    capability_grants=(grant,),
                )
            },
        ).explicit_yaml()
    )
    monkeypatch.setattr(cli, "_catalog_selection", lambda *_args, **_kwargs: selection)
    monkeypatch.setattr(
        cli,
        "resolve_repository_catalog",
        lambda *_args, **_kwargs: catalog,
    )
    loaded_secret_names: list[frozenset[str]] = []
    monkeypatch.setattr(
        cli,
        "_load_allowed_secrets",
        lambda _args, *, allowed_names: loaded_secret_names.append(allowed_names)
        or frozenset(),
    )
    root = Path(__file__).resolve().parents[1]
    research_policy = tmp_path / "missing-research-policy.yaml"
    if discovery_kind is DiscoveryKind.MOJEEK:
        research_policy = tmp_path / "research-policy.yaml"
        research_policy.write_text(
            (root / "config" / "research-policy.yaml")
            .read_text()
            .replace("MOJEEK_API_KEY", "TRUSTED_MOJEEK_KEY")
        )
    args = SimpleNamespace(
        name="gold",
        root=tmp_path / "runtime",
        geas_config=config_path,
        geas_profile="default",
        yolo=False,
        providers=root / "config" / "providers.toml",
        model_policy=root / "config" / "model-policy.yaml",
        budget_policy=root / "config" / "budget-policy.yaml",
        policy=root / "config" / "source-policy.yaml",
        research_policy=research_policy,
        env_file=None,
    )

    service = cli._ontology_update_service(args)

    assert isinstance(service, OntologyUpdateService)
    assert service.configs == {"gold": build}
    coordinator = service.coordinators["gold"]
    assert coordinator.authority == SourceAuthorityContext(
        authority_repository=repository_identity,
        target_repository=repository_identity,
        ref="refs/heads/main",
        path="ontology/gold",
    )
    assert discovery_kind in coordinator.adapter.adapters
    if discovery_kind is DiscoveryKind.MOJEEK:
        mojeek = coordinator.adapter.adapters[DiscoveryKind.MOJEEK]
        assert mojeek.search.connector.transport.api_key_env == "TRUSTED_MOJEEK_KEY"
        assert mojeek.max_discovery_requests == min(
            ResearchPolicy.from_yaml(research_policy)
            .provider("connector:mojeek")
            .max_requests_per_run,
            mojeek.search.connector.manifest.max_pages,
        )
    else:
        assert DiscoveryKind.MOJEEK not in coordinator.adapter.adapters
    assert coordinator.ontology_bundle_sha256 == bundle
    assert coordinator.library_manifest is not None
    assert coordinator.library_database == (
        tmp_path / "runtime" / "ontologies" / "gold" / "library.sqlite"
    )
    assert coordinator.extraction is not None
    assert loaded_secret_names == [
        (
            frozenset({"TRUSTED_MOJEEK_KEY"})
            if discovery_kind is DiscoveryKind.MOJEEK
            else frozenset()
        )
    ]
