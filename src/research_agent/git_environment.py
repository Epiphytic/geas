"""Confined environment construction for repository-bound Git commands."""

from __future__ import annotations

import os
from collections.abc import Mapping

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
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    environment.update(
        {
            name: os.environ[name]
            for name in _IDENTITY_VARIABLES
            if name in os.environ
        }
    )
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
        forbidden = tuple(
            sorted(
                name
                for name in extra_env
                if name.startswith("GIT_") and name not in _OPERATION_VARIABLES
            )
        )
        if forbidden:
            raise ValueError(
                "Git operation environment contains forbidden selectors: "
                + ", ".join(forbidden)
            )
        environment.update(extra_env)
    return environment
