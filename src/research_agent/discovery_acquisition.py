from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.parse
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import Field

from research_agent.discovery import (
    AccessConstraint,
    AccessConstraintReason,
    AcquisitionAttempt,
    AcquisitionState,
    DiscoveryHit,
    identified,
)
from research_agent.models import StrictModel, utc_now
from research_agent.parsing import ParsedDocumentManager, ParsedIngestReceipt
from research_agent.remote_acquisition import PinnedHttpsFetcher, RemoteFetchError
from research_agent.store import ImmutableStore


class DiscoveryAcquisitionError(ValueError):
    pass


class JsonBytesTransport(Protocol):
    def get_json(self, url: str) -> dict[str, object]: ...


class PinnedJsonTransport:
    def __init__(self, fetcher: PinnedHttpsFetcher | None = None) -> None:
        self.fetcher = fetcher or PinnedHttpsFetcher(max_bytes=5_000_000)

    def get_json(self, url: str) -> dict[str, object]:
        fetched = self.fetcher.fetch(url)
        try:
            value = json.loads(fetched.content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise RemoteFetchError("official repository API returned invalid JSON") from None
        if not isinstance(value, dict):
            raise RemoteFetchError("official repository API returned a non-object")
        return value


class RepositorySnapshot(StrictModel):
    id: str
    discovery_hit_id: str
    repository: str
    canonical_locator: str
    api_locator: str
    default_branch: str
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    readme_path: str
    readme_blob_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_version_id: str
    source_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    license: str | None = None
    archived: bool
    fork: bool
    description: str | None = None
    observed_at: datetime
    connector_id: str = "connector:github-repository"


class AcquiredRepository(StrictModel):
    hit: DiscoveryHit
    snapshot: RepositorySnapshot
    parsed_ingest: ParsedIngestReceipt
    acquisition_attempt: AcquisitionAttempt


class DiscoveryAcquisitionReceipt(StrictModel):
    discovery_path: str
    considered_hits: int
    eligible_repositories: int
    acquired: tuple[AcquiredRepository, ...]
    access_constraints: tuple[AccessConstraint, ...]
    record_hashes: dict[str, tuple[str, ...]]
    connector_version: str


class GitHubDiscoveryAcquirer:
    """Resolve discovered GitHub repositories through immutable official API data."""

    version = "github-discovery-acquirer/1"
    connector_id = "connector:github-repository"
    _component = re.compile(r"^[A-Za-z0-9_.-]+$")
    _reserved_owners = frozenset(
        {
            "about",
            "apps",
            "collections",
            "customer-stories",
            "enterprise",
            "events",
            "features",
            "marketplace",
            "new",
            "organizations",
            "pricing",
            "search",
            "security",
            "settings",
            "site",
            "sponsors",
            "topics",
            "trending",
        }
    )

    def __init__(
        self,
        *,
        store: ImmutableStore,
        transport: JsonBytesTransport | None = None,
        parser: ParsedDocumentManager | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.transport = transport or PinnedJsonTransport()
        self.parser = parser or ParsedDocumentManager(store=store, clock=clock)
        self.clock = clock

    def acquire_file(
        self,
        discovery_path: Path,
        *,
        limit: int = 20,
    ) -> DiscoveryAcquisitionReceipt:
        if limit < 1 or limit > 100:
            raise DiscoveryAcquisitionError("repository acquisition limit must be 1..100")
        resolved = discovery_path.resolve(strict=True)
        try:
            payload = json.loads(resolved.read_bytes())
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise DiscoveryAcquisitionError("discovery output is not valid JSON") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
            raise DiscoveryAcquisitionError("discovery output must contain a hits array")
        try:
            hits = tuple(DiscoveryHit.model_validate(item) for item in payload["hits"])
        except ValueError:
            raise DiscoveryAcquisitionError("discovery output contains an invalid hit") from None

        return self.acquire_hits(
            hits,
            discovery_label=str(resolved),
            limit=limit,
        )

    def acquire_hits(
        self,
        hits: tuple[DiscoveryHit, ...],
        *,
        discovery_label: str,
        limit: int = 20,
    ) -> DiscoveryAcquisitionReceipt:
        """Acquire eligible repositories from transient discovery metadata."""
        if limit < 1 or limit > 100:
            raise DiscoveryAcquisitionError("repository acquisition limit must be 1..100")
        selected: list[tuple[DiscoveryHit, str, str]] = []
        seen: set[str] = set()
        for hit in hits:
            repository = self._repository(hit.canonical_locator)
            if repository is None:
                continue
            owner, name = repository
            key = f"{owner.casefold()}/{name.casefold()}"
            if key in seen:
                continue
            seen.add(key)
            selected.append((hit, owner, name))
            if len(selected) >= limit:
                break

        self.store.initialize()
        acquired: list[AcquiredRepository] = []
        constraints: list[AccessConstraint] = []
        hashes: dict[str, list[str]] = {}
        for hit, owner, name in selected:
            try:
                item = self._acquire(hit, owner, name)
            except (DiscoveryAcquisitionError, RemoteFetchError) as error:
                constraint = self._constraint(hit, error_type=type(error).__name__)
                constraints.append(constraint)
                hashes.setdefault("access-constraint", []).append(
                    self.store.put_record("access-constraint", constraint)
                )
                continue
            acquired.append(item)
            for kind, values in item.parsed_ingest.record_hashes.items():
                hashes.setdefault(kind, []).extend(values)
            hashes.setdefault("repository-snapshot", []).append(
                self.store.put_record("repository-snapshot", item.snapshot)
            )
            hashes.setdefault("acquisition-attempt", []).append(
                self.store.put_record("acquisition-attempt", item.acquisition_attempt)
            )
        return DiscoveryAcquisitionReceipt(
            discovery_path=discovery_label,
            considered_hits=len(hits),
            eligible_repositories=len(selected),
            acquired=tuple(acquired),
            access_constraints=tuple(constraints),
            record_hashes={
                kind: tuple(sorted(set(values))) for kind, values in sorted(hashes.items())
            },
            connector_version=self.version,
        )

    def _acquire(self, hit: DiscoveryHit, owner: str, name: str) -> AcquiredRepository:
        repository = f"{owner}/{name}"
        api = f"https://api.github.com/repos/{owner}/{name}"
        metadata = self.transport.get_json(api)
        canonical = self._required_string(metadata, "html_url")
        if self._repository(canonical) != (owner, name):
            raise DiscoveryAcquisitionError("official API repository identity mismatch")
        default_branch = self._required_string(metadata, "default_branch")
        commit = self.transport.get_json(
            f"{api}/commits/{urllib.parse.quote(default_branch, safe='')}"
        )
        commit_sha = self._required_string(commit, "sha").casefold()
        if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
            raise DiscoveryAcquisitionError("official API returned an invalid commit")
        readme = self.transport.get_json(f"{api}/readme?ref={commit_sha}")
        readme_path = self._required_string(readme, "path")
        readme_sha = self._required_string(readme, "sha").casefold()
        if (
            not re.fullmatch(r"[0-9a-f]{40}", readme_sha)
            or readme.get("encoding") != "base64"
            or not isinstance(readme.get("content"), str)
        ):
            raise DiscoveryAcquisitionError("official API returned invalid README metadata")
        try:
            encoded = "".join(readme["content"].split())
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise DiscoveryAcquisitionError("official API returned invalid README bytes") from None
        if hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest() != readme_sha:
            raise DiscoveryAcquisitionError("README bytes do not match the Git blob identity")

        license_value = metadata.get("license")
        spdx = (
            license_value.get("spdx_id")
            if isinstance(license_value, dict)
            and isinstance(license_value.get("spdx_id"), str)
            else None
        )
        if spdx in {"NOASSERTION", "OTHER"}:
            spdx = None
        raw = (
            f"https://raw.githubusercontent.com/{owner}/{name}/"
            f"{commit_sha}/{urllib.parse.quote(readme_path, safe='/')}"
        )
        observed_at = self.clock()
        parsed = self.parser.ingest(
            content,
            source_uri=raw,
            media_type="text/markdown",
            connector_id=self.connector_id,
            license=spdx,
            acquired_at=observed_at,
        )
        snapshot_fields = {
            "discovery_hit_id": hit.id,
            "repository": repository,
            "canonical_locator": canonical,
            "api_locator": api,
            "default_branch": default_branch,
            "commit_sha": commit_sha,
            "readme_path": readme_path,
            "readme_blob_sha": readme_sha,
            "source_version_id": parsed.derived_source_version_id,
            "source_content_sha256": self._source_digest(parsed.derived_source_version_id),
            "license": spdx,
            "archived": bool(metadata.get("archived", False)),
            "fork": bool(metadata.get("fork", False)),
            "description": (
                metadata["description"] if isinstance(metadata.get("description"), str) else None
            ),
            "observed_at": observed_at,
            "connector_id": self.connector_id,
        }
        snapshot = RepositorySnapshot(
            id=identified("repository-snapshot", snapshot_fields),
            **snapshot_fields,
        )
        attempt_fields = {
            "discovery_hit_id": hit.id,
            "resolved_locator": raw,
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "attempted_at": observed_at,
        }
        attempt = AcquisitionAttempt(
            id=identified("acquisition-attempt", attempt_fields),
            discovery_hit_id=hit.id,
            connector_id=self.connector_id,
            resolved_locator=raw,
            outcome="official_repository_readme_parsed",
            state=AcquisitionState.PARSED,
            attempted_at=observed_at,
            content_length=len(content),
            media_type="text/markdown",
            content_sha256=hashlib.sha256(content).hexdigest(),
            licensing_outcome=f"reported:{spdx}" if spdx else "unknown",
            policy_outcome="allow:official-api-immutable-revision",
        )
        return AcquiredRepository(
            hit=hit,
            snapshot=snapshot,
            parsed_ingest=parsed,
            acquisition_attempt=attempt,
        )

    def _source_digest(self, source_id: str) -> str:
        matches = [
            value["content_sha256"]
            for value in self.store.iter_records("source-version")
            if value.get("id") == source_id
        ]
        if len(matches) != 1:
            raise DiscoveryAcquisitionError("parsed source version is missing or ambiguous")
        return matches[0]

    def _constraint(self, hit: DiscoveryHit, *, error_type: str) -> AccessConstraint:
        observed_at = self.clock()
        fields = {
            "target_id": hit.id,
            "locator": hit.canonical_locator,
            "connector_id": self.connector_id,
            "observed_at": observed_at,
        }
        return AccessConstraint(
            id=identified("access-constraint", fields),
            target_id=hit.id,
            locator=hit.canonical_locator,
            reason=AccessConstraintReason.DENIED,
            observed_at=observed_at,
            connector_id=self.connector_id,
            lawful_alternatives=(hit.canonical_locator,),
            human_resolvable=True,
            detail=(
                "Official immutable repository README acquisition failed "
                f"({error_type}); exception text was not retained"
            ),
        )

    @classmethod
    def _repository(cls, value: str) -> tuple[str, str] | None:
        parsed = urllib.parse.urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or parsed.hostname != "github.com":
            return None
        parts = [urllib.parse.unquote(item) for item in parsed.path.split("/") if item]
        if len(parts) != 2 or parts[0].casefold() in cls._reserved_owners:
            return None
        owner, name = parts
        name = name.removesuffix(".git")
        if not all(cls._component.fullmatch(item) for item in (owner, name)):
            return None
        return owner, name

    @staticmethod
    def _required_string(value: dict[str, object], key: str) -> str:
        item = value.get(key)
        if not isinstance(item, str) or not item.strip():
            raise DiscoveryAcquisitionError(f"official API omitted {key}")
        return item.strip()
