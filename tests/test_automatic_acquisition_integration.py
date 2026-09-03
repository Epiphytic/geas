"""Offline vertical-slice contract for automatic ontology acquisition."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

import research_agent.cli as cli
from research_agent.agent_skills import (
    GeasIdentity,
    OntologyIdentity,
    PortableArtifactIdentity,
    ProjectionIdentity,
    SkillFile,
    SkillIdentity,
    SkillManifest,
    canonical_manifest_bytes,
    export_skill,
    snapshot_digest,
)
from research_agent.bootstrap_models import (
    BootstrapPhase,
    ManagedPath,
    RepositoryBootstrapRequest,
    VerifiedRepositoryBootstrap,
)
from research_agent.bundles import KnowledgeBundle
from research_agent.capabilities import (
    Capability,
    CapabilityDecision,
    CapabilityGrant,
    CapabilityRequest,
    CapabilityResources,
    CapabilitySubject,
    DeterministicCapabilityEvaluator,
    VerifiedDelegationManifest,
)
from research_agent.library import SourceLibraryBuilder, SourceLibraryManifest
from research_agent.ontology_build import OntologyBuildConfig
from research_agent.publishing import (
    PathRole,
    PublicationManifest,
    PublicationManifestPath,
    PublicationProducer,
    PublishMode,
    PublishPath,
    PublishRequest,
)
from research_agent.remote_acquisition import (
    ConditionalHttpResponse,
    ConditionalHttpsTransport,
    SourceFetchRequest,
)
from research_agent.repository_bootstrap import BootstrapOperation, RepositoryBootstrapManager
from research_agent.repository_catalog import resolve_repository_catalog, verify_catalog
from research_agent.repository_publisher import GitRepositoryPublisher
from research_agent.source_intent import SourceCandidate, SourceIntent
from research_agent.source_work import (
    FetchedSourcePayload,
    ImmutableSourceWorkStore,
    SourceAuthorityContext,
    SourceCheckpoint,
    SourceRetentionDecision,
    SourceRetentionRequest,
    SourceWorkCoordinator,
    SourceWorkInterruption,
    SourceWorkOutcome,
    SourceWorkPhase,
)
from research_agent.store import ImmutableStore

FIXTURE = Path(__file__).parent / "fixtures" / "automatic_acquisition" / "gold"
NOW = datetime(2026, 9, 3, 12, tzinfo=UTC)
ROOT_REPOSITORY = "https://github.com/example/aurora-gold-ontology"
SOURCE_REPOSITORY = "https://github.com/example/aurora-gold-sources"
BUNDLE_SHA256 = "ec438e2de8e178cc997b5ab69308bcc8547a51a5f2cce84f18282a56fe206eab"
FEED_URL = "https://aurora-gold.example.test/news/feed.xml"
NEWS_URL = "https://aurora-gold.example.test/news/production-update.html"
REGULATORY_URL = "https://sedar-plus.example.test/filings/ni-43-101.html"
FINANCIAL_URL = "https://aurora-gold.example.test/financials/q2-2026.json"
CONSTRAINED_URL = "https://sedar-plus.example.test/filings/pending.html"


def _git(cwd: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Geas Gold Fixture",
            "GIT_AUTHOR_EMAIL": "gold-fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Geas Gold Fixture",
            "GIT_COMMITTER_EMAIL": "gold-fixture@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        },
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _local_bare_repository(tmp_path: Path) -> tuple[Path, Path, str]:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    upstream = tmp_path / "upstream"
    _git(tmp_path, "init", "--initial-branch=main", str(upstream))
    shutil.copytree(FIXTURE, upstream, dirs_exist_ok=True)
    _git(upstream, "remote", "add", "origin", str(remote))
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "seed offline Aurora Gold ontology")
    _git(upstream, "push", "--set-upstream", "origin", "main")
    _git(upstream, "remote", "set-url", "origin", ROOT_REPOSITORY)
    return remote, upstream, _git(upstream, "rev-parse", "HEAD")


class _AllowEvaluator:
    def __init__(self) -> None:
        self.requests: list[CapabilityRequest] = []

    def evaluate(self, request: CapabilityRequest) -> CapabilityDecision:
        self.requests.append(request)
        return CapabilityDecision(
            request=request,
            decision="allow",
            effective_capabilities=request.capabilities,
            reason="offline fixture grant",
            evaluator_version="gold-fixture/1",
            decided_at=request.requested_at,
        )


class _RecordingHttpClient:
    def __init__(self, responses: dict[str, list[ConditionalHttpResponse]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def request(self, **arguments: object) -> ConditionalHttpResponse:
        self.calls.append(arguments)
        url = str(arguments["url"])
        try:
            return self.responses[url].pop(0)
        except (KeyError, IndexError) as error:
            raise AssertionError(f"unexpected offline HTTP request: {url}") from error


class _FixtureRetentionPolicy:
    """Trusted test policy; response license strings do not grant storage rights."""

    def evaluate(self, request: SourceRetentionRequest) -> SourceRetentionDecision:
        request_id = request.id
        return SourceRetentionDecision(
            request_id=request_id,
            decision="allow",
            storage_rights="trusted-policy:checked-in-offline-fixture",
            reason="test suite owns these fixture bytes",
            policy_version="gold-fixture-retention/1",
        )


class _FixtureModel:
    validator_version = "gold-fixture-extraction/1"
    external = False

    def __init__(self, store: ImmutableStore) -> None:
        self.store = store
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    def propose(
        self,
        *,
        source_version_id: str,
        structural_derivation_id: str,
        anchor_ids: tuple[str, ...],
    ) -> object:
        self.calls.append((source_version_id, structural_derivation_id, anchor_ids))
        values = {
            "version": 1,
            "source_version_id": source_version_id,
            "structural_derivation_id": structural_derivation_id,
            "anchor_ids": anchor_ids,
            "review_state": "proposed",
            "commit_authority": "none_proposal_only",
            "model": "fixture-local",
        }
        digest = self.store.put_record("extraction-proposal", values)
        return SimpleNamespace(id=f"extraction-proposal:sha256:{digest}")


class _ScenarioAdapter:
    adapter_id = "source:gold-fixture"
    version = "1"
    max_discovery_requests = 0
    max_fetch_requests = 1

    def __init__(self, calls: list[tuple[str, SourceCheckpoint | None]]) -> None:
        self.calls = calls
        self.news_fetches = sum(locator == NEWS_URL for locator, _prior in calls)

    def discover(self, intent: SourceIntent) -> tuple[SourceCandidate, ...]:
        locators = {
            "aurora-constrained": CONSTRAINED_URL,
            "aurora-financials": FINANCIAL_URL,
            "aurora-news": NEWS_URL,
            "aurora-regulatory": REGULATORY_URL,
        }
        return (
            SourceCandidate(
                intent_id=intent.id,
                locator=locators[intent.id],
                discovered_at=NOW,
            ),
        )

    def fetch(
        self,
        candidate: SourceCandidate,
        *,
        prior: SourceCheckpoint | None,
    ) -> SourceCheckpoint:
        self.calls.append((candidate.locator, prior))
        if candidate.locator == CONSTRAINED_URL:
            return SourceCheckpoint(
                work_item_id=candidate.id,
                phase=SourceWorkPhase.ACCESS_CONSTRAINED,
                constraint="rate_limited",
                retry_after=60,
                request_count=1,
                recorded_at=NOW,
            )
        if candidate.locator == NEWS_URL and self.news_fetches:
            assert prior is not None
            assert prior.etag == '"news-v1"'
            assert prior.prior_source_version_id is not None
            return SourceCheckpoint(
                work_item_id=candidate.id,
                phase=SourceWorkPhase.NOT_MODIFIED,
                etag='"news-v1"',
                last_modified="Sat, 15 Aug 2026 12:00:00 GMT",
                request_count=1,
                recorded_at=NOW + timedelta(seconds=901),
            )
        if candidate.locator == NEWS_URL:
            self.news_fetches += 1
        content = _source_bytes(candidate.locator)
        return SourceCheckpoint(
            work_item_id=candidate.id,
            phase=SourceWorkPhase.FETCHED,
            result_sha256=hashlib.sha256(content).hexdigest(),
            etag='"news-v1"' if candidate.locator == NEWS_URL else None,
            last_modified=(
                "Sat, 15 Aug 2026 12:00:00 GMT"
                if candidate.locator == NEWS_URL
                else None
            ),
            request_count=1,
            recorded_at=NOW,
        )

    def payload(
        self,
        candidate: SourceCandidate,
        _checkpoint: SourceCheckpoint,
    ) -> FetchedSourcePayload:
        media_type = "application/json" if candidate.locator == FINANCIAL_URL else "text/html"
        return FetchedSourcePayload(
            content=_source_bytes(candidate.locator),
            source_uri=candidate.locator,
            media_type=media_type,
            connector_id=self.adapter_id,
            license=None,
            observed_at=NOW,
            published_at=NOW - timedelta(days=1),
            valid_at=NOW - timedelta(days=1),
        )


def _source_bytes(locator: str) -> bytes:
    names = {
        NEWS_URL: "news.html",
        REGULATORY_URL: "regulatory-filing.html",
        FINANCIAL_URL: "financials.json",
    }
    return (FIXTURE / "http" / names[locator]).read_bytes()


def _source_request(
    intent: SourceIntent,
    candidate: SourceCandidate,
    capabilities: tuple[Capability, ...],
    now: datetime,
) -> CapabilityRequest:
    return CapabilityRequest(
        authority_repository=ROOT_REPOSITORY,
        target_repository=SOURCE_REPOSITORY,
        capabilities=capabilities,
        ref="refs/heads/main",
        path="sources/aurora-gold",
        bundle_sha256=BUNDLE_SHA256,
        connector="source:gold-fixture",
        host=urlsplit(candidate.locator).hostname,
        target=candidate.locator,
        requested_at=now,
    )


def _coordinator(
    root: Path,
    *,
    adapter: _ScenarioAdapter,
    evaluator: _AllowEvaluator,
    model: _FixtureModel,
    after_phase: object = None,
) -> SourceWorkCoordinator:
    store = model.store
    return SourceWorkCoordinator(
        store=store,
        work_store=ImmutableSourceWorkStore(store),
        adapter=adapter,
        capability_evaluator=evaluator,
        capability_request=_source_request,
        authority=SourceAuthorityContext(
            authority_repository=ROOT_REPOSITORY,
            target_repository=SOURCE_REPOSITORY,
            ref="refs/heads/main",
            path="sources/aurora-gold",
        ),
        ontology_bundle_sha256=BUNDLE_SHA256,
        library_manifest=SourceLibraryManifest.from_yaml(FIXTURE / "ontology/library.yaml"),
        library_database=root / "library.sqlite",
        extraction=model,
        retention_policy=_FixtureRetentionPolicy(),
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        after_phase=after_phase,  # type: ignore[arg-type]
    )


def _delegated_decision(repository: Path, commit: str) -> CapabilityDecision:
    catalog = resolve_repository_catalog(repository)
    assert catalog.delegation_manifest is not None
    assert catalog.delegation_manifest_sha256 is not None
    grant = CapabilityGrant(
        decision="allow",
        subject=CapabilitySubject(
            repository=ROOT_REPOSITORY,
            refs=("refs/heads/main",),
            paths="*",
            bundle_sha256="*",
        ),
        capabilities=(
            Capability.REPOSITORY_READ,
            Capability.SOURCE_ARCHIVE,
            Capability.SOURCE_DISCOVER,
            Capability.SOURCE_EXTRACT,
            Capability.SOURCE_FETCH,
            Capability.TRUST_DELEGATE,
        ),
        delegable_capabilities=(
            Capability.SOURCE_ARCHIVE,
            Capability.SOURCE_DISCOVER,
            Capability.SOURCE_EXTRACT,
            Capability.SOURCE_FETCH,
        ),
        resources=CapabilityResources(
            delegated_repositories=(SOURCE_REPOSITORY,),
            hosts=("aurora-gold.example.test", "sedar-plus.example.test"),
            path_prefixes=("/filings/", "/financials/", "/news/"),
            connectors=("direct-https", "rss-atom"),
        ),
        max_delegation_depth=1,
        expires_at=None,
        created_at=NOW,
        created_via="manual",
    )
    evaluator = DeterministicCapabilityEvaluator(
        (grant,),
        {
            ROOT_REPOSITORY: VerifiedDelegationManifest(
                repository=ROOT_REPOSITORY,
                manifest=catalog.delegation_manifest,
                manifest_sha256=catalog.delegation_manifest_sha256,
                catalog_commit=commit,
            )
        },
        clock=lambda: NOW,
    )
    return evaluator.evaluate(
        CapabilityRequest(
            authority_repository=ROOT_REPOSITORY,
            target_repository=SOURCE_REPOSITORY,
            capabilities=(Capability.SOURCE_FETCH,),
            ref="refs/heads/main",
            path="sources/aurora-gold",
            connector="direct-https",
            host="sedar-plus.example.test",
            target=REGULATORY_URL,
            requested_at=NOW,
        )
    )


def _skill_files(commit: str) -> dict[Path, bytes]:
    content = {
        Path("SKILL.md"): (
            b"# Aurora Gold\n\nRead `references/index.md`; generated knowledge is not "
            b"canonical ontology truth.\n"
        ),
        Path("references/index.md"): (
            b"# Aurora Gold fixture index\n\nExact evidence remains in the Geas source library.\n"
        ),
    }
    inventory = tuple(
        SkillFile(path=path.as_posix(), sha256=hashlib.sha256(value).hexdigest())
        for path, value in sorted(content.items(), key=lambda item: item[0].as_posix())
    )
    manifest = SkillManifest(
        format_version=2,
        skill=SkillIdentity(name="aurora-gold"),
        ontology=OntologyIdentity(
            name="aurora-gold",
            repository_url=ROOT_REPOSITORY,
            branch="main",
            commit=commit,
            active_ref="refs/heads/main",
            ontology_commit=commit,
            subscription_name="aurora-gold",
            catalog_path="geas.yaml",
            ontology_path="ontology",
            bundle_sha256=BUNDLE_SHA256,
        ),
        geas=GeasIdentity(
            project_url="https://github.com/Epiphytic/geas",
            version="0.1.0",
            commit=None,
        ),
        projection=ProjectionIdentity(
            snapshot_id="truth-snapshot:sha256:" + "a" * 64,
            topic_concept_id="concept:aurora-gold",
        ),
        artifact=PortableArtifactIdentity(
            role="knowledge-projection",
            content_sha256="b" * 64,
            input_revision="c" * 64,
        ),
        files=inventory,
        snapshot_sha256=snapshot_digest(inventory),
    )
    return {**content, Path("geas-skill.json"): canonical_manifest_bytes(manifest)}


def _managed_file(root: Path, path: str, *, role: str) -> ManagedPath:
    return ManagedPath(
        path=path,
        sha256=hashlib.sha256((root / path).read_bytes()).hexdigest(),
        role=role,
    )


class _Forge:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def upsert_pull_request(self, **values: str) -> str:
        self.calls.append(values)
        return "https://github.com/example/aurora-gold-ontology/pull/7"

    def enable_auto_merge(self, **_values: str) -> None:
        raise AssertionError("pull-request default must not enable auto-merge")


class _ReceiptVerifier:
    def __init__(self, expected: PublicationManifest) -> None:
        self.expected = expected

    def verify(self, manifest: PublicationManifest) -> None:
        assert manifest == self.expected


class _LocalRemoteTransport:
    def __init__(self, worktree: Path, remote: Path) -> None:
        self.worktree = worktree
        self.remote = remote

    def ls_remote(self, *, endpoint: str, ref: str) -> str:
        assert endpoint == ROOT_REPOSITORY
        return _git(
            self.remote,
            "for-each-ref",
            "--format=%(objectname)%09%(refname)",
            ref,
        )

    def push(
        self,
        *,
        endpoint: str,
        commit: str,
        ref: str,
        expected: str | None,
    ) -> subprocess.CompletedProcess[str]:
        assert endpoint == ROOT_REPOSITORY
        return subprocess.run(
            (
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "push",
                f"--force-with-lease={ref}:{expected or ''}",
                str(self.remote),
                f"{commit}:{ref}",
            ),
            cwd=self.worktree,
            check=False,
            capture_output=True,
            text=True,
        )
def test_canadian_gold_miner_automatic_acquisition_is_offline_resumable_and_owned(
    tmp_path: Path,
) -> None:
    """Catches any missing cross-stream handoff in the complete operator workflow."""
    remote, upstream, commit = _local_bare_repository(tmp_path)
    verified = verify_catalog(upstream / "geas.yaml")
    config = OntologyBuildConfig.from_yaml(upstream / "ontology/build.yaml")
    library = SourceLibraryManifest.from_yaml(upstream / "ontology/library.yaml")
    bundle = KnowledgeBundle.from_yaml(upstream / "ontology/bundle.yaml")

    assert len(verified) == 1
    assert bundle.topic_concept_id == "concept:aurora-gold"
    assert tuple(item.id for item in config.source_intent) == (
        "aurora-constrained",
        "aurora-financials",
        "aurora-news",
        "aurora-regulatory",
    )
    assert library.include_all_parsed_sources is True
    assert _source_bytes(NEWS_URL) != _source_bytes(REGULATORY_URL)
    assert (upstream / "geas-delegations.yaml").read_bytes() == (
        upstream / "ontology/geas-delegations.yaml"
    ).read_bytes()

    delegated = _delegated_decision(upstream, commit)
    assert delegated.allowed
    assert delegated.delegation_chain == (ROOT_REPOSITORY, SOURCE_REPOSITORY)
    assert delegated.effective_remaining_depth == 0

    # The real bounded transport runs over an injected DNS resolver and response
    # queue. No socket, credential, wall clock, live model, or live forge is used.
    transport_evaluator = _AllowEvaluator()
    client = _RecordingHttpClient(
        {
            FEED_URL: [
                ConditionalHttpResponse(
                    status=200,
                    headers={"Content-Type": "application/rss+xml"},
                    body=(FIXTURE / "http/issuer-feed.xml").read_bytes(),
                )
            ],
            NEWS_URL: [
                ConditionalHttpResponse(
                    status=200,
                    headers={"Content-Type": "text/html"},
                    body=_source_bytes(NEWS_URL),
                )
            ],
            FINANCIAL_URL: [
                ConditionalHttpResponse(
                    status=200,
                    headers={"Content-Type": "application/json"},
                    body=_source_bytes(FINANCIAL_URL),
                )
            ],
            REGULATORY_URL: [
                ConditionalHttpResponse(
                    status=200,
                    headers={"Content-Type": "text/html", "ETag": '"filing-v1"'},
                    body=_source_bytes(REGULATORY_URL),
                ),
                ConditionalHttpResponse(status=304, headers={"ETag": '"filing-v1"'}),
            ],
            CONSTRAINED_URL: [
                ConditionalHttpResponse(status=429, headers={"Retry-After": "60"})
            ],
        }
    )
    transport = ConditionalHttpsTransport(
        dns_resolver=lambda _host: ("8.8.8.8", "2001:4860:4860::8888"),
        http_client=client,
        capability_evaluator=transport_evaluator,
        clock=lambda: NOW,
    )
    public_documents = (
        (
            FEED_URL,
            ("aurora-gold.example.test",),
            ("/news/",),
            ("application/rss+xml",),
        ),
        (
            NEWS_URL,
            ("aurora-gold.example.test",),
            ("/news/",),
            ("text/html",),
        ),
        (
            FINANCIAL_URL,
            ("aurora-gold.example.test",),
            ("/financials/",),
            ("application/json",),
        ),
    )
    transported = tuple(
        transport.fetch(
            SourceFetchRequest(
                locator=locator,
                allowed_hosts=hosts,
                allowed_path_prefixes=prefixes,
                accepted_media_types=media_types,
                capability_request=delegated.request,
            )
        )
        for locator, hosts, prefixes, media_types in public_documents
    )
    assert tuple(item.status for item in transported) == (200, 200, 200)
    assert tuple(item.media_type for item in transported) == (
        "application/rss+xml",
        "text/html",
        "application/json",
    )
    transport_request = SourceFetchRequest(
        locator=REGULATORY_URL,
        allowed_hosts=("sedar-plus.example.test",),
        allowed_path_prefixes=("/filings/",),
        accepted_media_types=("text/html",),
        capability_request=delegated.request,
    )
    fetched = transport.fetch(transport_request)
    unchanged = transport.fetch(transport_request, prior=fetched.validator)
    constrained = transport.fetch(
        transport_request.model_copy(update={"locator": CONSTRAINED_URL})
    )
    assert unchanged.status == 304
    assert client.calls[4]["headers"] == {
        "accept-encoding": "gzip, deflate",
        "if-none-match": '"filing-v1"',
    }
    assert constrained.constraint is not None
    assert constrained.constraint.value == "rate_limited"

    state = tmp_path / "source-state"
    store = ImmutableStore(state)
    model = _FixtureModel(store)
    source_calls: list[tuple[str, SourceCheckpoint | None]] = []
    evaluator = _AllowEvaluator()
    interrupted = False

    def stop_after_first_archive(phase: SourceWorkPhase) -> None:
        nonlocal interrupted
        if phase is SourceWorkPhase.ARCHIVED and not interrupted:
            interrupted = True
            raise SourceWorkInterruption("fixture interruption after durable archive")

    with pytest.raises(SourceWorkInterruption, match="durable archive"):
        _coordinator(
            state,
            adapter=_ScenarioAdapter(source_calls),
            evaluator=evaluator,
            model=model,
            after_phase=stop_after_first_archive,
        ).run_due(config.source_intent, now=NOW)

    calls_before_resume = tuple(locator for locator, _prior in source_calls)
    receipt = _coordinator(
        state,
        adapter=_ScenarioAdapter(source_calls),
        evaluator=evaluator,
        model=model,
    ).run_due(config.source_intent, now=NOW)
    assert receipt.complete, receipt.model_dump(mode="json")
    assert len(receipt.source_version_ids) == 3
    assert calls_before_resume.count(FINANCIAL_URL) == 1
    assert tuple(locator for locator, _prior in source_calls).count(FINANCIAL_URL) == 1
    assert SourceWorkOutcome.CONSTRAINED_OPTIONAL in receipt.semantic_outcomes
    assert len(tuple(store.iter_records("extraction-proposal"))) >= 2
    assert tuple(store.iter_records("claim")) == ()

    first_source_count = len(tuple(store.iter_records("source-version")))
    first_model_count = len(model.calls)
    refresh = _coordinator(
        state,
        adapter=_ScenarioAdapter(source_calls),
        evaluator=evaluator,
        model=model,
    ).run_due(
        tuple(item for item in config.source_intent if item.id == "aurora-news"),
        now=NOW + timedelta(seconds=901),
    )
    assert refresh.complete
    assert SourceWorkPhase.NOT_MODIFIED in refresh.completed_phases
    assert len(tuple(store.iter_records("source-version"))) == first_source_count
    assert len(model.calls) == first_model_count

    rebuilt = SourceLibraryBuilder(store=store, clock=lambda: NOW).build(
        library, state / "library-rebuilt.sqlite"
    )
    anchors = tuple(store.iter_records("structural-anchor"))
    exact_text = {
        store.read_blob(value["source_content_sha256"])[value["start"] : value["end"]]
        for value in anchors
        if value["kind"] in {"paragraph", "block"}
    }
    assert rebuilt.source_count == 3
    assert any(b"51,200 ounces" in value for value in exact_text)
    assert any(b"1.8 million ounces" in value for value in exact_text)
    assert any(b"sustaining_cost_per_ounce" in value for value in exact_text)

    # Task 7 is intentionally absent from this branch. This is the first
    # cross-stream fan-in boundary after the existing services above prove green.
    install = cli._build_parser().parse_args(
        [
            "repository-install",
            "aurora-gold",
            ROOT_REPOSITORY,
            "--ref",
            "refs/heads/main",
            "--trust-repository",
            "--link",
        ]
    )
    assert install.publish == "pull-request"
    assert install.direct_push is False

    sentinel = upstream / "operator-notes.txt"
    sentinel.write_text("unrelated operator state must survive\n")
    trust_events: list[str] = []
    subscription_path = ".geas/subscriptions/aurora-gold.json"

    def subscribe(_operation: BootstrapOperation) -> tuple[ManagedPath, ...]:
        target = upstream / subscription_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"name":"aurora-gold"}\n')
        return (_managed_file(upstream, subscription_path, role="manifest"),)

    def export_catalog(_operation: BootstrapOperation) -> tuple[ManagedPath, ...]:
        exported = export_skill(
            _skill_files(commit),
            config_root=tmp_path / "config",
            home=tmp_path / "home",
            repository=upstream,
            link=False,
            force=False,
            which=lambda _name: None,
        )
        return tuple(
            _managed_file(
                upstream,
                path.relative_to(upstream).as_posix(),
                role="skill",
            )
            for path in sorted(exported.path.rglob("*"))
            if path.is_file()
        )

    def remove_paths(operation: BootstrapOperation) -> None:
        for owned in operation.owned_paths:
            target = upstream / owned.path
            if target.exists() and target.is_file():
                target.unlink()
        for relative in (
            ".agents/skills/aurora-gold/references",
            ".agents/skills/aurora-gold",
        ):
            directory = upstream / relative
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()

    bootstrap_request = RepositoryBootstrapRequest(
        name="aurora-gold",
        repository=ROOT_REPOSITORY,
        ref="refs/heads/main",
        catalog="geas.yaml",
        commit_sha256=commit,
        trust="trust_repository",
        delegate_depth=1,
        ontology_paths=("ontology",),
        bundle_sha256=(BUNDLE_SHA256,),
        source_hosts=("aurora-gold.example.test", "sedar-plus.example.test"),
        source_path_prefixes=("/filings/", "/financials/", "/news/"),
        source_connectors=("direct-https", "rss-atom"),
        delegated_repositories=(SOURCE_REPOSITORY,),
    )
    verified_bootstrap = VerifiedRepositoryBootstrap(
        repository=ROOT_REPOSITORY,
        ref="refs/heads/main",
        catalog="geas.yaml",
        commit_sha256=commit,
        ontology_paths=("ontology",),
        bundle_sha256=(BUNDLE_SHA256,),
        source_hosts=("aurora-gold.example.test", "sedar-plus.example.test"),
        source_path_prefixes=("/filings/", "/financials/", "/news/"),
        source_connectors=("direct-https", "rss-atom"),
        delegated_repositories=(SOURCE_REPOSITORY,),
    )
    bootstrap = RepositoryBootstrapManager(
        root=upstream,
        announce=lambda _message: None,
        now=lambda: NOW,
        verify=lambda _request: verified_bootstrap,
        record_trust=lambda _operation, grant: trust_events.append(grant.id),
        subscribe=subscribe,
        hydrate_artifacts=lambda _operation: (),
        install_generic_skill=lambda _operation: (),
        export_catalog_skills=export_catalog,
        link_agents=lambda _operation: (),
        remove_trust=lambda _operation, grant: trust_events.remove(grant.id),
        unsubscribe=remove_paths,
        remove_skills=remove_paths,
    )
    installed = bootstrap.install(bootstrap_request)
    assert installed.completed_phases[-1] is BootstrapPhase.COMPLETED
    assert len(trust_events) == 1
    skill_path = ".agents/skills/aurora-gold/SKILL.md"
    assert (upstream / skill_path).is_file()

    publication_decision = CapabilityDecision(
        request=CapabilityRequest(
            authority_repository=ROOT_REPOSITORY,
            target_repository=ROOT_REPOSITORY,
            capabilities=(Capability.GIT_PULL_REQUEST,),
            ref="refs/heads/main",
            path=skill_path,
            requested_at=NOW,
        ),
        decision="allow",
        effective_capabilities=(Capability.GIT_PULL_REQUEST,),
        reason="fixture publication grant",
        evaluator_version="gold-fixture/1",
        decided_at=NOW,
    )
    publication_manifest = PublicationManifest(
        producer=PublicationProducer.EXPORTED_SKILL,
        receipt_sha256=installed.id.rsplit(":", 1)[-1],
        paths=(
            PublicationManifestPath(
                path=skill_path,
                role=PathRole.EXPORTED_SKILL,
                sha256=hashlib.sha256((upstream / skill_path).read_bytes()).hexdigest(),
            ),
        ),
    )
    publication_request = PublishRequest(
        repository=ROOT_REPOSITORY,
        target_ref="refs/heads/main",
        mode=PublishMode.PULL_REQUEST,
        paths=(PublishPath(path=skill_path, role=PathRole.EXPORTED_SKILL),),
        capability_decision_sha256=publication_decision.sha256,
        created_at=NOW,
    )
    forge = _Forge()
    publisher = GitRepositoryPublisher(
        repository=upstream,
        manifests=(publication_manifest,),
        capability_decision=publication_decision,
        forge=forge,
        now=lambda: NOW,
        receipt_verifier=_ReceiptVerifier(publication_manifest),
        remote_transport=_LocalRemoteTransport(upstream, remote),
    )
    first_publication = publisher.publish(publication_request)
    second_publication = publisher.publish(publication_request)
    assert first_publication == second_publication
    assert first_publication.pull_request_url == (
        "https://github.com/example/aurora-gold-ontology/pull/7"
    )
    assert forge.calls[0] == forge.calls[1]

    removed = bootstrap.remove(bootstrap_request)
    assert removed.removed is True
    assert trust_events == []
    assert not (upstream / ".agents/skills/aurora-gold").exists()
    assert not (upstream / subscription_path).exists()
    assert sentinel.read_text() == "unrelated operator state must survive\n"
