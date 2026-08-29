from __future__ import annotations

import hashlib
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from research_agent.ontology_resolution import OntologySelection

_SLUG_PART = re.compile(r"[^a-z0-9]+")


def geas_config_home(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    platform: str | None = None,
) -> Path:
    """Return the per-user Geas configuration root for the current OS."""
    values = os.environ if environ is None else environ
    user_home = (home or Path.home()).expanduser().resolve()
    override = values.get("GEAS_CONFIG_HOME")
    if override:
        return _absolute_config_path(override, variable="GEAS_CONFIG_HOME")

    current_platform = platform or sys.platform
    if current_platform == "win32":
        appdata = values.get("APPDATA")
        base = (
            _absolute_config_path(appdata, variable="APPDATA")
            if appdata
            else user_home / "AppData" / "Roaming"
        )
        return base / "geas"
    if current_platform == "darwin":
        return user_home / "Library" / "Application Support" / "geas"

    xdg = values.get("XDG_CONFIG_HOME")
    base = (
        _absolute_config_path(xdg, variable="XDG_CONFIG_HOME")
        if xdg
        else user_home / ".config"
    )
    return base / "geas"


def shared_ontology_directory(
    concept_id: str,
    *,
    config_home: Path | None = None,
) -> Path:
    """Resolve one stable, user-readable ontology configuration directory."""
    if not concept_id.startswith("concept:"):
        raise ValueError("ontology concept ID must start with 'concept:'")
    remainder = concept_id.removeprefix("concept:").strip().casefold()
    slug = _SLUG_PART.sub("-", remainder).strip("-")
    if not slug:
        raise ValueError("ontology concept ID does not contain a usable name")
    root = (config_home or geas_config_home()).expanduser().resolve()
    return root / "ontologies" / slug


def resolve_ontology_build_config(value: Path) -> Path:
    """Resolve an explicit build path or a shared ontology name."""
    return _resolve_ontology_config(value, "build.yaml")


def resolve_ontology_library_config(value: Path) -> Path:
    """Resolve an explicit library path or a shared ontology name."""
    return _resolve_ontology_config(value, "library.yaml")


def resolve_profile_ontology_config(
    value: Path,
    *,
    filename: str,
    ontology_root: Path,
) -> Path:
    """Resolve a config within the selected named profile's ontology root."""
    return _resolve_ontology_config(value, filename, ontology_root=ontology_root)


def resolve_selected_ontology_config(
    value: Path,
    *,
    filename: str,
    selection: OntologySelection | None,
) -> Path:
    """Resolve a bare ontology name through an already authorized selection."""
    if selection is None:
        return value
    if value.exists() or value.is_absolute() or len(value.parts) != 1:
        return value
    return selected_conventional_path(selection, filename=filename)


def selected_conventional_path(
    selection: OntologySelection,
    *,
    filename: str,
) -> Path:
    """Resolve and reverify one conventional file from a selected ontology."""
    relative = Path(filename)
    if (
        not filename
        or relative.is_absolute()
        or len(relative.parts) != 1
        or relative.name != filename
        or relative.name.startswith(".")
    ):
        raise ValueError("selected ontology filename must be one conventional relative name")
    path = selection.ontology_directory / relative
    if selection.files is None:
        return path
    _assert_selected_directory_identity(selection)
    declared = next((item for item in selection.files if item.path == relative), None)
    if declared is None:
        raise ValueError(
            f"ontology input {filename!r} is not declared in the verified bundle inventory"
        )
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"declared ontology input is missing or symbolic: {filename}")
    resolved = path.resolve(strict=True)
    expected_directory = selection.verified_ontology_directory
    assert expected_directory is not None
    if not resolved.is_relative_to(expected_directory):
        raise ValueError("declared ontology input escapes the selected ontology directory")
    content = resolved.read_bytes()
    if (
        len(content) != declared.size_bytes
        or hashlib.sha256(content).hexdigest() != declared.sha256
    ):
        raise ValueError(f"declared ontology input size or digest mismatch: {filename}")
    return resolved


def _assert_selected_directory_identity(selection: OntologySelection) -> None:
    expected_directory = selection.verified_ontology_directory
    expected_root = selection.verified_repository_root
    if expected_directory is None or expected_root is None:
        raise ValueError("selected ontology has no verified directory identity")
    if selection.ontology_directory != expected_directory:
        raise ValueError("selected ontology directory identity changed")
    if (
        selection.repository_root is not None
        and selection.repository_root != expected_root
    ):
        raise ValueError("selected ontology repository-root identity changed")
    if not expected_directory.is_relative_to(expected_root):
        raise ValueError("verified ontology directory escapes its verified root")
    for candidate in (expected_directory, *expected_directory.parents):
        if candidate.is_symlink():
            raise ValueError("selected ontology directory ancestry is symbolic")
    try:
        current_root = expected_root.resolve(strict=True)
        current_directory = expected_directory.resolve(strict=True)
    except OSError as error:
        raise ValueError("selected ontology directory identity is no longer present") from error
    if current_root != expected_root or current_directory != expected_directory:
        raise ValueError("selected ontology directory identity changed")


def _resolve_ontology_config(
    value: Path,
    filename: str,
    *,
    ontology_root: Path | None = None,
) -> Path:
    if value.exists() or value.is_absolute() or len(value.parts) != 1:
        return value
    root = ontology_root or geas_config_home() / "ontologies"
    candidate = root / value.name / filename
    return candidate if candidate.exists() else value


def _absolute_config_path(value: str, *, variable: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{variable} must be an absolute path")
    return path.resolve()
