"""Confined environment construction for repository-bound Git commands."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

_IDENTITY_VARIABLES = frozenset(
    {
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_COMMITTER_DATE",
    }
)
_OPERATION_VARIABLES = _IDENTITY_VARIABLES | {"GIT_INDEX_FILE"}
_GITHUB_AUTH_VARIABLES = frozenset({"GH_TOKEN", "GITHUB_TOKEN"})
_GITHUB_REPOSITORY = re.compile(
    r"^github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
)


def _case_insensitive_matches(
    values: Mapping[str, str],
    canonical_name: str,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (name, value)
        for name, value in values.items()
        if name.upper() == canonical_name
    )


def confined_git_environment(
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a Git environment that cannot redirect repository or object state.

    Host process configuration is retained except for Git-specific variables.
    Commit identity is the sole inherited Git namespace because it cannot select
    a repository, index, object database, ref namespace, or configuration file.
    Callers may additionally bind a private index and deterministic commit
    identity for a single bounded operation.
    """

    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith("GIT_")
    }
    for name in sorted(_IDENTITY_VARIABLES):
        matches = _case_insensitive_matches(os.environ, name)
        if len(matches) > 1:
            raise ValueError(
                "ambient Git environment contains colliding names: "
                + ", ".join(sorted(item[0] for item in matches))
            )
        if matches:
            environment[name] = matches[0][1]
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": os.devnull,
            "GIT_CONFIG_KEY_1": "credential.helper",
            "GIT_CONFIG_VALUE_1": "",
        }
    )
    if extra_env is not None:
        canonical_names: dict[str, str] = {}
        collisions: set[str] = set()
        forbidden: list[str] = []
        normalized: dict[str, str] = {}
        for supplied_name, value in extra_env.items():
            canonical_name = supplied_name.upper()
            previous = canonical_names.get(canonical_name)
            if previous is not None and previous != supplied_name:
                collisions.update((previous, supplied_name))
                continue
            canonical_names[canonical_name] = supplied_name
            if (
                canonical_name.startswith("GIT_")
                and canonical_name not in _OPERATION_VARIABLES
            ):
                forbidden.append(supplied_name)
                continue
            output_name = (
                canonical_name
                if canonical_name in _OPERATION_VARIABLES
                else supplied_name
            )
            existing = tuple(
                name for name in environment if name.upper() == canonical_name
            )
            if existing and any(name != output_name for name in existing):
                collisions.update((*existing, supplied_name))
                continue
            normalized[output_name] = value
        if collisions:
            raise ValueError(
                "operation environment contains colliding names: "
                + ", ".join(sorted(collisions))
            )
        if forbidden:
            raise ValueError(
                "Git operation environment contains forbidden selectors: "
                + ", ".join(sorted(forbidden))
            )
        environment.update(normalized)
    return environment


def confined_github_environment(
    *,
    repository: str,
    config_directory: Path,
    auth_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Bind ``gh`` to GitHub.com and one repository without ambient selectors.

    Authentication variables are accepted only through the explicit injected
    mapping. Ambient GitHub tokens, enterprise selectors, sockets, repository
    selectors, and configuration locations are removed case-insensitively.
    """

    if _GITHUB_REPOSITORY.fullmatch(repository) is None:
        raise ValueError("GitHub CLI repository identifier is invalid")
    config = config_directory.expanduser()
    if not config.is_absolute() or any(
        ord(character) < 32 or ord(character) == 127 for character in str(config)
    ):
        raise ValueError("GitHub CLI config directory must be an absolute safe path")
    config = config.resolve(strict=False)
    environment = {
        name: value
        for name, value in confined_git_environment().items()
        if not name.upper().startswith("GH_")
        and not name.upper().startswith("GITHUB_")
    }
    authentication: dict[str, str] = {}
    if auth_environment is not None:
        canonical_names: dict[str, str] = {}
        collisions: set[str] = set()
        forbidden: list[str] = []
        for supplied_name, value in auth_environment.items():
            canonical_name = supplied_name.upper()
            previous = canonical_names.get(canonical_name)
            if previous is not None and previous != supplied_name:
                collisions.update((previous, supplied_name))
                continue
            canonical_names[canonical_name] = supplied_name
            if supplied_name != canonical_name or canonical_name not in _GITHUB_AUTH_VARIABLES:
                forbidden.append(supplied_name)
                continue
            authentication[canonical_name] = value
        if collisions:
            raise ValueError(
                "GitHub authentication environment contains colliding names: "
                + ", ".join(sorted(collisions))
            )
        if forbidden:
            raise ValueError(
                "GitHub authentication environment contains forbidden selectors: "
                + ", ".join(sorted(forbidden))
            )
    environment.update(
        {
            "GH_HOST": "github.com",
            "GH_REPO": repository,
            "GH_CONFIG_DIR": str(config),
            "GH_PROMPT_DISABLED": "1",
            **authentication,
        }
    )
    return environment
