from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from research_agent.repository_catalog import (
    CatalogFile,
    CatalogOntology,
    RepositoryCatalog,
    discover_catalogs,
    load_catalog,
    refresh_catalog,
    resolve_repository_catalog,
    verify_catalog,
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _bundle_digest(*, name: str, description: str, files: list[dict[str, object]]) -> str:
    payload = {
        "description": description,
        "files": files,
        "format": "geas-ontology-bundle/1",
        "name": name,
    }
    return _sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )


def _entry(
    root: Path,
    name: str = "example",
    path: str = "ontology/example",
    *,
    description: str = "A test ontology.",
    files: dict[str, bytes] | None = None,
) -> dict[str, object]:
    contents = files or {"build.yaml": b"topic: test\n"}
    directory = root / path
    directory.mkdir(parents=True, exist_ok=True)
    for relative, content in contents.items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    inventory = [
        {"path": relative, "sha256": _sha256(content), "size_bytes": len(content)}
        for relative, content in sorted(contents.items())
    ]
    return {
        "name": name,
        "description": description,
        "path": path,
        "files": inventory,
        "bundle_sha256": _bundle_digest(name=name, description=description, files=inventory),
    }


def _write_catalog(path: Path, *entries: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"version": 1, "ontologies": list(entries)}, sort_keys=False))


def _catalog_ontology(root: Path, *, description: str = "A test ontology.") -> dict[str, object]:
    entry = _entry(root, description=description)
    _write_catalog(root / "geas.yaml", entry)
    return entry


def _git(directory: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(directory), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Catalog Test")
    _git(repository, "config", "user.email", "catalog@example.test")
    return repository


def test_catalog_models_forbid_extra_fields_and_invalid_names() -> None:
    """Removing strict validation would accept unsafe catalog authority data."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CatalogFile.model_validate(
            {"path": "build.yaml", "sha256": "0" * 64, "size_bytes": 0, "extra": True}
        )
    with pytest.raises(ValidationError, match="ontology name"):
        CatalogOntology.model_validate(
            {
                "name": ".unsafe",
                "description": "test",
                "path": "ontology/example",
                "files": [],
                "bundle_sha256": "0" * 64,
            }
        )
    with pytest.raises(ValidationError, match="ontology names must be unique"):
        RepositoryCatalog.model_validate(
            {
                "version": 1,
                "ontologies": [
                    {
                        "name": "same",
                        "description": "test",
                        "path": "ontology/a",
                        "files": [{"path": "build.yaml", "sha256": "0" * 64, "size_bytes": 0}],
                        "bundle_sha256": "0" * 64,
                    },
                    {
                        "name": "same",
                        "description": "other",
                        "path": "ontology/b",
                        "files": [{"path": "build.yaml", "sha256": "1" * 64, "size_bytes": 0}],
                        "bundle_sha256": "1" * 64,
                    },
                ],
            }
        )


def test_catalog_digest_is_portable_and_metadata_sensitive(tmp_path: Path) -> None:
    """Dropping description from the digest would accept changed ontology meaning."""
    ontology = _catalog_ontology(tmp_path, description="first")
    verified = verify_catalog(tmp_path / "geas.yaml")
    assert verified[0].bundle_sha256 == ontology["bundle_sha256"]
    moved = tmp_path / "moved"
    shutil.copytree(tmp_path / "ontology", moved / "ontology")
    _write_catalog(moved / "geas.yaml", ontology)
    assert verify_catalog(moved / "geas.yaml")[0].bundle_sha256 == ontology["bundle_sha256"]
    ontology["description"] = "second"
    _write_catalog(moved / "geas.yaml", ontology)
    with pytest.raises(ValueError, match="bundle digest"):
        verify_catalog(moved / "geas.yaml")


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda entry: entry.update({"unknown": True}), "Extra inputs"),
        (lambda entry: entry.update({"path": "/absolute"}), "relative"),
        (lambda entry: entry.update({"path": "ontology/../example"}), "normalized"),
        (
            lambda entry: entry["files"].__setitem__(
                0, {**entry["files"][0], "path": "bad\x00.yaml"}
            ),
            "control",
        ),
        (lambda entry: entry.update({"files": list(reversed(entry["files"]))}), "ascending"),
        (lambda entry: entry.update({"files": [*entry["files"], entry["files"][0]]}), "unique"),
    ],
)
def test_load_catalog_rejects_unsafe_or_noncanonical_manifest_values(
    tmp_path: Path, mutate: object, message: str
) -> None:
    """Relaxing lexical validation would let a manifest escape its declaration root."""
    entry = _entry(tmp_path, files={"build.yaml": b"one", "library.yaml": b"two"})
    mutate(entry)  # type: ignore[operator]
    _write_catalog(tmp_path / "geas.yaml", entry)
    with pytest.raises(ValueError, match=message):
        load_catalog(tmp_path / "geas.yaml")


@pytest.mark.parametrize("kind", ["catalog", "directory", "file"])
def test_verify_catalog_rejects_symlinked_authority_boundaries(tmp_path: Path, kind: str) -> None:
    """Following any symlink would allow a catalog to read outside its repository."""
    entry = _catalog_ontology(tmp_path)
    catalog = tmp_path / "geas.yaml"
    ontology_directory = tmp_path / str(entry["path"])
    if kind == "catalog":
        target = tmp_path / "real-geas.yaml"
        catalog.replace(target)
        catalog.symlink_to(target)
    elif kind == "directory":
        target = tmp_path / "real-ontology"
        ontology_directory.replace(target)
        ontology_directory.symlink_to(target, target_is_directory=True)
    else:
        target = ontology_directory / "real-build.yaml"
        (ontology_directory / "build.yaml").replace(target)
        (ontology_directory / "build.yaml").symlink_to(target)
    with pytest.raises(ValueError, match="symbolic link"):
        verify_catalog(catalog)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda root, entry: (root / str(entry["path"]) / "build.yaml").unlink(), "missing"),
        (
            lambda root, entry: (
                (root / str(entry["path"]) / "build.yaml").unlink(),
                (root / str(entry["path"]) / "build.yaml").mkdir(),
            ),
            "regular",
        ),
        (
            lambda root, entry: (root / str(entry["path"]) / "build.yaml").write_bytes(b"changed"),
            "size|sha256",
        ),
        (lambda root, entry: entry.update({"bundle_sha256": "0" * 64}), "bundle digest"),
    ],
)
def test_verify_catalog_rejects_changed_or_missing_inventory(
    tmp_path: Path, mutate: object, message: str
) -> None:
    """Skipping exact-byte verification would allow changed ontology inputs."""
    entry = _catalog_ontology(tmp_path)
    mutate(tmp_path, entry)  # type: ignore[operator]
    _write_catalog(tmp_path / "geas.yaml", entry)
    with pytest.raises(ValueError, match=message):
        verify_catalog(tmp_path / "geas.yaml")


@pytest.mark.parametrize("reference_key", ["build_path", "library_path", "source_card"])
def test_verify_catalog_requires_known_transitive_yaml_inputs_in_inventory(
    tmp_path: Path, reference_key: str
) -> None:
    """Removing closed-world checks would silently add an undeclared ontology input."""
    entry = _entry(
        tmp_path,
        files={"build.yaml": f"{reference_key}: card.yaml\n".encode()},
    )
    ontology = tmp_path / "ontology/example"
    (ontology / "card.yaml").write_text("id: source-card:test\n")
    _write_catalog(tmp_path / "geas.yaml", entry)
    with pytest.raises(ValueError, match="undeclared"):
        verify_catalog(tmp_path / "geas.yaml")


def test_verify_catalog_rejects_undeclared_workspace_relative_seed_bundle(
    tmp_path: Path,
) -> None:
    """Ignoring seed_bundles would let build.yaml import an unreviewed bundle."""
    entry = _entry(
        tmp_path,
        files={
            "build.yaml": b"seed_bundles:\n  - ontology/example/seeds/seed.yaml\n",
        },
    )
    (tmp_path / "ontology/example/seeds").mkdir()
    (tmp_path / "ontology/example/seeds/seed.yaml").write_text("version: 1\n")
    _write_catalog(tmp_path / "geas.yaml", entry)

    with pytest.raises(ValueError, match="undeclared"):
        verify_catalog(tmp_path / "geas.yaml")


def test_verify_catalog_rejects_undeclared_tracked_seed_bundle_glob(git_repo: Path) -> None:
    """Ignoring seed_bundle_globs would let a tracked bundle bypass the inventory."""
    entry = _entry(
        git_repo,
        files={
            "build.yaml": b"seed_bundle_globs:\n  - ontology/example/seeds/*.yaml\n",
        },
    )
    seed = git_repo / "ontology/example/seeds/seed.yaml"
    seed.parent.mkdir()
    seed.write_text("version: 1\n")
    _write_catalog(git_repo / "geas.yaml", entry)
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "seed glob fixture")

    with pytest.raises(ValueError, match="undeclared"):
        verify_catalog(git_repo / "geas.yaml")


def test_refresh_rehashes_existing_inventory_and_writes_atomically(tmp_path: Path) -> None:
    """Refresh must preserve the old catalog until a complete replacement is ready."""
    _catalog_ontology(tmp_path)
    build = tmp_path / "ontology/example/build.yaml"
    build.write_bytes(b"new bytes\n")
    refreshed = refresh_catalog(tmp_path / "geas.yaml")
    assert refreshed.ontologies[0].files[0].sha256 == _sha256(b"new bytes\n")
    assert load_catalog(tmp_path / "geas.yaml") == refreshed
    assert (
        verify_catalog(tmp_path / "geas.yaml")[0].bundle_sha256
        == refreshed.ontologies[0].bundle_sha256
    )


def test_refresh_only_updates_requested_existing_entries(tmp_path: Path) -> None:
    """A refresh must not introduce an unreviewed authority file or entry."""
    first = _entry(tmp_path, "first", "ontology/first")
    second = _entry(tmp_path, "second", "ontology/second")
    _write_catalog(tmp_path / "geas.yaml", first, second)
    (tmp_path / "ontology/second/build.yaml").write_text("changed")
    refreshed = refresh_catalog(tmp_path / "geas.yaml", names=("first",))
    assert (
        refreshed.ontologies[1]
        == RepositoryCatalog.model_validate({"version": 1, "ontologies": [second]}).ontologies[0]
    )
    with pytest.raises(ValueError, match="unknown ontology"):
        refresh_catalog(tmp_path / "geas.yaml", names=("missing",))


def test_nested_catalogs_merge_complete_entries_from_root_to_cwd(git_repo: Path) -> None:
    """Partial or reverse merging would keep outer fields for an inner declaration."""
    root_entry = _entry(git_repo, "shared", "ontology/root", description="root")
    root_only = _entry(git_repo, "root-only", "ontology/a")
    inner_entry = _entry(git_repo / "service", "shared", "ontology/inner", description="inner")
    _write_catalog(git_repo / "geas.yaml", root_entry, root_only)
    _write_catalog(git_repo / "service/geas.yaml", inner_entry)
    (git_repo / "service/api").mkdir(parents=True)
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "catalog fixture")

    result = resolve_repository_catalog(git_repo / "service/api")

    assert [item.name for item in result.ontologies] == ["root-only", "shared"]
    assert result.by_name("shared").ontology_path == git_repo / "service/ontology/inner"
    assert result.by_name("shared").description == "inner"
    assert result.catalog_paths == (git_repo / "geas.yaml", git_repo / "service/geas.yaml")


def test_discovery_only_reads_direct_git_ancestors_and_fails_closed_on_outer_error(
    git_repo: Path,
) -> None:
    """Recursive discovery could let sibling data affect the current repository context."""
    root = _entry(git_repo, "root", "ontology/root")
    _write_catalog(git_repo / "geas.yaml", root)
    sibling = _entry(git_repo / "sibling", "sibling", "ontology/sibling")
    _write_catalog(git_repo / "sibling/geas.yaml", sibling)
    _write_catalog(git_repo / "service/geas.yaml", {**root, "unknown": True})
    (git_repo / "service/api").mkdir(parents=True)
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "catalog fixture")

    assert discover_catalogs(git_repo / "service/api") == (
        git_repo / "geas.yaml",
        git_repo / "service/geas.yaml",
    )
    assert discover_catalogs(git_repo / "sibling") == (
        git_repo / "geas.yaml",
        git_repo / "sibling/geas.yaml",
    )
    with pytest.raises(ValueError, match="Extra inputs"):
        resolve_repository_catalog(git_repo / "service/api")


def test_non_git_discovery_is_empty(tmp_path: Path) -> None:
    """Discovery must not climb arbitrary non-Git parent directories."""
    _catalog_ontology(tmp_path)
    assert discover_catalogs(tmp_path) == ()


def test_resolved_catalog_preserves_exact_discovery_start(git_repo: Path) -> None:
    """Reverification must retain the endpoint below the deepest present catalog."""
    _catalog_ontology(git_repo)
    discovery_start = git_repo / "service/api"
    discovery_start.mkdir(parents=True)
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "discovery endpoint fixture")

    result = resolve_repository_catalog(discovery_start)

    assert result.discovery_start == discovery_start.resolve()


def test_resolved_catalog_includes_normalized_git_receipt_and_declared_file_dirtiness(
    git_repo: Path,
) -> None:
    """A credential-bearing origin or dirty input must not be hidden from later trust checks."""
    entry = _catalog_ontology(git_repo)
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "catalog fixture")
    _git(git_repo, "remote", "add", "origin", "git@github.com:Owner/Example.git")
    result = resolve_repository_catalog(git_repo)
    assert result.repository_identity == "https://github.com/Owner/Example"
    assert result.identity_kind == "remote"
    assert result.active_ref == "refs/heads/main"
    assert len(result.commit) == 40
    assert result.by_name("example").dirty is False
    (git_repo / str(entry["path"]) / "build.yaml").write_text("dirty")
    refresh_catalog(git_repo / "geas.yaml")
    assert resolve_repository_catalog(git_repo).by_name("example").dirty is True


def test_resolved_catalog_uses_machine_local_identity_without_origin(git_repo: Path) -> None:
    """Treating an origin-less worktree as a remote would create portable trust by mistake."""
    _catalog_ontology(git_repo)
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "catalog fixture")
    result = resolve_repository_catalog(git_repo)
    assert result.identity_kind == "machine_local"
    assert result.repository_identity == str(git_repo.resolve())
