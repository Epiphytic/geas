from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Literal
from uuid import uuid4

import yaml
from pydantic import Field, field_validator, model_validator

from research_agent.models import StrictModel
from research_agent.paths import geas_config_home

DEFAULT_ONTOLOGY_REPOSITORY = "https://github.com/liamhelmer-bel/ontologies.git"
DEFAULT_CONFIG_FILENAMES = (
    "providers.toml",
    "source-policy.yaml",
    "research-policy.yaml",
    "truth-policy.yaml",
    "deposit-policy.yaml",
    "model-policy.yaml",
    "budget-policy.yaml",
    "workload-policy.yaml",
    "query-vocabulary.yaml",
)
_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class ManagedDefaultFile(StrictModel):
    installed_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ManagedDefaultsState(StrictModel):
    version: Literal[1] = 1
    files: dict[str, ManagedDefaultFile] = Field(default_factory=dict)


class DefaultConfigReceipt(StrictModel):
    root: str
    installed: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    preserved: tuple[str, ...] = ()
    review_candidates: tuple[str, ...] = ()


class SecretSource(StrictModel):
    path: Path
    format: Literal["dotenv", "yaml", "json"] = "dotenv"

    @field_validator("path")
    @classmethod
    def path_is_relative(cls, value: Path) -> Path:
        if value.is_absolute() or ".." in value.parts:
            raise ValueError("secret source paths must be config-relative")
        return value


class OntologyGitConfig(StrictModel):
    url: str = Field(min_length=1)
    branch: str = Field(default="main", pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
    remote: str = Field(default="origin", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    pull_before_update: bool = False
    push_on_update: bool = False

    @field_validator("url")
    @classmethod
    def url_has_no_embedded_credentials(cls, value: str) -> str:
        if any(character in value for character in ("\n", "\r", "\x00")):
            raise ValueError("ontology Git URL contains control characters")
        if "://" in value and "@" in value.partition("://")[2].partition("/")[0]:
            raise ValueError("ontology Git URLs cannot embed credentials")
        if value.startswith(("file:", "/", "./", "../")):
            raise ValueError("ontology Git URL must be a remote repository")
        return value


class OntologyFreshnessConfig(StrictModel):
    check_before_use: bool = True
    max_age_seconds: int = Field(default=3600, ge=60, le=604_800)
    hydrate_artifacts_before_use: bool = False


class GeasProfile(StrictModel):
    ontology_directory: Path = Path("ontologies")
    secret_sources: tuple[SecretSource, ...] = (
        SecretSource(path=Path("secrets/common.env"), format="dotenv"),
    )
    ontology_git: OntologyGitConfig | None = None

    @field_validator("ontology_directory")
    @classmethod
    def ontology_directory_is_relative(cls, value: Path) -> Path:
        if value.is_absolute() or ".." in value.parts:
            raise ValueError("profile ontology_directory must be config-relative")
        return value


class GeasUserConfig(StrictModel):
    version: Literal[1] = 1
    default_profile: str = "default"
    ontology_freshness: OntologyFreshnessConfig = Field(
        default_factory=OntologyFreshnessConfig
    )
    profiles: dict[str, GeasProfile]

    @field_validator("default_profile")
    @classmethod
    def default_profile_is_safe(cls, value: str) -> str:
        if not _PROFILE_NAME.fullmatch(value):
            raise ValueError("default profile name is invalid")
        return value

    @model_validator(mode="after")
    def profiles_are_named_and_default_exists(self) -> GeasUserConfig:
        if self.default_profile not in self.profiles:
            raise ValueError("default_profile is absent from profiles")
        invalid = sorted(name for name in self.profiles if not _PROFILE_NAME.fullmatch(name))
        if invalid:
            raise ValueError(f"invalid profile names: {', '.join(invalid)}")
        return self

    @classmethod
    def default(cls) -> GeasUserConfig:
        return cls(
            profiles={
                "default": GeasProfile(
                    ontology_git=OntologyGitConfig(url=DEFAULT_ONTOLOGY_REPOSITORY),
                )
            }
        )

    @classmethod
    def from_yaml(cls, path: Path) -> GeasUserConfig:
        return cls.model_validate(yaml.safe_load(path.read_text()))

    def explicit_yaml(self) -> str:
        return yaml.safe_dump(
            self.model_dump(mode="json", exclude_none=False),
            sort_keys=False,
            allow_unicode=True,
        )

    def profile(self, name: str | None = None) -> tuple[str, GeasProfile]:
        selected = name or self.default_profile
        try:
            return selected, self.profiles[selected]
        except KeyError:
            raise ValueError(f"unknown Geas profile: {selected}") from None


class UserConfigManager:
    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or geas_config_home() / "config.yaml").expanduser().resolve()
        self.root = self.path.parent
        self.last_defaults_receipt: DefaultConfigReceipt | None = None

    def load(self) -> GeasUserConfig:
        if not self.path.is_file():
            raise ValueError(f"Geas user config does not exist: {self.path}")
        return GeasUserConfig.from_yaml(self.path)

    def load_or_create(self, *, update_defaults: bool = False) -> GeasUserConfig:
        if self.path.exists():
            config = self.load()
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            config = GeasUserConfig.default()
            self.path.write_text(config.explicit_yaml())
        self._ensure_secret_scaffold()
        self.last_defaults_receipt = self.install_defaults(update=update_defaults)
        return config

    def install_defaults(self, *, update: bool = False) -> DefaultConfigReceipt:
        """Install packaged policy defaults without overwriting operator changes."""
        self.root.mkdir(parents=True, exist_ok=True)
        state_path = self.root / "defaults-state.json"
        if state_path.is_symlink():
            raise ValueError("managed-default state cannot be a symbolic link")
        state = self._load_defaults_state(state_path)
        installed: list[str] = []
        updated: list[str] = []
        unchanged: list[str] = []
        preserved: list[str] = []
        candidates: list[str] = []
        next_files = dict(state.files)

        for filename in DEFAULT_CONFIG_FILENAMES:
            template = default_config_path(filename).read_bytes()
            template_hash = _sha256(template)
            destination = self.policy_path(filename)
            if destination.is_symlink():
                raise ValueError(f"managed config cannot be a symbolic link: {destination}")
            previous = state.files.get(filename)
            if not destination.exists():
                _atomic_write(destination, template)
                installed.append(filename)
                next_files[filename] = ManagedDefaultFile(
                    installed_sha256=template_hash,
                    template_sha256=template_hash,
                )
                continue
            if not destination.is_file():
                raise ValueError(f"managed config must be a regular file: {destination}")

            current_hash = _sha256(destination.read_bytes())
            if current_hash == template_hash:
                unchanged.append(filename)
                next_files[filename] = ManagedDefaultFile(
                    installed_sha256=template_hash,
                    template_sha256=template_hash,
                )
                self._remove_candidate(destination)
                continue
            if (
                previous is not None
                and previous.installed_sha256 is not None
                and current_hash == previous.installed_sha256
                and update
            ):
                _atomic_write(destination, template)
                updated.append(filename)
                next_files[filename] = ManagedDefaultFile(
                    installed_sha256=template_hash,
                    template_sha256=template_hash,
                )
                self._remove_candidate(destination)
                continue

            preserved.append(filename)
            candidate = destination.with_name(f"{destination.name}.new")
            if candidate.is_symlink():
                raise ValueError(f"managed config candidate cannot be a symlink: {candidate}")
            if not candidate.exists() or candidate.read_bytes() != template:
                _atomic_write(candidate, template)
            candidates.append(candidate.name)
            next_files[filename] = ManagedDefaultFile(
                installed_sha256=(
                    previous.installed_sha256 if previous is not None else None
                ),
                template_sha256=template_hash,
            )

        next_state = ManagedDefaultsState(files=next_files)
        _atomic_write(
            state_path,
            json.dumps(next_state.model_dump(mode="json"), indent=2, sort_keys=True).encode()
            + b"\n",
        )
        return DefaultConfigReceipt(
            root=str(self.root),
            installed=tuple(installed),
            updated=tuple(updated),
            unchanged=tuple(unchanged),
            preserved=tuple(preserved),
            review_candidates=tuple(candidates),
        )

    def policy_path(self, filename: str) -> Path:
        if filename not in DEFAULT_CONFIG_FILENAMES:
            raise ValueError(f"unknown managed Geas config file: {filename}")
        return self._confined(Path(filename))

    @staticmethod
    def _load_defaults_state(path: Path) -> ManagedDefaultsState:
        if not path.exists():
            return ManagedDefaultsState()
        try:
            return ManagedDefaultsState.model_validate_json(path.read_text())
        except ValueError as error:
            raise ValueError(f"invalid managed-default state: {path}") from error

    @staticmethod
    def _remove_candidate(destination: Path) -> None:
        candidate = destination.with_name(f"{destination.name}.new")
        if candidate.is_symlink():
            raise ValueError(f"managed config candidate cannot be a symlink: {candidate}")
        if candidate.exists():
            candidate.unlink()

    def _ensure_secret_scaffold(self) -> None:
        secrets = self.root / "secrets"
        secrets.mkdir(parents=True, exist_ok=True)
        secrets.chmod(0o700)
        ignore = secrets / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n!.gitignore\n")

    def profile(
        self,
        name: str | None = None,
        *,
        create: bool = False,
    ) -> tuple[str, GeasProfile]:
        config = self.load_or_create() if create else self.load()
        return config.profile(name)

    def ontology_root(self, profile: GeasProfile) -> Path:
        return self._confined(profile.ontology_directory)

    def secret_paths(self, profile: GeasProfile) -> tuple[tuple[Path, str], ...]:
        return tuple(
            (self._confined(source.path), source.format)
            for source in profile.secret_sources
        )

    def _confined(self, relative: Path) -> Path:
        resolved = (self.root / relative).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("Geas profile path escapes the user config directory")
        return resolved


def default_config_path(filename: str) -> Path:
    if filename not in DEFAULT_CONFIG_FILENAMES:
        raise ValueError(f"unknown packaged Geas config file: {filename}")
    path = Path(__file__).parent / "default_config" / filename
    if not path.is_file():
        raise ValueError(f"packaged Geas config is missing: {filename}")
    return path


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
    try:
        temporary.write_bytes(value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
