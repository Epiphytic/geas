from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from pathlib import Path

import yaml

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def load_env_file(path: Path, *, allowed_names: frozenset[str]) -> frozenset[str]:
    """Load only explicitly allowed names without shell evaluation or interpolation."""
    if not path.exists():
        return frozenset()
    loaded: set[str] = set()
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, raw_value = line.partition("=")
        name = name.strip()
        if not separator or not _NAME.fullmatch(name):
            raise ValueError(f"invalid environment assignment at {path}:{line_number}")
        if name not in allowed_names or name in os.environ:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[name] = value
        loaded.add(name)
    return frozenset(loaded)


def load_secret_file(
    path: Path,
    *,
    format: str,
    allowed_names: frozenset[str],
) -> frozenset[str]:
    """Load one allowlisted dotenv, YAML, or JSON secret mapping."""
    if format == "dotenv":
        return load_env_file(path, allowed_names=allowed_names)
    if format not in {"yaml", "json"}:
        raise ValueError(f"unsupported secret source format: {format}")
    if not path.exists():
        return frozenset()
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"secret source must be a regular non-symlink file: {path}")
    try:
        raw = (
            yaml.safe_load(path.read_text())
            if format == "yaml"
            else json.loads(path.read_text())
        )
    except (yaml.YAMLError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid {format} secret source: {path}") from error
    if raw is None:
        return frozenset()
    if not isinstance(raw, dict):
        raise ValueError(f"{format} secret source must contain a top-level mapping: {path}")
    loaded: set[str] = set()
    for name, value in raw.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError(f"invalid environment name in secret source: {path}")
        if not isinstance(value, str):
            raise ValueError(f"secret value for {name} must be a string: {path}")
        if name not in allowed_names or name in os.environ:
            continue
        os.environ[name] = value
        loaded.add(name)
    return frozenset(loaded)


def load_secret_sources(
    sources: Iterable[tuple[Path, str]],
    *,
    allowed_names: frozenset[str],
) -> frozenset[str]:
    """Load ordered modular secret sources without overwriting earlier authority."""
    loaded: set[str] = set()
    for path, format in sources:
        loaded.update(
            load_secret_file(
                path,
                format=format,
                allowed_names=allowed_names,
            )
        )
    return frozenset(loaded)
