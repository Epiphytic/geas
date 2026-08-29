from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from research_agent.ontology_resolution import (
    resolve_ontology_catalog,
    select_ontology,
)
from research_agent.ontology_subscriptions import OntologySubscription
from research_agent.ontology_trust import InstalledOntologySnapshot, TrustRule
from research_agent.paths import resolve_selected_ontology_config
from research_agent.repository_catalog import (
    CatalogFile,
    CatalogOntology,
    ontology_bundle_sha256,
    refresh_catalog,
)
from research_agent.user_config import GeasProfile, GeasUserConfig, UserConfigManager


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
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


def _repository(root: Path, *names: str, nested: Path | None = None) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "--initial-branch=main")
    catalog_root = root if nested is None else root / nested
    catalog_root.mkdir(parents=True, exist_ok=True)
    ontologies = []
    for name in names:
        ontology = catalog_root / "ontology" / name
        ontology.mkdir(parents=True)
        (ontology / "payload.txt").write_text(f"{name}\n")
        ontologies.append(
            {
                "name": name,
                "description": f"Ontology {name}",
                "path": f"ontology/{name}",
                "files": [
                    {
                        "path": "payload.txt",
                        "sha256": "0" * 64,
                        "size_bytes": 0,
                    }
                ],
                "bundle_sha256": "0" * 64,
            }
        )
    catalog = catalog_root / "geas.yaml"
    catalog.write_text(yaml.safe_dump({"version": 1, "ontologies": ontologies}, sort_keys=False))
    refresh_catalog(catalog)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "catalog")
    return catalog_root


def _manager(tmp_path: Path, profile: GeasProfile) -> UserConfigManager:
    manager = UserConfigManager(tmp_path / "config" / "config.yaml")
    manager.root.mkdir(parents=True, exist_ok=True)
    manager.replace(GeasUserConfig(profiles={"default": profile}))
    return manager


def _legacy_ontology(manager: UserConfigManager, name: str) -> Path:
    directory = manager.root / "ontologies" / name
    directory.mkdir(parents=True)
    (directory / "build.yaml").write_text("intentionally: not parsed during resolution\n")
    return directory


def test_repository_catalog_augments_profile_without_shadowing(tmp_path: Path) -> None:
    manager = _manager(tmp_path, GeasProfile(ontology_git=None))
    _legacy_ontology(manager, "profile-only")
    repository = _repository(tmp_path / "repository", "repo-only")

    result = resolve_ontology_catalog(
        user_config=manager.load(),
        manager=manager,
        cwd=repository,
        yolo=False,
        prompt=None,
    )

    assert [item.name for item in result.candidates] == ["profile-only", "repo-only"]
    assert [item.source_kind for item in result.candidates] == [
        "legacy_profile",
        "repository",
    ]


def test_same_name_from_profile_and_repository_is_explicitly_ambiguous(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, GeasProfile(ontology_git=None))
    _legacy_ontology(manager, "shared")
    repository = _repository(tmp_path / "repository", "shared")
    catalog = resolve_ontology_catalog(
        user_config=manager.load(),
        manager=manager,
        cwd=repository,
        yolo=False,
        prompt=None,
    )

    with pytest.raises(ValueError, match="ambiguous ontology 'shared'"):
        select_ontology("shared", catalog=catalog)


def test_untrusted_repository_candidate_is_inert_until_operational_selection(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, GeasProfile(ontology_git=None))
    repository = _repository(tmp_path / "repository", "untrusted")

    catalog = resolve_ontology_catalog(
        user_config=manager.load(),
        manager=manager,
        cwd=repository,
        yolo=False,
        prompt=None,
    )

    assert catalog.candidates[0].trust_status == "untrusted"
    with pytest.raises(ValueError, match="not trusted"):
        select_ontology("untrusted", catalog=catalog)


def test_yolo_authorizes_repository_candidate_without_persisting_configuration(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, GeasProfile(ontology_git=None))
    repository = _repository(tmp_path / "repository", "temporary")
    before = manager.path.read_bytes()

    catalog = resolve_ontology_catalog(
        user_config=manager.load(),
        manager=manager,
        cwd=repository,
        yolo=True,
        prompt=None,
    )

    selection = select_ontology("temporary", catalog=catalog)
    assert selection.authorization == "yolo"
    assert selection.ontology_directory == repository / "ontology" / "temporary"
    assert manager.path.read_bytes() == before


def test_subscription_candidate_preserves_declaring_repository_metadata(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "config" / "subscriptions" / "default" / "research"
    catalog_root = _repository(checkout, "subscribed", nested=Path("catalogs/research"))
    subscription = OntologySubscription(
        url="https://example.invalid/research.git",
        active_ref="refs/heads/main",
        checkout=Path("subscriptions/default/research"),
        catalog=Path("catalogs/research/geas.yaml"),
    )
    trust = TrustRule(
        decision="allow",
        repository=str(checkout.resolve()),
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
        created_via="manual",
    )
    manager = _manager(
        tmp_path,
        GeasProfile(
            ontology_git=None,
            subscriptions={"research": subscription},
            trust_rules=(trust,),
        ),
    )

    catalog = resolve_ontology_catalog(
        user_config=manager.load(),
        manager=manager,
        cwd=tmp_path,
        yolo=False,
        prompt=None,
    )

    candidate = catalog.candidates[0]
    assert candidate.source == "subscription:research"
    assert candidate.subscription_name == "research"
    assert candidate.subscription == subscription
    assert candidate.catalog_path == catalog_root / "geas.yaml"
    assert candidate.repository_identity == str(checkout.resolve())
    assert candidate.active_ref == "refs/heads/main"
    assert candidate.commit == _git(checkout, "rev-parse", "HEAD")
    assert candidate.bundle_sha256
    assert [item.path for item in candidate.files] == [Path("payload.txt")]
    assert candidate.trust_status == "trusted"
    assert select_ontology("subscribed", catalog=catalog).subscription == subscription


def test_selected_conventional_input_must_be_in_verified_inventory(tmp_path: Path) -> None:
    manager = _manager(tmp_path, GeasProfile(ontology_git=None))
    repository = _repository(tmp_path / "repository", "closed-world")
    undeclared = repository / "ontology" / "closed-world" / "build.yaml"
    undeclared.write_text("must: remain inert\n")
    catalog = resolve_ontology_catalog(
        user_config=manager.load(),
        manager=manager,
        cwd=repository,
        yolo=True,
        prompt=None,
    )
    selection = select_ontology("closed-world", catalog=catalog)

    with pytest.raises(ValueError, match="not declared"):
        resolve_selected_ontology_config(
            Path("closed-world"),
            filename="build.yaml",
            selection=selection,
        )


def test_selected_conventional_input_is_rehashed_immediately_before_use(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, GeasProfile(ontology_git=None))
    repository = _repository(tmp_path / "repository", "changed")
    catalog = resolve_ontology_catalog(
        user_config=manager.load(),
        manager=manager,
        cwd=repository,
        yolo=True,
        prompt=None,
    )
    selection = select_ontology("changed", catalog=catalog)
    (selection.ontology_directory / "payload.txt").write_text("changed after selection\n")

    with pytest.raises(ValueError, match="mismatch"):
        resolve_selected_ontology_config(
            Path("changed"),
            filename="payload.txt",
            selection=selection,
        )


def test_installed_snapshot_is_a_trusted_exact_path_candidate(tmp_path: Path) -> None:
    content = b"snapshot\n"
    file = CatalogFile(
        path=Path("payload.txt"),
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )
    entry = CatalogOntology(
        name="installed",
        description="Installed snapshot",
        path=Path("installed"),
        files=(file,),
        bundle_sha256="0" * 64,
    )
    digest = ontology_bundle_sha256(entry)
    snapshot = InstalledOntologySnapshot(
        name=entry.name,
        description=entry.description,
        bundle_sha256=digest,
        path=Path("snapshots/installed") / digest,
        files=entry.files,
    )
    manager = _manager(
        tmp_path,
        GeasProfile(ontology_git=None, installed_ontologies=(snapshot,)),
    )
    directory = manager.root / snapshot.path
    directory.mkdir(parents=True)
    (directory / "payload.txt").write_bytes(content)

    catalog = resolve_ontology_catalog(
        user_config=manager.load(),
        manager=manager,
        cwd=tmp_path,
        yolo=False,
        prompt=None,
    )

    selection = select_ontology("installed", catalog=catalog)
    assert selection.source_kind == "snapshot"
    assert selection.bundle_sha256 == digest
    assert selection.ontology_directory == directory
    assert selection.trust_status == "trusted"


def test_candidate_order_is_name_then_source(tmp_path: Path) -> None:
    manager = _manager(tmp_path, GeasProfile(ontology_git=None))
    _legacy_ontology(manager, "same")
    _legacy_ontology(manager, "zeta")
    repository = _repository(tmp_path / "repository", "alpha", "same")

    catalog = resolve_ontology_catalog(
        user_config=manager.load(),
        manager=manager,
        cwd=repository,
        yolo=False,
        prompt=None,
    )

    assert [(item.name, item.source) for item in catalog.candidates] == [
        ("alpha", f"repository:{repository / 'geas.yaml'}"),
        ("same", "profile:default"),
        ("same", f"repository:{repository / 'geas.yaml'}"),
        ("zeta", "profile:default"),
    ]


@pytest.mark.parametrize(
    "action, expected_status, expected_authorization",
    (("1", "trusted", "interactive"), ("2", "trusted", "interactive"), ("4", "denied", None)),
)
def test_repository_prompt_decisions_flow_through_catalog_resolution(
    tmp_path: Path,
    action: str,
    expected_status: str,
    expected_authorization: str | None,
) -> None:
    manager = _manager(tmp_path, GeasProfile(ontology_git=None))
    repository = _repository(tmp_path / "repository", "prompted")

    class Prompt:
        def choose_action(self, catalog: object) -> str:
            return action

        def select_ontology(self, ontology: object, *, action: str) -> bool:
            return True

    catalog = resolve_ontology_catalog(
        user_config=manager.load(),
        manager=manager,
        cwd=repository,
        yolo=False,
        prompt=Prompt(),
    )

    assert catalog.candidates[0].trust_status == expected_status
    assert catalog.candidates[0].authorization == expected_authorization


def test_snapshot_prompt_selects_installed_copy_not_denied_mutable_source(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, GeasProfile(ontology_git=None))
    repository = _repository(tmp_path / "repository", "installed-by-prompt")

    class Prompt:
        def choose_action(self, catalog: object) -> str:
            return "3"

        def select_ontology(self, ontology: object, *, action: str) -> bool:
            return True

    catalog = resolve_ontology_catalog(
        user_config=manager.load(),
        manager=manager,
        cwd=repository,
        yolo=False,
        prompt=Prompt(),
    )

    selection = select_ontology("installed-by-prompt", catalog=catalog)
    assert selection.source_kind == "snapshot"
    assert selection.authorization == "snapshot"
    assert selection.ontology_directory.is_relative_to(manager.root / "snapshots")
