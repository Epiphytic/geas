"""Versioned, fail-closed authority contracts for automatic acquisition."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, field_validator, model_validator

from research_agent.models import StrictModel, content_id


class Capability(StrEnum):
    REPOSITORY_READ = "repository.read"
    TRUST_DELEGATE = "trust.delegate"
    SOURCE_DISCOVER = "source.discover"
    SOURCE_FETCH = "source.fetch"
    SOURCE_ARCHIVE = "source.archive"
    SOURCE_EXTRACT = "source.extract"
    MODEL_EXTERNAL = "model.external"
    GIT_PULL_REQUEST = "git.pull_request"
    GIT_AUTO_MERGE = "git.auto_merge"
    GIT_DIRECT_PUSH = "git.direct_push"
    KNOWLEDGE_AUTO_PROMOTE = "knowledge.auto_promote"


_REF = re.compile(r"^refs/(?:heads|tags)/[A-Za-z0-9][A-Za-z0-9._/-]*$")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")


def _https_url(value: object, *, label: str) -> str:
    raw = str(value)
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise ValueError(f"{label} must be a credential-free HTTPS URL on the default port")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        raise ValueError(f"{label} must name a hostname, not an IP address")
    path = parsed.path.rstrip("/")
    if not path or "/../" in f"/{path}/" or "//" in path:
        raise ValueError(f"{label} path is unsafe")
    return urlunsplit(("https", parsed.hostname.lower(), path, "", ""))


def _relative_path(value: object, *, label: str, allow_root: bool = False) -> str:
    raw = str(value)
    if (
        not raw
        or raw.startswith("/")
        or "\\" in raw
        or any(part in {"", ".", ".."} for part in raw.split("/"))
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise ValueError(f"{label} must be a normalized relative path")
    if raw == "." and allow_root:
        return raw
    return raw


def _path_prefix(value: object) -> str:
    raw = str(value)
    if raw == "*":
        return raw
    if (
        not raw.startswith("/")
        or "\\" in raw
        or "//" in raw
        or "/../" in f"/{raw}/"
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise ValueError("path prefix must be a normalized absolute URL path or '*'")
    return raw


def _ref(value: object) -> str:
    raw = str(value)
    if (
        not _REF.fullmatch(raw)
        or "//" in raw
        or ".." in raw
        or "@{" in raw
        or raw.endswith("/")
        or raw.endswith(".lock")
    ):
        raise ValueError("git ref must be a safe fully-qualified branch or tag ref")
    return raw


def _sha256(value: object, *, label: str = "sha256") -> str:
    raw = str(value)
    if not _HEX.fullmatch(raw):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return raw


def _host(value: object) -> str:
    raw = str(value).lower().rstrip(".")
    if not _HOST.fullmatch(raw) or "." not in raw:
        raise ValueError("host must be a normalized public hostname")
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        return raw
    raise ValueError("host must not be an IP address")


def _ordered(values: tuple[str, ...], *, label: str, minimum: int = 0) -> tuple[str, ...]:
    normalized = tuple(sorted(set(values)))
    if len(normalized) < minimum:
        raise ValueError(f"{label} must contain at least {minimum} selector")
    return normalized


class CapabilitySubject(StrictModel):
    version: Literal[1] = 1
    repository: str
    refs: tuple[str, ...] | Literal["*"]
    paths: tuple[str, ...] | Literal["*"]
    bundle_sha256: tuple[str, ...] | Literal["*"]

    @field_validator("repository", mode="before")
    @classmethod
    def normalize_repository(cls, value: object) -> str:
        return _https_url(value, label="repository")

    @field_validator("refs", mode="before")
    @classmethod
    def normalize_refs(cls, value: object) -> tuple[str, ...] | Literal["*"]:
        if value == "*":
            return "*"
        return _ordered(tuple(_ref(item) for item in value), label="refs", minimum=1)  # type: ignore[arg-type]

    @field_validator("paths", mode="before")
    @classmethod
    def normalize_paths(cls, value: object) -> tuple[str, ...] | Literal["*"]:
        if value == "*":
            return "*"
        return _ordered(
            tuple(_relative_path(item, label="subject path") for item in value),
            label="paths",
            minimum=1,
        )  # type: ignore[arg-type]

    @field_validator("bundle_sha256", mode="before")
    @classmethod
    def normalize_digests(cls, value: object) -> tuple[str, ...] | Literal["*"]:
        if value == "*":
            return "*"
        return _ordered(
            tuple(_sha256(item, label="bundle_sha256") for item in value),
            label="bundle_sha256",
            minimum=1,
        )  # type: ignore[arg-type]


class CapabilityResources(StrictModel):
    version: Literal[1] = 1
    delegated_repositories: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()
    path_prefixes: tuple[str, ...] | Literal["*"] = ()
    connectors: tuple[str, ...] = ()
    providers: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    data_classes: tuple[str, ...] = ()
    git_refs: tuple[str, ...] | Literal["*"] = ()

    @field_validator("delegated_repositories", mode="before")
    @classmethod
    def normalize_repositories(cls, value: object) -> tuple[str, ...]:
        repositories = tuple(
            _https_url(item, label="delegated repository")
            for item in value  # type: ignore[arg-type]
        )
        return _ordered(repositories, label="delegated_repositories")

    @field_validator("hosts", mode="before")
    @classmethod
    def normalize_hosts(cls, value: object) -> tuple[str, ...]:
        return _ordered(tuple(_host(item) for item in value), label="hosts")  # type: ignore[arg-type]

    @field_validator("path_prefixes", mode="before")
    @classmethod
    def normalize_prefixes(cls, value: object) -> tuple[str, ...] | Literal["*"]:
        if value == "*":
            return "*"
        return _ordered(tuple(_path_prefix(item) for item in value), label="path_prefixes")  # type: ignore[arg-type]

    @field_validator("git_refs", mode="before")
    @classmethod
    def normalize_git_refs(cls, value: object) -> tuple[str, ...] | Literal["*"]:
        if value == "*":
            return "*"
        return _ordered(tuple(_ref(item) for item in value), label="git_refs")  # type: ignore[arg-type]

    @field_validator("connectors", "providers", "models", "data_classes", mode="before")
    @classmethod
    def normalize_identifiers(cls, value: object) -> tuple[str, ...]:
        values = tuple(str(item) for item in value)  # type: ignore[arg-type]
        if any(not item or item.strip() != item for item in values):
            raise ValueError("resource identifiers must be non-empty normalized strings")
        return _ordered(values, label="resource identifiers")


class CapabilityGrant(StrictModel):
    version: Literal[1] = 1
    decision: Literal["allow", "deny"]
    subject: CapabilitySubject
    capabilities: tuple[Capability, ...] = Field(min_length=1)
    delegable_capabilities: tuple[Capability, ...] = ()
    resources: CapabilityResources
    max_delegation_depth: int = Field(default=1, ge=0, le=32)
    expires_at: datetime | None
    created_at: datetime
    created_via: Literal["interactive", "manual", "repository_install"]

    @field_validator("capabilities", "delegable_capabilities")
    @classmethod
    def normalize_capabilities(cls, value: tuple[Capability, ...]) -> tuple[Capability, ...]:
        return tuple(sorted(set(value), key=lambda item: item.value))

    @field_validator("expires_at", "created_at")
    @classmethod
    def timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def delegation_only_narrows(self) -> CapabilityGrant:
        if not set(self.delegable_capabilities).issubset(self.capabilities):
            raise ValueError("delegable_capabilities must be a subset of capabilities")
        return self

    @property
    def id(self) -> str:
        return content_id("capability-grant", self.model_dump(mode="json"))


class CapabilityRequest(StrictModel):
    version: Literal[1] = 1
    authority_repository: str
    target_repository: str
    capabilities: tuple[Capability, ...] = Field(min_length=1)
    ref: str
    path: str
    bundle_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    connector: str | None = None
    host: str | None = None
    target: str | None = None
    requested_at: datetime

    @field_validator("authority_repository", "target_repository", mode="before")
    @classmethod
    def normalize_repositories(cls, value: object) -> str:
        return _https_url(value, label="repository")

    @field_validator("ref", mode="before")
    @classmethod
    def normalize_ref(cls, value: object) -> str:
        return _ref(value)

    @field_validator("path", mode="before")
    @classmethod
    def normalize_path(cls, value: object) -> str:
        return _relative_path(value, label="request path")

    @field_validator("host", mode="before")
    @classmethod
    def normalize_host(cls, value: object) -> str | None:
        return None if value is None else _host(value)

    @field_validator("capabilities")
    @classmethod
    def normalize_capabilities(cls, value: tuple[Capability, ...]) -> tuple[Capability, ...]:
        return tuple(sorted(set(value), key=lambda item: item.value))

    @field_validator("requested_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @property
    def id(self) -> str:
        return content_id("capability-request", self.model_dump(mode="json"))


class CapabilityDecision(StrictModel):
    version: Literal[1] = 1
    request: CapabilityRequest
    decision: Literal["allow", "deny"]
    effective_capabilities: tuple[Capability, ...] = ()
    grant_ids: tuple[str, ...] = ()
    delegation_chain: tuple[str, ...] = ()
    reason: str
    evaluator_version: str
    decided_at: datetime

    @field_validator("effective_capabilities")
    @classmethod
    def normalize_effective_capabilities(
        cls, value: tuple[Capability, ...]
    ) -> tuple[Capability, ...]:
        return tuple(sorted(set(value), key=lambda item: item.value))

    @field_validator("grant_ids")
    @classmethod
    def normalize_grant_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _ordered(value, label="receipt identities")

    @field_validator("delegation_chain", mode="before")
    @classmethod
    def preserve_ordered_delegation_chain(cls, value: object) -> tuple[str, ...]:
        chain = tuple(_https_url(item, label="delegation repository") for item in value)  # type: ignore[arg-type]
        if len(chain) != len(set(chain)):
            raise ValueError("delegation_chain must not repeat repository identities")
        return chain

    @field_validator("decided_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def effective_capabilities_match_decision(self) -> CapabilityDecision:
        if self.decision == "deny" and self.effective_capabilities:
            raise ValueError("denied decisions must have empty effective_capabilities")
        if self.decision == "allow" and set(self.effective_capabilities) != set(
            self.request.capabilities
        ):
            raise ValueError(
                "allowed effective_capabilities must exactly match requested capabilities"
            )
        return self

    @property
    def id(self) -> str:
        return content_id("capability-decision", self.model_dump(mode="json"))

    @property
    def sha256(self) -> str:
        return self.id.rsplit(":", 1)[-1]


class DelegationEntry(StrictModel):
    version: Literal[1] = 1
    subject: CapabilitySubject
    capabilities: tuple[Capability, ...] = Field(min_length=1)
    delegable_capabilities: tuple[Capability, ...] = ()
    resources: CapabilityResources
    max_delegation_depth: int = Field(ge=0, le=32)
    expires_at: datetime | None
    description: str = ""

    @field_validator("capabilities", "delegable_capabilities")
    @classmethod
    def normalize_capabilities(cls, value: tuple[Capability, ...]) -> tuple[Capability, ...]:
        return tuple(sorted(set(value), key=lambda item: item.value))

    @field_validator("expires_at")
    @classmethod
    def timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def delegation_only_narrows(self) -> DelegationEntry:
        if not set(self.delegable_capabilities).issubset(self.capabilities):
            raise ValueError("delegable_capabilities must be a subset of capabilities")
        return self


class DelegationManifest(StrictModel):
    version: Literal[1] = 1
    delegations: tuple[DelegationEntry, ...] = ()

    @field_validator("delegations")
    @classmethod
    def sorted_unique_subjects(
        cls, value: tuple[DelegationEntry, ...]
    ) -> tuple[DelegationEntry, ...]:
        ordered = tuple(sorted(value, key=lambda entry: entry.subject.repository))
        repositories = [entry.subject.repository for entry in ordered]
        if len(repositories) != len(set(repositories)):
            raise ValueError("delegations must have unique child repositories")
        return ordered

    @property
    def id(self) -> str:
        return content_id("delegation-manifest", self.model_dump(mode="json"))


class CapabilityEvaluator(Protocol):
    def evaluate(self, request: CapabilityRequest) -> CapabilityDecision: ...
