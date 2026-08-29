from __future__ import annotations

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
    return selection.ontology_directory / filename


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
