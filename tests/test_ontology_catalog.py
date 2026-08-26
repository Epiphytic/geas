import json
import subprocess
from pathlib import Path

import pytest

from research_agent.library import SourceLibraryManifest
from research_agent.ontology_build import OntologyBuildConfig
from research_agent.ontology_catalog import inventory_ontologies


def _write_ontology(directory: Path, *, concept_id: str) -> None:
    directory.mkdir(parents=True)
    (directory / "build.yaml").write_text(
        OntologyBuildConfig(
            version=1,
            topic=f"Topic {concept_id}",
            topic_concept_id=concept_id,
            provider="deepseek_local",
            output_directory=Path("data/generated"),
        ).explicit_yaml()
    )
    (directory / "library.yaml").write_text(
        SourceLibraryManifest(
            version=1,
            id=f"library:{concept_id.removeprefix('concept:')}",
            title=f"Library {concept_id}",
            description="Test inventory library.",
            include_all_parsed_sources=True,
        ).explicit_yaml()
    )


def test_inventory_lists_valid_incomplete_and_invalid_ontologies(tmp_path: Path) -> None:
    root = tmp_path / "ontologies"
    _write_ontology(root / "z-valid", concept_id="concept:z-valid")
    incomplete = root / "a-incomplete"
    _write_ontology(incomplete, concept_id="concept:a-incomplete")
    (incomplete / "library.yaml").unlink()
    invalid = root / "m-invalid"
    invalid.mkdir()
    (invalid / "build.yaml").write_text("not: a build\n")
    (invalid / "library.yaml").write_text("not: a library\n")
    ignored = root / "ordinary-directory"
    ignored.mkdir()

    result = inventory_ontologies(root)

    assert tuple(item.name for item in result.ontologies) == (
        "a-incomplete",
        "m-invalid",
        "z-valid",
    )
    assert tuple(item.status for item in result.ontologies) == (
        "incomplete",
        "invalid",
        "valid",
    )
    assert result.ontologies[-1].topic_concept_id == "concept:z-valid"


def test_ontology_list_cli_accepts_a_provided_directory(tmp_path: Path) -> None:
    root = tmp_path / "ontologies"
    _write_ontology(root / "example", concept_id="concept:example")

    completed = subprocess.run(
        ("uv", "run", "geas", "ontology-list", str(root)),
        check=True,
        capture_output=True,
        text=True,
    )
    receipt = json.loads(completed.stdout)

    assert receipt["location"] == "provided_directory"
    assert receipt["profile"] is None
    assert receipt["count"] == 1
    assert receipt["ontologies"][0]["name"] == "example"


def test_ontology_inventory_rejects_symlinked_roots(tmp_path: Path) -> None:
    root = tmp_path / "ontologies"
    root.mkdir()
    link = tmp_path / "linked-ontologies"
    link.symlink_to(root, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        inventory_ontologies(link)
