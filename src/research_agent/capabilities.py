"""Versioned, fail-closed authority contracts for automatic acquisition."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal, Protocol
from urllib.parse import quote_from_bytes, unquote, unquote_to_bytes, urlsplit, urlunsplit

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
_RESOURCE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40,64}$")


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
    path = parsed.path.rstrip("/").removesuffix(".git")
    if not path or "/../" in f"/{path}/" or "//" in path:
        raise ValueError(f"{label} path is unsafe")
    return urlunsplit(("https", parsed.hostname.lower(), path, "", ""))


def _repository_identity(value: object, *, label: str) -> str:
    """Normalize repository identity without granting network authority."""
    raw = str(value)
    if not raw or any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise ValueError(f"{label} is invalid")
    if raw.startswith("/"):
        pure = PurePosixPath(raw)
        if raw == "/" or pure.as_posix() != raw or any(part in {".", ".."} for part in pure.parts):
            raise ValueError(f"{label} must be a normalized absolute repository path")
        return raw
    parsed = urlsplit(raw)
    if parsed.scheme not in {"https", "ssh"}:
        raise ValueError(f"{label} uses an unsupported repository transport")
    if parsed.password is not None or (
        parsed.username is not None and not (parsed.scheme == "ssh" and parsed.username == "git")
    ):
        raise ValueError(f"{label} cannot embed credentials")
    if not parsed.hostname or parsed.query or parsed.fragment:
        raise ValueError(f"{label} must be a credential-free repository identity")
    try:
        port_number = parsed.port
    except ValueError as error:
        raise ValueError(f"{label} port is invalid") from error
    decoded_path = unquote(parsed.path)
    pure_path = PurePosixPath(decoded_path)
    if (
        not decoded_path
        or pure_path.as_posix() != decoded_path
        or any(part in {"", ".", ".."} for part in pure_path.parts)
    ):
        raise ValueError(f"{label} path is unsafe")
    host = parsed.hostname.lower()
    path = parsed.path.rstrip("/").removesuffix(".git")
    if (
        host == "github.com"
        and port_number is None
        and (parsed.scheme == "https" or parsed.username == "git")
    ):
        return f"https://github.com/{path.lstrip('/')}"
    rendered_host = f"[{host}]" if ":" in host else host
    username = "git@" if parsed.username == "git" else ""
    port = f":{port_number}" if port_number is not None else ""
    return f"{parsed.scheme.lower()}://{username}{rendered_host}{port}{path}"


def _source_target(value: object) -> tuple[str, str]:
    """Validate a byte-stable source wire URL and derive its host."""
    raw = str(value)
    parsed = urlsplit(raw)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("source target must be a safe credential-free HTTPS URL") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise ValueError("source target must be a canonical credential-free HTTPS URL")
    host = _host(parsed.hostname)
    path = parsed.path
    canonical_path = quote_from_bytes(unquote_to_bytes(path), safe="/-._~")
    canonical_target = urlunsplit(("https", host, path, "", ""))
    if (
        not path.startswith("/")
        or "\\" in path
        or "//" in path
        or any(part in {".", ".."} for part in path.split("/"))
        or canonical_path != path
        or canonical_target != raw
    ):
        raise ValueError("source target must use a canonical wire URL path")
    return raw, host


def _resource_identifier(value: object, *, label: str) -> str:
    raw = str(value)
    if not _RESOURCE_IDENTIFIER.fullmatch(raw):
        raise ValueError(f"{label} must be a normalized resource identifier")
    return raw


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
    if _GIT_OBJECT_ID.fullmatch(raw):
        return raw
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
        return _repository_identity(value, label="repository")

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
    delegated_repositories: tuple[str, ...] | Literal["*"] = ()
    hosts: tuple[str, ...] | Literal["*"] = ()
    path_prefixes: tuple[str, ...] | Literal["*"] = ()
    connectors: tuple[str, ...] | Literal["*"] = ()
    providers: tuple[str, ...] | Literal["*"] = ()
    models: tuple[str, ...] | Literal["*"] = ()
    data_classes: tuple[str, ...] | Literal["*"] = ()
    git_refs: tuple[str, ...] | Literal["*"] = ()

    @field_validator("delegated_repositories", mode="before")
    @classmethod
    def normalize_repositories(cls, value: object) -> tuple[str, ...] | Literal["*"]:
        if value == "*":
            return "*"
        repositories = tuple(
            _repository_identity(item, label="delegated repository")
            for item in value  # type: ignore[arg-type]
        )
        return _ordered(repositories, label="delegated_repositories")

    @field_validator("hosts", mode="before")
    @classmethod
    def normalize_hosts(cls, value: object) -> tuple[str, ...] | Literal["*"]:
        if value == "*":
            return "*"
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
    def normalize_identifiers(cls, value: object) -> tuple[str, ...] | Literal["*"]:
        if value == "*":
            return "*"
        values = tuple(
            _resource_identifier(item, label="resource identifier")
            for item in value  # type: ignore[arg-type]
        )
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
    provider: str | None = None
    model: str | None = None
    data_class: str | None = None
    dirty: bool = False
    requested_at: datetime

    @model_validator(mode="before")
    @classmethod
    def resources_are_complete_and_bound(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        normalized = dict(value)
        capabilities = {
            item.value if isinstance(item, Capability) else str(item)
            for item in normalized.get("capabilities", ())
        }
        source_capabilities = {
            Capability.SOURCE_DISCOVER.value,
            Capability.SOURCE_FETCH.value,
            Capability.SOURCE_ARCHIVE.value,
            Capability.SOURCE_EXTRACT.value,
        }
        if capabilities.intersection(source_capabilities):
            supplied = tuple(
                normalized.get(selector) is not None for selector in ("connector", "host", "target")
            )
            if any(supplied):
                if not all(supplied):
                    raise ValueError(
                        "source capability resource selectors must be supplied together"
                    )
                canonical_target, derived_host = _source_target(normalized["target"])
                claimed_host = _host(normalized["host"])
                if claimed_host != derived_host:
                    raise ValueError("source request host must match the host derived from target")
                normalized["target"] = canonical_target
                normalized["host"] = derived_host
        elif normalized.get("target") is not None:
            normalized["target"] = _resource_identifier(normalized["target"], label="target")
        if Capability.MODEL_EXTERNAL.value in capabilities:
            supplied = tuple(
                normalized.get(selector) is not None
                for selector in ("provider", "model", "data_class")
            )
            if any(supplied) and not all(supplied):
                raise ValueError("model.external resource selectors must be supplied together")
        return normalized

    @field_validator("authority_repository", "target_repository", mode="before")
    @classmethod
    def normalize_repositories(cls, value: object) -> str:
        return _repository_identity(value, label="repository")

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

    @field_validator("connector", "provider", "model", "data_class", mode="before")
    @classmethod
    def normalize_resource_identifiers(cls, value: object) -> str | None:
        return (
            None
            if value is None
            else _resource_identifier(value, label="request resource selector")
        )

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
    manifest_ids: tuple[str, ...] = ()
    manifest_sha256s: tuple[str, ...] = ()
    catalog_commits: tuple[str, ...] = ()
    effective_resources: CapabilityResources = Field(default_factory=CapabilityResources)
    effective_remaining_depth: int = Field(default=0, ge=0, le=32)

    @field_validator("effective_capabilities")
    @classmethod
    def normalize_effective_capabilities(
        cls, value: tuple[Capability, ...]
    ) -> tuple[Capability, ...]:
        return tuple(sorted(set(value), key=lambda item: item.value))

    @field_validator("grant_ids", "manifest_ids")
    @classmethod
    def normalize_grant_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _ordered(value, label="receipt identities")

    @field_validator("manifest_sha256s", mode="before")
    @classmethod
    def normalize_manifest_sha256s(cls, value: object) -> tuple[str, ...]:
        return _ordered(
            tuple(
                _sha256(item, label="manifest_sha256")
                for item in value  # type: ignore[arg-type]
            ),
            label="manifest_sha256s",
        )

    @field_validator("catalog_commits", mode="before")
    @classmethod
    def normalize_catalog_commits(cls, value: object) -> tuple[str, ...]:
        commits = tuple(str(item) for item in value)  # type: ignore[arg-type]
        if any(not _GIT_OBJECT_ID.fullmatch(item) for item in commits):
            raise ValueError("catalog commits must be lowercase full Git object IDs")
        return _ordered(commits, label="catalog_commits")

    @field_validator("delegation_chain", mode="before")
    @classmethod
    def preserve_ordered_delegation_chain(cls, value: object) -> tuple[str, ...]:
        chain = tuple(
            _repository_identity(item, label="delegation repository")
            for item in value  # type: ignore[arg-type]
        )
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

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


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
        repositories = [entry.subject.repository for entry in value]
        if len(repositories) != len(set(repositories)):
            raise ValueError("delegations must have unique child repositories")
        if repositories != sorted(repositories, key=lambda item: item.encode("utf-8")):
            raise ValueError("delegations must be in ascending repository order")
        return value

    @property
    def id(self) -> str:
        return content_id("delegation-manifest", self.model_dump(mode="json"))


class VerifiedDelegationManifest(StrictModel):
    """A parsed manifest bound to catalog-verified bytes and Git truth."""

    version: Literal[1] = 1
    repository: str
    manifest: DelegationManifest
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")

    @field_validator("repository", mode="before")
    @classmethod
    def normalize_repository(cls, value: object) -> str:
        return _repository_identity(value, label="verified delegation repository")

    @field_validator("manifest_sha256", mode="before")
    @classmethod
    def normalize_manifest_sha256(cls, value: object) -> str:
        return _sha256(value, label="manifest_sha256")

    @field_validator("catalog_commit", mode="before")
    @classmethod
    def normalize_catalog_commit(cls, value: object) -> str:
        raw = str(value)
        if not _GIT_OBJECT_ID.fullmatch(raw):
            raise ValueError("catalog_commit must be a lowercase full Git object ID")
        return raw

    @property
    def id(self) -> str:
        return content_id("verified-delegation-manifest", self.model_dump(mode="json"))


class CapabilityEvaluator(Protocol):
    def evaluate(self, request: CapabilityRequest) -> CapabilityDecision: ...


def _subject_matches(
    subject: CapabilitySubject,
    request: CapabilityRequest,
    *,
    repository: str,
    decision: Literal["allow", "deny"] = "allow",
) -> bool:
    if subject.repository != repository:
        return False
    if subject.refs != "*" and request.ref not in subject.refs:
        return False
    if subject.paths != "*" and request.path not in subject.paths:
        return False
    if (
        subject.bundle_sha256 != "*"
        and (
            request.bundle_sha256 is None
            or request.bundle_sha256 not in subject.bundle_sha256
        )
    ):
        return False
    return not (
        request.dirty
        and decision == "allow"
        and subject.refs != "*"
        and subject.paths == "*"
        and subject.bundle_sha256 == "*"
    )


def _subject_specificity(subject: CapabilitySubject) -> int:
    return (
        (0 if subject.bundle_sha256 == "*" else 4)
        + (0 if subject.paths == "*" else 2)
        + (0 if subject.refs == "*" else 1)
    )


def _resource_specificity(
    resources: CapabilityResources,
    capability: Capability,
) -> tuple[int, int]:
    if capability is Capability.TRUST_DELEGATE:
        values = (resources.delegated_repositories,)
    elif capability in {
        Capability.SOURCE_DISCOVER,
        Capability.SOURCE_FETCH,
        Capability.SOURCE_ARCHIVE,
        Capability.SOURCE_EXTRACT,
    }:
        values = (resources.hosts, resources.path_prefixes, resources.connectors)
    elif capability is Capability.MODEL_EXTERNAL:
        values = (resources.providers, resources.models, resources.data_classes)
    elif capability in {
        Capability.GIT_PULL_REQUEST,
        Capability.GIT_AUTO_MERGE,
        Capability.GIT_DIRECT_PUSH,
    }:
        values = (resources.git_refs,)
    else:
        values = ()
    constrained = sum(value != "*" and bool(value) for value in values)
    cardinality = sum(len(value) for value in values if value != "*")
    return constrained, -cardinality


def _bounded_values(
    child: tuple[str, ...] | Literal["*"],
    parent: tuple[str, ...] | Literal["*"],
    *,
    prefixes: bool = False,
) -> bool:
    if parent == "*":
        return True
    if child == "*":
        return False
    if not prefixes:
        return set(child).issubset(parent)
    return all(any(item.startswith(bound) for bound in parent) for item in child)


def _resources_narrow(
    child: CapabilityResources,
    parent: CapabilityResources,
) -> bool:
    fields = (
        (child.delegated_repositories, parent.delegated_repositories, False),
        (child.hosts, parent.hosts, False),
        (child.path_prefixes, parent.path_prefixes, True),
        (child.connectors, parent.connectors, False),
        (child.providers, parent.providers, False),
        (child.models, parent.models, False),
        (child.data_classes, parent.data_classes, False),
        (child.git_refs, parent.git_refs, False),
    )
    return all(
        _bounded_values(child_value, parent_value, prefixes=prefixes)
        for child_value, parent_value, prefixes in fields
    )


def _intersect_values(
    left: tuple[str, ...] | Literal["*"],
    right: tuple[str, ...] | Literal["*"],
    *,
    prefixes: bool = False,
) -> tuple[str, ...] | Literal["*"]:
    if left == "*":
        return right
    if right == "*":
        return left
    if not prefixes:
        return tuple(sorted(set(left).intersection(right)))
    selected: set[str] = set()
    for first in left:
        for second in right:
            if first.startswith(second):
                selected.add(first)
            elif second.startswith(first):
                selected.add(second)
    return tuple(sorted(selected))


def _selected(value: tuple[str, ...] | Literal["*"], item: str) -> bool:
    return value == "*" or item in value


def _resources_match(
    resources: CapabilityResources,
    capability: Capability,
    request: CapabilityRequest,
) -> bool:
    if capability is Capability.TRUST_DELEGATE:
        return _selected(resources.delegated_repositories, request.target_repository)
    if capability in {
        Capability.SOURCE_DISCOVER,
        Capability.SOURCE_FETCH,
        Capability.SOURCE_ARCHIVE,
        Capability.SOURCE_EXTRACT,
    }:
        if request.connector is None or request.host is None or request.target is None:
            return False
        try:
            canonical_target, derived_host = _source_target(request.target)
        except ValueError:
            return False
        if canonical_target != request.target or derived_host != request.host:
            return False
        if not _selected(resources.connectors, request.connector):
            return False
        if not _selected(resources.hosts, request.host):
            return False
        path = urlsplit(request.target).path
        prefixes = resources.path_prefixes
        return prefixes == "*" or any(
            prefix == "/" or path == prefix.rstrip("/") or path.startswith(f"{prefix.rstrip('/')}/")
            for prefix in prefixes
        )
    if capability is Capability.MODEL_EXTERNAL:
        if request.provider is None or request.model is None or request.data_class is None:
            return False
        return (
            _selected(resources.providers, request.provider)
            and _selected(resources.models, request.model)
            and _selected(resources.data_classes, request.data_class)
        )
    if capability in {
        Capability.GIT_PULL_REQUEST,
        Capability.GIT_AUTO_MERGE,
        Capability.GIT_DIRECT_PUSH,
    }:
        return _selected(resources.git_refs, request.ref)
    return True


def _intersect_resources(
    left: CapabilityResources,
    right: CapabilityResources,
) -> CapabilityResources:
    return CapabilityResources(
        delegated_repositories=_intersect_values(
            left.delegated_repositories, right.delegated_repositories
        ),
        hosts=_intersect_values(left.hosts, right.hosts),
        path_prefixes=_intersect_values(
            left.path_prefixes, right.path_prefixes, prefixes=True
        ),
        connectors=_intersect_values(left.connectors, right.connectors),
        providers=_intersect_values(left.providers, right.providers),
        models=_intersect_values(left.models, right.models),
        data_classes=_intersect_values(left.data_classes, right.data_classes),
        git_refs=_intersect_values(left.git_refs, right.git_refs),
    )


@dataclass(frozen=True)
class _EffectiveGrant:
    capabilities: frozenset[Capability]
    delegable_capabilities: frozenset[Capability]
    resources: CapabilityResources
    remaining_depth: int
    grant_ids: tuple[str, ...]
    manifest_ids: tuple[str, ...]
    manifest_sha256s: tuple[str, ...]
    catalog_commits: tuple[str, ...]
    chain: tuple[str, ...]


class DeterministicCapabilityEvaluator:
    """Resolve trusted local grants without deriving authority from requested data."""

    version = "2"

    def __init__(
        self,
        grants: Sequence[CapabilityGrant],
        manifests: Mapping[str, VerifiedDelegationManifest | Mapping[str, object]],
        *,
        clock: Callable[[], datetime],
        yolo: bool = False,
    ) -> None:
        self.grants = tuple(grants)
        normalized_manifests: dict[str, VerifiedDelegationManifest] = {}
        for repository, manifest_value in manifests.items():
            normalized = _repository_identity(repository, label="delegation repository")
            if normalized in normalized_manifests:
                raise ValueError("duplicate normalized delegation repository")
            try:
                manifest = VerifiedDelegationManifest.model_validate(manifest_value)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "delegation manifest requires verified sha256 and catalog commit context"
                ) from error
            if manifest.repository != normalized:
                raise ValueError("verified delegation repository does not match mapping key")
            normalized_manifests[normalized] = manifest
        self.manifests = normalized_manifests
        self.clock = clock
        self.yolo = yolo
        self._reject_manifest_cycles()

    def evaluate(self, request: CapabilityRequest) -> CapabilityDecision:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("capability evaluator clock must be timezone-aware")
        effective_states: list[_EffectiveGrant] = []
        for capability in request.capabilities:
            winner = self._winning_local(
                request,
                capability,
                repository=request.target_repository,
                now=now,
            )
            if winner is not None and winner.decision == "deny":
                return self._decision(
                    request,
                    allowed=False,
                    grants=(winner,),
                    now=now,
                    reason="winning target-local capability grant denies the request",
                )
            if winner is not None:
                effective_states.append(self._direct_state(winner))
                continue
            if request.authority_repository != request.target_repository:
                delegated = self._delegated_state(request, capability, now=now)
                if delegated is not None:
                    effective_states.append(delegated)
                    continue
            if self.yolo and capability is Capability.REPOSITORY_READ:
                effective_states.append(
                    _EffectiveGrant(
                        capabilities=frozenset((Capability.REPOSITORY_READ,)),
                        delegable_capabilities=frozenset(),
                        resources=CapabilityResources(),
                        remaining_depth=0,
                        grant_ids=(),
                        manifest_ids=(),
                        manifest_sha256s=(),
                        catalog_commits=(),
                        chain=(),
                    )
                )
                continue
            return self._decision(
                request,
                allowed=False,
                grants=(),
                now=now,
                reason="no matching local or delegated capability grant",
            )
        chains = {state.chain for state in effective_states if state.chain}
        if len(chains) > 1:
            return self._decision(
                request,
                allowed=False,
                grants=(),
                now=now,
                reason="requested capabilities require incompatible delegation chains",
            )
        effective_resources = CapabilityResources(
            delegated_repositories="*",
            hosts="*",
            path_prefixes="*",
            connectors="*",
            providers="*",
            models="*",
            data_classes="*",
            git_refs="*",
        )
        for state in effective_states:
            effective_resources = _intersect_resources(effective_resources, state.resources)
        if any(
            not _resources_match(effective_resources, capability, request)
            for capability in request.capabilities
        ):
            return self._decision(
                request,
                allowed=False,
                grants=(),
                now=now,
                reason="effective resource intersection does not authorize the request",
            )
        state_grant_ids = tuple(
            sorted({identifier for state in effective_states for identifier in state.grant_ids})
        )
        state_manifest_ids = tuple(
            sorted({identifier for state in effective_states for identifier in state.manifest_ids})
        )
        state_manifest_sha256s = tuple(
            sorted(
                {identifier for state in effective_states for identifier in state.manifest_sha256s}
            )
        )
        state_catalog_commits = tuple(
            sorted(
                {identifier for state in effective_states for identifier in state.catalog_commits}
            )
        )
        return CapabilityDecision(
            request=request,
            decision="allow",
            effective_capabilities=request.capabilities,
            grant_ids=state_grant_ids,
            delegation_chain=min(chains) if chains else (),
            reason=(
                "unresolved repository.read allowed for this invocation by --yolo"
                if not state_grant_ids
                else "all requested capabilities have deterministic authority"
            ),
            evaluator_version=self.version,
            decided_at=now,
            manifest_ids=state_manifest_ids,
            manifest_sha256s=state_manifest_sha256s,
            catalog_commits=state_catalog_commits,
            effective_resources=effective_resources,
            effective_remaining_depth=min(
                (state.remaining_depth for state in effective_states), default=0
            ),
        )

    def _direct_state(self, grant: CapabilityGrant) -> _EffectiveGrant:
        return _EffectiveGrant(
            capabilities=frozenset(grant.capabilities),
            delegable_capabilities=frozenset(grant.delegable_capabilities),
            resources=grant.resources,
            remaining_depth=grant.max_delegation_depth,
            grant_ids=(grant.id,),
            manifest_ids=(),
            manifest_sha256s=(),
            catalog_commits=(),
            chain=(),
        )

    def _delegated_state(
        self,
        request: CapabilityRequest,
        capability: Capability,
        *,
        now: datetime,
    ) -> _EffectiveGrant | None:
        root_candidates = tuple(
            grant
            for grant in self.grants
            if grant.decision == "allow"
            and (grant.expires_at is None or now < grant.expires_at)
            and _subject_matches(
                grant.subject,
                request,
                repository=request.authority_repository,
                decision=grant.decision,
            )
            and capability in grant.capabilities
            and Capability.TRUST_DELEGATE in grant.capabilities
            and capability in grant.delegable_capabilities
            and grant.max_delegation_depth > 0
        )
        origin_winner = self._winning_local(
            request,
            capability,
            repository=request.authority_repository,
            now=now,
        )
        delegate_winner = self._winning_local(
            request,
            Capability.TRUST_DELEGATE,
            repository=request.authority_repository,
            now=now,
        )
        if (
            origin_winner is None
            or delegate_winner is None
            or origin_winner.decision == "deny"
            or delegate_winner.decision == "deny"
        ):
            return None
        candidates: list[_EffectiveGrant] = []
        for grant in root_candidates:
            state = _EffectiveGrant(
                capabilities=frozenset(grant.capabilities),
                delegable_capabilities=frozenset(grant.delegable_capabilities),
                resources=grant.resources,
                remaining_depth=grant.max_delegation_depth,
                grant_ids=(grant.id,),
                manifest_ids=(),
                manifest_sha256s=(),
                catalog_commits=(),
                chain=(request.authority_repository,),
            )
            assert origin_winner is not None
            assert delegate_winner is not None
            state = self._intersect_local(state, origin_winner)
            state = self._intersect_local(state, delegate_winner)
            if capability in state.delegable_capabilities and _resources_match(
                state.resources, capability, request
            ):
                candidates.extend(self._walk(request, capability, state, now=now))
        return min(candidates, key=lambda item: item.chain) if candidates else None

    @staticmethod
    def _intersect_local(
        state: _EffectiveGrant,
        grant: CapabilityGrant,
    ) -> _EffectiveGrant:
        return _EffectiveGrant(
            capabilities=state.capabilities,
            delegable_capabilities=state.delegable_capabilities.intersection(
                grant.delegable_capabilities
            ),
            resources=_intersect_resources(state.resources, grant.resources),
            remaining_depth=min(state.remaining_depth, grant.max_delegation_depth),
            grant_ids=(*state.grant_ids, grant.id),
            manifest_ids=state.manifest_ids,
            manifest_sha256s=state.manifest_sha256s,
            catalog_commits=state.catalog_commits,
            chain=state.chain,
        )

    def _walk(
        self,
        request: CapabilityRequest,
        capability: Capability,
        state: _EffectiveGrant,
        *,
        now: datetime,
    ) -> tuple[_EffectiveGrant, ...]:
        parent = state.chain[-1]
        verified_manifest = self.manifests.get(parent)
        if verified_manifest is None or state.remaining_depth <= 0:
            return ()
        if Capability.TRUST_DELEGATE not in state.capabilities:
            return ()
        if capability not in state.delegable_capabilities:
            return ()
        candidates: list[_EffectiveGrant] = []
        for entry in verified_manifest.manifest.delegations:
            child = entry.subject.repository
            if child in state.chain:
                continue
            if not _selected(state.resources.delegated_repositories, child):
                continue
            if entry.expires_at is not None and now >= entry.expires_at:
                continue
            if not _subject_matches(entry.subject, request, repository=child):
                continue
            if not set(entry.capabilities).issubset(state.delegable_capabilities):
                continue
            if not set(entry.delegable_capabilities).issubset(state.delegable_capabilities):
                continue
            if not _resources_narrow(entry.resources, state.resources):
                continue
            next_state = _EffectiveGrant(
                capabilities=frozenset(entry.capabilities).intersection(
                    state.delegable_capabilities
                ),
                delegable_capabilities=(
                    frozenset(entry.delegable_capabilities)
                    .intersection(entry.capabilities)
                    .intersection(state.delegable_capabilities)
                ),
                resources=_intersect_resources(state.resources, entry.resources),
                remaining_depth=min(
                    state.remaining_depth - 1,
                    entry.max_delegation_depth,
                ),
                grant_ids=state.grant_ids,
                manifest_ids=(*state.manifest_ids, verified_manifest.id),
                manifest_sha256s=(
                    *state.manifest_sha256s,
                    verified_manifest.manifest_sha256,
                ),
                catalog_commits=(
                    *state.catalog_commits,
                    verified_manifest.catalog_commit,
                ),
                chain=(*state.chain, child),
            )
            if not _resources_match(next_state.resources, capability, request):
                continue
            local = self._winning_local(
                request,
                capability,
                repository=child,
                now=now,
            )
            if local is not None:
                if local.decision == "deny":
                    continue
                next_state = self._intersect_local(next_state, local)
                if not _resources_match(next_state.resources, capability, request):
                    continue
            if capability not in next_state.capabilities:
                continue
            if child == request.target_repository:
                candidates.append(next_state)
            else:
                local_delegate = self._winning_local(
                    request,
                    Capability.TRUST_DELEGATE,
                    repository=child,
                    now=now,
                )
                if local_delegate is not None:
                    if local_delegate.decision == "deny":
                        continue
                    next_state = self._intersect_local(next_state, local_delegate)
                    if not _resources_match(next_state.resources, capability, request):
                        continue
                if capability not in next_state.delegable_capabilities:
                    continue
                candidates.extend(self._walk(request, capability, next_state, now=now))
        return tuple(candidates)

    def _reject_manifest_cycles(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(repository: str) -> None:
            if repository in visiting:
                raise ValueError("delegation manifests contain a cycle or repeated identity")
            if repository in visited:
                return
            visiting.add(repository)
            verified_manifest = self.manifests.get(repository)
            if verified_manifest is not None:
                for entry in verified_manifest.manifest.delegations:
                    if entry.subject.repository in self.manifests:
                        visit(entry.subject.repository)
            visiting.remove(repository)
            visited.add(repository)

        for repository in sorted(self.manifests):
            visit(repository)

    def _winning_local(
        self,
        request: CapabilityRequest,
        capability: Capability,
        *,
        repository: str,
        now: datetime,
    ) -> CapabilityGrant | None:
        matching = tuple(
            grant
            for grant in self.grants
            if capability in grant.capabilities
            and (grant.expires_at is None or now < grant.expires_at)
            and _subject_matches(
                grant.subject,
                request,
                repository=repository,
                decision=grant.decision,
            )
            and _resources_match(grant.resources, capability, request)
        )
        if not matching:
            return None
        return max(
            matching,
            key=lambda grant: (
                _subject_specificity(grant.subject),
                _resource_specificity(grant.resources, capability),
                grant.decision == "deny",
                grant.id,
            ),
        )

    def _decision(
        self,
        request: CapabilityRequest,
        *,
        allowed: bool,
        grants: Sequence[CapabilityGrant],
        now: datetime,
        reason: str,
    ) -> CapabilityDecision:
        return CapabilityDecision(
            request=request,
            decision="allow" if allowed else "deny",
            effective_capabilities=request.capabilities if allowed else (),
            grant_ids=tuple(grant.id for grant in grants),
            reason=reason,
            evaluator_version=self.version,
            decided_at=now,
            effective_remaining_depth=(
                min((grant.max_delegation_depth for grant in grants), default=0)
                if allowed
                else 0
            ),
        )
