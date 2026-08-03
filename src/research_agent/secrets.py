from __future__ import annotations

import os
import re
from pathlib import Path

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
