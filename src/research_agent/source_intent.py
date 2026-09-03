"""Strict checked-in source declarations; declarations are never authority."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Literal, Protocol
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from pydantic import Field, field_validator, model_validator

from research_agent.capabilities import Capability, CapabilityDecision
from research_agent.models import StrictModel, content_id
from research_agent.source_work import SourceCheckpoint


class DiscoveryKind(StrEnum):
    DIRECT_URL = "direct_url"
    RSS_ATOM = "rss_atom"
    SITEMAP = "sitemap"
    HTTPS_HTML = "https_html"
    MOJEEK = "mojeek"
    GITHUB_REPOSITORY = "github_repository"


def _source_url(value: object) -> str:
    raw = str(value)
    if (
        "\\" in raw
        or re.search(r"%(?![0-9A-Fa-f]{2})", raw)
        or re.search(r"%(?:2f|5c)", raw, flags=re.IGNORECASE)
    ):
        raise ValueError("locator path is unsafe")
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.fragment
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise ValueError("locator must be a credential-free HTTPS URL on the default port")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        raise ValueError("locator must name a hostname, not an IP address")
    decoded_path = parsed.path
    for _ in range(4):
        if re.search(r"%(?:2f|5c)", decoded_path, flags=re.IGNORECASE):
            raise ValueError("locator path is unsafe")
        next_path = unquote(decoded_path)
        if next_path == decoded_path:
            break
        decoded_path = next_path
    else:
        raise ValueError("locator path has excessive percent encoding")
    if (
        not parsed.path.startswith("/")
        or "//" in decoded_path
        or "\\" in decoded_path
        or any(ord(character) < 32 or ord(character) == 127 for character in decoded_path)
        or any(segment in {".", ".."} for segment in decoded_path.split("/"))
    ):
        raise ValueError("locator path is unsafe")
    canonical_path = quote(decoded_path, safe="/:@!$&'()*+,;=-._~")
    if unquote(canonical_path) != decoded_path:
        raise ValueError("locator path is not canonically encodable")
    return urlunsplit(("https", parsed.hostname.lower(), canonical_path, parsed.query, ""))


def _ordered(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    if any(
        not value
        or value.strip() != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        for value in values
    ):
        raise ValueError(f"{label} must contain normalized non-empty strings")
    return tuple(sorted(set(values)))


def _host(value: object) -> str:
    raw = str(value).lower().rstrip(".")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", raw) or "." not in raw:
        raise ValueError("allowed host must be a normalized hostname")
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        return raw
    raise ValueError("allowed host must not be an IP address")


def _prefix(value: object) -> str:
    raw = str(value)
    if (
        not raw.startswith("/")
        or "//" in raw
        or "\\" in raw
        or "/../" in f"/{raw}/"
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise ValueError("allowed path prefix must be normalized and absolute")
    return raw


def _media_type(value: object) -> str:
    raw = str(value).casefold()
    if not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", raw):
        raise ValueError("media type must be a normalized type/subtype")
    return raw


def _document_pattern(value: object) -> str:
    raw = str(value)
    if (
        not raw.startswith("/")
        or "\\" in raw
        or "//" in raw
        or "/../" in f"/{raw}/"
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise ValueError("document pattern must be a normalized absolute glob")
    return raw


def _path_is_within(path: str, prefix: str) -> bool:
    if prefix == "/":
        return True
    root = prefix.rstrip("/")
    return path == root or path.startswith(f"{root}/")


def _matches_document_pattern(path: str, pattern: str) -> bool:
    """Glob each URL segment independently; `*` may not cross a slash."""
    path_parts = path.split("/")
    pattern_parts = pattern.split("/")
    return len(path_parts) == len(pattern_parts) and all(
        fnmatchcase(value, selector)
        for value, selector in zip(path_parts, pattern_parts, strict=True)
    )


class SourceDiscovery(StrictModel):
    version: Literal[1] = 1
    kind: DiscoveryKind
    locator: str

    @field_validator("locator", mode="before")
    @classmethod
    def normalize_locator(cls, value: object) -> str:
        return _source_url(value)


class SourceRefreshPolicy(StrictModel):
    version: Literal[1] = 1
    interval_seconds: int = Field(gt=0, le=31_536_000)
    max_items: int = Field(gt=0, le=10_000)
    max_depth: int = Field(ge=0, le=16)


class SourceAssociations(StrictModel):
    version: Literal[1] = 1
    concepts: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()

    @field_validator("concepts", "topics")
    @classmethod
    def normalize(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _ordered(value, label="associations")


class SourceTemporalPolicy(StrictModel):
    version: Literal[1] = 1
    field: Literal["published_at", "observed_at", "valid_at"]
    retention: Literal["append_only", "latest"]


class SourceIntent(StrictModel):
    version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,127}$")
    role: str = Field(min_length=1, max_length=128)
    discovery: SourceDiscovery
    allowed_hosts: tuple[str, ...] = Field(min_length=1)
    allowed_path_prefixes: tuple[str, ...] = Field(min_length=1)
    accepted_media_types: tuple[str, ...] = Field(min_length=1)
    document_patterns: tuple[str, ...] = ()
    refresh: SourceRefreshPolicy
    required: bool
    priority: int = Field(ge=0, le=1_000_000)
    associations: SourceAssociations
    temporal: SourceTemporalPolicy
    created_at: datetime

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def normalize_hosts(cls, value: object) -> tuple[str, ...]:
        return tuple(sorted({_host(item) for item in value}))  # type: ignore[arg-type]

    @field_validator("allowed_path_prefixes", mode="before")
    @classmethod
    def normalize_prefixes(cls, value: object) -> tuple[str, ...]:
        return tuple(sorted({_prefix(item) for item in value}))  # type: ignore[arg-type]

    @field_validator("accepted_media_types", mode="before")
    @classmethod
    def normalize_media_types(cls, value: object) -> tuple[str, ...]:
        return tuple(sorted({_media_type(item) for item in value}))  # type: ignore[arg-type]

    @field_validator("document_patterns", mode="before")
    @classmethod
    def normalize_document_patterns(cls, value: object) -> tuple[str, ...]:
        return tuple(sorted({_document_pattern(item) for item in value}))  # type: ignore[arg-type]

    @field_validator("created_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def discovery_stays_within_declared_scope(self) -> SourceIntent:
        parsed = urlsplit(self.discovery.locator)
        if parsed.hostname not in self.allowed_hosts:
            raise ValueError("discovery locator host must be in allowed_hosts")
        if not any(_path_is_within(parsed.path, prefix) for prefix in self.allowed_path_prefixes):
            raise ValueError("discovery locator path must be in allowed_path_prefixes")
        return self

    @property
    def canonical_id(self) -> str:
        return content_id("source-intent", self.model_dump(mode="json"))

    def permits_locator(self, locator: str) -> bool:
        """Return whether a normalized candidate remains inside this declaration."""
        normalized = _source_url(locator)
        parsed = urlsplit(normalized)
        return parsed.hostname in self.allowed_hosts and any(
            _path_is_within(parsed.path, prefix) for prefix in self.allowed_path_prefixes
        )


class SourceCandidate(StrictModel):
    version: Literal[1] = 1
    intent_id: str
    locator: str
    media_type: str | None = None
    upstream_id: str | None = None
    discovered_at: datetime

    @field_validator("locator", mode="before")
    @classmethod
    def normalize_locator(cls, value: object) -> str:
        return _source_url(value)

    @field_validator("media_type", mode="before")
    @classmethod
    def normalize_media_type(cls, value: object) -> str | None:
        return None if value is None else _media_type(value)

    @field_validator("discovered_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @property
    def id(self) -> str:
        return content_id("source-candidate", self.model_dump(mode="json"))


class SourceAuthorizationError(ValueError):
    """A source declaration did not authorize a discovered child URL."""


def authorize_candidate(
    candidate: SourceCandidate, authority: SourceIntent | CapabilityDecision
) -> SourceCandidate:
    """Fail closed when an enumerated child escapes its checked-in source intent."""
    if isinstance(authority, CapabilityDecision):
        request = authority.request
        if authority.decision != "allow" or not {
            Capability.SOURCE_DISCOVER,
            Capability.SOURCE_FETCH,
        }.intersection(authority.effective_capabilities):
            raise SourceAuthorizationError(
                "candidate capability decision does not authorize source access"
            )
        if request.host is None or urlsplit(candidate.locator).hostname != request.host:
            raise SourceAuthorizationError("candidate host is outside the capability decision")
        return candidate
    if candidate.intent_id != authority.id:
        raise SourceAuthorizationError("candidate intent does not match its authority")
    parsed = urlsplit(candidate.locator)
    if parsed.hostname not in authority.allowed_hosts:
        raise SourceAuthorizationError("candidate host is outside the authorized source intent")
    if not any(_path_is_within(parsed.path, prefix) for prefix in authority.allowed_path_prefixes):
        raise SourceAuthorizationError("candidate path is outside the authorized source intent")
    if authority.document_patterns and not any(
        _matches_document_pattern(parsed.path, pattern) for pattern in authority.document_patterns
    ):
        raise SourceAuthorizationError(
            "candidate path does not match an authorized document pattern"
        )
    if (
        candidate.media_type is not None
        and candidate.media_type.casefold() not in authority.accepted_media_types
    ):
        raise SourceAuthorizationError(
            "candidate media type is outside the authorized source intent"
        )
    return candidate


class SourceAdapter(Protocol):
    adapter_id: str
    version: str

    def discover(self, intent: SourceIntent) -> tuple[SourceCandidate, ...]: ...

    def fetch(
        self, candidate: SourceCandidate, *, prior: SourceCheckpoint | None
    ) -> SourceCheckpoint: ...
