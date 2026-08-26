from __future__ import annotations

from pathlib import Path
from typing import Literal

from research_agent.library import SourceLibraryManifest
from research_agent.models import StrictModel
from research_agent.ontology_build import OntologyBuildConfig


class OntologyInventoryItem(StrictModel):
    name: str
    directory: str
    status: Literal["valid", "incomplete", "invalid"]
    build_config: str | None = None
    library_config: str | None = None
    topic: str | None = None
    topic_concept_id: str | None = None
    provider: str | None = None
    library_id: str | None = None
    library_title: str | None = None
    problems: tuple[str, ...] = ()


class OntologyInventory(StrictModel):
    version: Literal[1] = 1
    root: str
    exists: bool
    count: int
    ontologies: tuple[OntologyInventoryItem, ...]


def inventory_ontologies(root: Path) -> OntologyInventory:
    """List direct child ontology configurations without following symlinks."""
    expanded = root.expanduser()
    if expanded.is_symlink():
        raise ValueError("ontology inventory root cannot be a symbolic link")
    resolved = expanded.resolve()
    if not resolved.exists():
        return OntologyInventory(root=str(resolved), exists=False, count=0, ontologies=())
    if not resolved.is_dir():
        raise ValueError("ontology inventory root must be a directory")

    items: list[OntologyInventoryItem] = []
    for directory in sorted(resolved.iterdir(), key=lambda item: item.name.casefold()):
        if directory.name.startswith("."):
            continue
        if directory.is_symlink():
            raise ValueError(f"ontology directory cannot be a symbolic link: {directory.name}")
        if not directory.is_dir():
            continue
        build_path = directory / "build.yaml"
        library_path = directory / "library.yaml"
        if not build_path.exists() and not library_path.exists():
            continue
        problems: list[str] = []
        build = None
        library = None
        if build_path.is_symlink() or library_path.is_symlink():
            raise ValueError(f"ontology config cannot be a symbolic link: {directory.name}")
        if build_path.is_file():
            try:
                build = OntologyBuildConfig.from_yaml(build_path)
            except (OSError, ValueError):
                problems.append("invalid build.yaml")
        else:
            problems.append("missing build.yaml")
        if library_path.is_file():
            try:
                library = SourceLibraryManifest.from_yaml(library_path)
            except (OSError, ValueError):
                problems.append("invalid library.yaml")
        else:
            problems.append("missing library.yaml")
        status: Literal["valid", "incomplete", "invalid"] = "valid"
        if problems:
            status = (
                "invalid"
                if any(problem.startswith("invalid") for problem in problems)
                else "incomplete"
            )
        items.append(
            OntologyInventoryItem(
                name=directory.name,
                directory=str(directory),
                status=status,
                build_config=str(build_path) if build_path.is_file() else None,
                library_config=str(library_path) if library_path.is_file() else None,
                topic=build.topic if build is not None else None,
                topic_concept_id=build.topic_concept_id if build is not None else None,
                provider=build.provider if build is not None else None,
                library_id=library.id if library is not None else None,
                library_title=library.title if library is not None else None,
                problems=tuple(problems),
            )
        )
    return OntologyInventory(
        root=str(resolved),
        exists=True,
        count=len(items),
        ontologies=tuple(items),
    )
