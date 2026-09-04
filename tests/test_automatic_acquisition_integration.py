"""Offline vertical-slice contract for automatic ontology acquisition."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
from research_agent.extraction import (
    AnchorGroundedExtractionManager,
    ValidatedExtractionProposal,
)
from research_agent.library import (
    SourceLibraryBuilder,
    SourceLibraryManifest,
    SourceLibraryQueryEngine,
)
from research_agent.models import ModelParameters, ProviderConfig
from research_agent.ontology_build import OntologyBuildConfig
from research_agent.ontology_subscriptions import OntologySubscription, SubscriptionManager
from research_agent.parsing import ParsedDocumentManager
from research_agent.providers import ModelClient
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
)
from research_agent.repository_bootstrap import BootstrapOperation, RepositoryBootstrapManager
from research_agent.repository_catalog import resolve_repository_catalog, verify_catalog
from research_agent.repository_publisher import GitRepositoryPublisher
from research_agent.source_intent import SourceCandidate, SourceIntent
from research_agent.source_work import (
    AnchorGroundedSourceExtractionAdapter,
    ImmutableSourceWorkStore,
    SourceAuthorityContext,
    SourceCheckpoint,
    SourceExtractionConfig,
    SourceRetentionDecision,
    SourceRetentionRequest,
    SourceWorkCoordinator,
    SourceWorkOutcome,
    SourceWorkPhase,
)
from research_agent.store import ImmutableStore
from research_agent.user_config import GeasProfile, GeasUserConfig, UserConfigManager
from research_agent.web_sources import DirectUrlAdapter, FeedAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "automatic_acquisition" / "gold"
NOW = datetime(2026, 9, 3, 12, tzinfo=UTC)
ROOT_REPOSITORY = "https://github.com/example/aurora-gold-ontology"
SOURCE_REPOSITORY = "https://github.com/example/aurora-gold-sources"
BUNDLE_SHA256 = "7e778112e6b36b284c3de267a02e71908cc1c50fb0d2f287493cfd789f1385f7"
FEED_URL = "https://aurora-gold.example.test/news/feed.xml"
NEWS_URL = "https://aurora-gold.example.test/news/production-update.html"
REGULATORY_URL = "https://sedar-plus.example.test/filings/ni-43-101.html"
FINANCIAL_URL = "https://aurora-gold.example.test/financials/q2-2026.json"
CONSTRAINED_URL = "https://sedar-plus.example.test/filings/pending.html"


def _git(cwd: Path, *arguments: str) -> str:
    environment = {
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
        "GIT_AUTHOR_EMAIL": "gold-fixture@example.invalid",
        "GIT_AUTHOR_NAME": "Geas Gold Fixture",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
        "GIT_COMMITTER_EMAIL": "gold-fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Geas Gold Fixture",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_DEFAULT_HASH": "sha1",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": str(cwd),
        "LC_ALL": "C",
        "PATH": os.environ.get("PATH", ""),
    }
    completed = subprocess.run(
        (
            "git",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *arguments,
        ),
        cwd=cwd,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _local_bare_repository(tmp_path: Path) -> tuple[Path, Path, str]:
    remote = tmp_path / "remote.git"
    template = tmp_path / "empty-git-template"
    template.mkdir()
    _git(
        tmp_path,
        "init",
        "--bare",
        "--object-format=sha1",
        "--initial-branch=main",
        f"--template={template}",
        str(remote),
    )
    upstream = tmp_path / "upstream"
    _git(
        tmp_path,
        "init",
        "--object-format=sha1",
        "--initial-branch=main",
        f"--template={template}",
        str(upstream),
    )
    shutil.copytree(FIXTURE, upstream, dirs_exist_ok=True)
    _git(upstream, "remote", "add", "origin", str(remote))
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "seed offline Aurora Gold ontology")
    _git(upstream, "push", "--set-upstream", "origin", "main")
    _git(upstream, "remote", "set-url", "origin", ROOT_REPOSITORY)
    return remote, upstream, _git(upstream, "rev-parse", "HEAD")


def test_local_gold_seed_is_reproducible_and_ignores_host_git_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selector = tmp_path / "host-selected.git"
    selector.mkdir()
    monkeypatch.setenv("GIT_DIR", str(selector))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "host-worktree"))
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2040-01-01T00:00:00Z")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2040-01-01T00:00:00Z")

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = _local_bare_repository(first_root)
    second = _local_bare_repository(second_root)

    assert first[2] == second[2]
    assert _git(first[1], "show", "-s", "--format=%aI%n%cI", "HEAD").splitlines() == [
        "2000-01-01T00:00:00Z",
        "2000-01-01T00:00:00Z",
    ]


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


class _FixtureModelClient(ModelClient):
    """A local typed model client whose only external effect is deterministic JSON."""

    def __init__(
        self,
        config: ProviderConfig,
        parameters: ModelParameters,
    ) -> None:
        super().__init__("fixture-local", config, parameters=parameters)
        self.calls: list[dict[str, object]] = []

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        max_output_tokens: int | None = None,
    ) -> dict[str, object]:
        del system, max_output_tokens
        payload = json.loads(user)
        anchors = payload["untrusted_source_anchors"]
        self.calls.append(payload)
        joined = "\n".join(str(item["untrusted_text"]) for item in anchors)
        if "Management maintained full-year production guidance" in joined:
            key = "management_guidance"
            predicate = "mining:production_guidance"
            value: str | float = "195,000 to 205,000 ounces"
            exact = (
                "Management maintained full-year production guidance of "
                "195,000 to 205,000 ounces."
            )
        elif '"revenue_millions": 148.2' in joined:
            key = "financial_health"
            predicate = "finance:quarterly_revenue_millions"
            value = 148.2
            exact = '"revenue_millions": 148.2'
        else:
            key = "resource_estimate"
            predicate = "mining:measured_indicated_resources"
            value = "1.8 million ounces"
            exact = (
                "The technical report estimates 1.8 million ounces of measured and "
                "indicated gold resources."
            )
        anchor = next(item for item in anchors if exact in str(item["untrusted_text"]))
        return {
            "version": 1,
            "concepts": [],
            "claims": [
                {
                    "key": key,
                    "subject": "concept:aurora-gold",
                    "predicate": predicate,
                    "object": value,
                    "qualifiers": {},
                    "stance": "reports",
                    "epistemic_status": "observed",
                    "evidence": [{"anchor_id": anchor["anchor_id"], "exact": exact}],
                }
            ],
            "controversies": [],
            "gaps": [],
        }


class _FailingParser:
    """Interrupt at the real parser boundary after the source archive is durable."""

    def __init__(self, delegate: ParsedDocumentManager) -> None:
        self.registry = delegate.registry
        self.calls = 0

    def parse_source(self, *_args: object, **_kwargs: object) -> object:
        self.calls += 1
        raise RuntimeError("fixture parser boundary interruption")


def _source_bytes(locator: str) -> bytes:
    names = {
        NEWS_URL: "news.html",
        REGULATORY_URL: "regulatory-filing.html",
        FINANCIAL_URL: "financials.json",
    }
    return (FIXTURE / "http" / names[locator]).read_bytes()


class _SourceRequests:
    def __init__(self, connector: str, now: datetime) -> None:
        self.connector = connector
        self.now = now

    def _request(
        self,
        locator: str,
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
            connector=self.connector,
            host=urlsplit(locator).hostname,
            target=locator,
            requested_at=now,
        )

    def adapter(
        self,
        _intent: SourceIntent,
        locator: str,
        capability: Capability,
    ) -> CapabilityRequest:
        return self._request(locator, (capability,), self.now)

    def coordinator(
        self,
        _intent: SourceIntent,
        candidate: SourceCandidate,
        capabilities: tuple[Capability, ...],
        now: datetime,
    ) -> CapabilityRequest:
        return self._request(candidate.locator, capabilities, now)


def _coordinator(
    root: Path,
    *,
    store: ImmutableStore,
    adapter_type: type[DirectUrlAdapter] | type[FeedAdapter],
    transport: ConditionalHttpsTransport,
    evaluator: DeterministicCapabilityEvaluator,
    extraction: AnchorGroundedSourceExtractionAdapter,
    now: datetime = NOW,
    parser: object | None = None,
) -> tuple[SourceWorkCoordinator, DirectUrlAdapter | FeedAdapter]:
    requests = _SourceRequests(adapter_type.adapter_id, now)
    adapter = adapter_type(
        transport=transport,
        clock=lambda: now,
        capability_evaluator=evaluator,
        capability_request=requests.adapter,
    )
    coordinator = SourceWorkCoordinator(
        store=store,
        work_store=ImmutableSourceWorkStore(store),
        adapter=adapter,
        capability_evaluator=evaluator,
        capability_request=requests.coordinator,
        authority=SourceAuthorityContext(
            authority_repository=ROOT_REPOSITORY,
            target_repository=SOURCE_REPOSITORY,
            ref="refs/heads/main",
            path="sources/aurora-gold",
        ),
        ontology_bundle_sha256=BUNDLE_SHA256,
        library_manifest=SourceLibraryManifest.from_yaml(FIXTURE / "ontology/library.yaml"),
        library_database=root / "library.sqlite",
        extraction=extraction,
        retention_policy=_FixtureRetentionPolicy(),
        clock=lambda: now,
        monotonic=lambda: 0.0,
        parser=parser,  # type: ignore[arg-type]
    )
    return coordinator, adapter


def _capability_evaluator(
    repository: Path,
    commit: str,
) -> DeterministicCapabilityEvaluator:
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
            connectors=("source:direct-url", "source:feed"),
        ),
        max_delegation_depth=1,
        expires_at=None,
        created_at=NOW,
        created_via="manual",
    )
    return DeterministicCapabilityEvaluator(
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


def _extraction(
    store: ImmutableStore,
) -> tuple[AnchorGroundedSourceExtractionAdapter, _FixtureModelClient]:
    provider = ProviderConfig(
        kind="openai_compatible",
        base_url="http://127.0.0.1:8000/v1",
        model="fixture-local",
        external=False,
        max_output_tokens=4096,
        context_window_tokens=8192,
    )
    parameters = ModelParameters(thinking=False, reasoning_effort="none")
    client = _FixtureModelClient(provider, parameters)
    manager = AnchorGroundedExtractionManager(
        store=store,
        client=client,
        provider="fixture-local",
        model="fixture-local",
        clock=lambda: NOW,
    )
    adapter = AnchorGroundedSourceExtractionAdapter(
        manager,
        SourceExtractionConfig(
            question="Extract exact management, financial, and resource facts.",
            provider=provider,
            max_output_tokens=4096,
            model_parameters=parameters,
            allowed_concept_ids=("concept:aurora-gold",),
            debug_reasoning=False,
        ),
        provider_registry={"fixture-local": provider},
    )
    return adapter, client


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
    catalog = resolve_repository_catalog(upstream)
    assert catalog.delegation_manifest is not None
    assert catalog.delegation_manifest.delegations[0].resources.connectors == (
        "source:direct-url",
        "source:feed",
    )

    evaluator = _capability_evaluator(upstream, commit)
    delegated = evaluator.evaluate(
        _SourceRequests(DirectUrlAdapter.adapter_id, NOW)._request(
            REGULATORY_URL,
            (Capability.SOURCE_FETCH,),
            NOW,
        )
    )
    assert delegated.allowed
    assert delegated.delegation_chain == (ROOT_REPOSITORY, SOURCE_REPOSITORY)
    assert delegated.effective_remaining_depth == 0

    # Only DNS and HTTP are fake. The real adapters enumerate the feed, the real
    # delegated evaluator authorizes every hop, and the real coordinator owns work.
    client = _RecordingHttpClient(
        {
            FEED_URL: [
                ConditionalHttpResponse(
                    status=200,
                    headers={"Content-Type": "application/rss+xml"},
                    body=(FIXTURE / "http/issuer-feed.xml").read_bytes(),
                ),
                ConditionalHttpResponse(
                    status=200,
                    headers={"Content-Type": "application/rss+xml"},
                    body=(FIXTURE / "http/issuer-feed.xml").read_bytes(),
                ),
            ],
            NEWS_URL: [
                ConditionalHttpResponse(
                    status=200,
                    headers={
                        "Content-Type": "text/html",
                        "ETag": '"news-v1"',
                        "Last-Modified": "Sat, 15 Aug 2026 12:00:00 GMT",
                    },
                    body=_source_bytes(NEWS_URL),
                ),
                ConditionalHttpResponse(status=304, headers={"ETag": '"news-v1"'}),
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
                )
            ],
            CONSTRAINED_URL: [
                ConditionalHttpResponse(status=429, headers={"Retry-After": "60"})
            ],
        }
    )
    transport = ConditionalHttpsTransport(
        dns_resolver=lambda _host: ("8.8.8.8", "2001:4860:4860::8888"),
        http_client=client,
        capability_evaluator=evaluator,
        clock=lambda: NOW,
    )

    state = tmp_path / "source-state"
    store = ImmutableStore(state)
    extraction, model_client = _extraction(store)
    financial = next(item for item in config.source_intent if item.id == "aurora-financials")
    direct_intents = tuple(
        item
        for item in config.source_intent
        if item.discovery.kind.value == "direct_url"
    )
    failing_parser = _FailingParser(ParsedDocumentManager(store=store, clock=lambda: NOW))
    interrupted, _adapter = _coordinator(
        state,
        store=store,
        adapter_type=DirectUrlAdapter,
        transport=transport,
        evaluator=evaluator,
        extraction=extraction,
        parser=failing_parser,
    )
    with pytest.raises(RuntimeError, match="parser boundary interruption"):
        interrupted.run_due((financial,), now=NOW)
    assert failing_parser.calls == 1
    assert [call["url"] for call in client.calls].count(FINANCIAL_URL) == 1

    resumed, _adapter = _coordinator(
        state,
        store=store,
        adapter_type=DirectUrlAdapter,
        transport=transport,
        evaluator=evaluator,
        extraction=extraction,
    )
    direct_receipt = resumed.run_due(direct_intents, now=NOW)
    feed_worker, _adapter = _coordinator(
        state,
        store=store,
        adapter_type=FeedAdapter,
        transport=transport,
        evaluator=evaluator,
        extraction=extraction,
    )
    news = next(item for item in config.source_intent if item.id == "aurora-news")
    feed_receipt = feed_worker.run_due((news,), now=NOW)

    assert direct_receipt.complete, direct_receipt.model_dump(mode="json")
    assert feed_receipt.complete, feed_receipt.model_dump(mode="json")
    assert len({*direct_receipt.source_version_ids, *feed_receipt.source_version_ids}) == 3
    news_work = tuple(
        value
        for value in store.iter_records("source-work")
        if value["source_intent_id"] == "aurora-news"
    )
    assert news_work
    assert {value["locator"] for value in news_work} == {NEWS_URL}
    assert [call["url"] for call in client.calls].count(FEED_URL) == 1
    assert [call["url"] for call in client.calls].count(FINANCIAL_URL) == 1
    assert SourceWorkOutcome.CONSTRAINED_OPTIONAL in direct_receipt.semantic_outcomes
    proposals = tuple(
        ValidatedExtractionProposal.model_validate(value)
        for value in store.iter_records("extraction-proposal")
    )
    assert {claim.key for proposal in proposals for claim in proposal.claims} >= {
        "financial_health",
        "management_guidance",
    }
    assert all(proposal.review_state == "proposed" for proposal in proposals)
    assert all(proposal.commit_authority == "none_proposal_only" for proposal in proposals)
    claims = {
        claim.key: (proposal, claim)
        for proposal in proposals
        for claim in proposal.claims
    }
    expected_evidence = {
        "financial_health": '"revenue_millions": 148.2',
        "management_guidance": (
            "Management maintained full-year production guidance of "
            "195,000 to 205,000 ounces."
        ),
        "resource_estimate": (
            "The technical report estimates 1.8 million ounces of measured and "
            "indicated gold resources."
        ),
    }
    for key, exact in expected_evidence.items():
        proposal, claim = claims[key]
        assert len(claim.evidence) == 1
        evidence = claim.evidence[0]
        source_text = store.read_blob(proposal.source_content_sha256).decode()
        assert source_text[evidence.start : evidence.end] == exact
        assert evidence.exact == exact
        assert evidence.exact_sha256 == hashlib.sha256(exact.encode()).hexdigest()
    decisions = tuple(
        CapabilityDecision.model_validate(value)
        for value in store.iter_records("capability-decision")
    )
    assert decisions
    assert all(decision.grant_ids for decision in decisions)
    assert {decision.request.connector for decision in decisions} <= {
        "source:direct-url",
        "source:feed",
    }
    assert tuple(store.iter_records("claim")) == ()

    constraint_checkpoints = tuple(
        SourceCheckpoint.model_validate(value)
        for value in store.iter_records("source-checkpoint")
        if value.get("constraint") == "rate_limited"
    )
    assert len(constraint_checkpoints) == 1
    assert constraint_checkpoints[0].retry_after == 60

    first_source_count = len(tuple(store.iter_records("source-version")))
    first_model_count = len(model_client.calls)
    refreshed_worker, _adapter = _coordinator(
        state,
        store=store,
        adapter_type=FeedAdapter,
        transport=transport,
        evaluator=evaluator,
        extraction=extraction,
        now=NOW + timedelta(seconds=901),
    )
    refresh = refreshed_worker.run_due((news,), now=NOW + timedelta(seconds=901))
    assert refresh.complete
    assert SourceWorkPhase.NOT_MODIFIED in refresh.completed_phases
    assert len(tuple(store.iter_records("source-version"))) == first_source_count
    assert len(model_client.calls) == first_model_count
    news_calls = tuple(call for call in client.calls if call["url"] == NEWS_URL)
    assert len(news_calls) == 2
    assert news_calls[-1]["headers"] == {
        "accept-encoding": "gzip, deflate",
        "if-modified-since": "Sat, 15 Aug 2026 12:00:00 GMT",
        "if-none-match": '"news-v1"',
    }

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
    query = SourceLibraryQueryEngine(state / "library-rebuilt.sqlite").query(
        "sustaining cost per ounce"
    )
    assert query.hits
    assert query.hits[0].source_uri == FINANCIAL_URL
    financial_anchor = next(
        value
        for value in anchors
        if b"sustaining_cost_per_ounce"
        in store.read_blob(value["source_content_sha256"])[value["start"] : value["end"]]
    )
    assert query.hits[0].anchor_id == financial_anchor["id"]
    assert (query.hits[0].start, query.hits[0].end) == (
        financial_anchor["start"],
        financial_anchor["end"],
    )

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

    # These methods land with Task 7's bootstrap-state fan-in. Keeping the calls
    # after the parser gate makes the pre-fan-in failure precise without fake state.
    from research_agent.repository_bootstrap import remove_obsolete_paths

    state_root = tmp_path / "geas-state"
    config_manager = UserConfigManager(state_root / "config.yaml")
    config_manager.root.mkdir(parents=True)
    unrelated_subscription = OntologySubscription(
        url="https://github.com/example/unrelated-ontology.git",
        active_ref="refs/heads/main",
        checkout=Path("subscriptions/default/unrelated"),
    )
    unrelated_grant = CapabilityGrant(
        decision="allow",
        subject=CapabilitySubject(
            repository="https://github.com/example/unrelated-ontology",
            refs=("refs/heads/main",),
            paths="*",
            bundle_sha256="*",
        ),
        capabilities=(Capability.REPOSITORY_READ,),
        delegable_capabilities=(),
        resources=CapabilityResources(),
        max_delegation_depth=0,
        expires_at=None,
        created_at=NOW,
        created_via="manual",
    )
    config_manager.replace(
        GeasUserConfig(
            version=2,
            profiles={
                "default": GeasProfile(
                    ontology_git=None,
                    subscriptions={"unrelated": unrelated_subscription},
                    capability_grants=(unrelated_grant,),
                ),
                "preserved": GeasProfile(
                    ontology_git=None,
                    ontology_directory=Path("preserved-ontologies"),
                ),
            }
        ),
        upgrade_version=True,
    )
    sentinel = state_root / "operator-notes.txt"
    sentinel.write_text("unrelated operator state must survive\n")

    class LocalSubscriptionRepository:
        def __init__(self, checkout: Path, subscription: OntologySubscription) -> None:
            self.checkout = checkout
            self.subscription = subscription

        def pull(self) -> dict[str, object]:
            self.checkout.parent.mkdir(parents=True, exist_ok=True)
            _git(
                self.checkout.parent,
                "clone",
                "--no-checkout",
                str(remote),
                str(self.checkout),
            )
            _git(self.checkout, "checkout", "--detach", commit)
            _git(self.checkout, "remote", "set-url", "origin", self.subscription.url)
            return {"commit": commit}

        def assert_removable(self) -> None:
            assert _git(self.checkout, "status", "--porcelain") == ""
            assert _git(self.checkout, "remote", "get-url", "origin") == ROOT_REPOSITORY

        def assert_verified_commit(self, expected_commit: str) -> None:
            assert _git(self.checkout, "rev-parse", "HEAD") == expected_commit

    subscriptions = SubscriptionManager(
        config_manager=config_manager,
        profile_name="default",
        catalog_verifier=verify_catalog,
        authorizer=lambda verified_catalog: verified_catalog,
        repository_factory=LocalSubscriptionRepository,
    )

    def operation_subscription(operation: BootstrapOperation) -> OntologySubscription:
        return OntologySubscription(
            url=operation.verified.repository,
            active_ref=operation.verified.ref,
            checkout=Path("subscriptions/default") / operation.request.name,
            catalog=Path(operation.verified.catalog),
        )

    def record_trust(operation: BootstrapOperation, grant: CapabilityGrant) -> object:
        return config_manager.record_bootstrap_grant(
            operation_key=operation.idempotency_key,
            profile_name="default",
            bootstrap_name=operation.request.name,
            grant=grant,
        )

    def subscribe(operation: BootstrapOperation) -> object:
        return subscriptions.ensure_bootstrap_subscription(
            operation.request.name,
            operation_subscription(operation),
            operation_key=operation.idempotency_key,
            verified_commit=operation.verified.commit_sha256,
        )

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

    def remove_trust(operation: BootstrapOperation, grant: CapabilityGrant) -> object:
        assert operation.grant_ownership is not None
        return config_manager.remove_bootstrap_grant(
            operation_key=operation.idempotency_key,
            profile_name="default",
            bootstrap_name=operation.request.name,
            ownership=operation.grant_ownership,
            grant=grant,
        )

    def unsubscribe(operation: BootstrapOperation) -> object:
        assert operation.subscription_ownership is not None
        return subscriptions.remove_bootstrap_subscription(
            operation.request.name,
            operation_subscription(operation),
            operation_key=operation.idempotency_key,
            ownership=operation.subscription_ownership,
        )

    def remove_skill_paths(operation: BootstrapOperation) -> None:
        skill_operation = BootstrapOperation(
            request=operation.request,
            verified=operation.verified,
            phase=operation.phase,
            idempotency_key=operation.idempotency_key,
            owned_paths=tuple(
                path for path in operation.owned_paths if path.role != "receipt"
            ),
            grant_ownership=operation.grant_ownership,
            subscription_ownership=operation.subscription_ownership,
        )
        remove_obsolete_paths(upstream, skill_operation, state_root=state_root)

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
        source_connectors=("source:direct-url", "source:feed"),
        delegated_repositories=(SOURCE_REPOSITORY,),
        current_worktree=upstream.resolve(),
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
        source_connectors=("source:direct-url", "source:feed"),
        delegated_repositories=(SOURCE_REPOSITORY,),
        current_worktree=upstream.resolve(),
    )
    bootstrap = RepositoryBootstrapManager(
        managed_root=upstream,
        state_root=state_root,
        announce=lambda _message: None,
        now=lambda: NOW,
        verify=lambda _request: verified_bootstrap,
        record_trust=record_trust,
        subscribe=subscribe,
        hydrate_artifacts=lambda _operation: (),
        install_generic_skill=lambda _operation: (),
        export_catalog_skills=export_catalog,
        link_agents=lambda _operation: (),
        remove_trust=remove_trust,
        unsubscribe=unsubscribe,
        remove_skills=remove_skill_paths,
        verify_software_provenance=lambda: None,
    )
    installed = bootstrap.install(bootstrap_request)
    assert installed.completed_phases[-1] is BootstrapPhase.COMPLETED
    assert installed.grant_ownership is not None
    assert installed.subscription_ownership is not None
    assert installed.grant_mutation is not None
    assert installed.subscription_mutation is not None
    installed_profile = config_manager.load().profiles["default"]
    assert len(installed_profile.capability_grants) == 2
    assert tuple(sorted(installed_profile.subscriptions)) == ("aurora-gold", "unrelated")
    checkout = config_manager.root / "subscriptions/default/aurora-gold"
    assert _git(checkout, "rev-parse", "HEAD") == commit
    assert (state_root / "repository-bootstrap/aurora-gold.json").is_file()
    assert not (upstream / "repository-bootstrap").exists()
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
    publication = tmp_path / "publication"
    _git(tmp_path, "clone", str(remote), str(publication))
    _git(publication, "remote", "set-url", "origin", ROOT_REPOSITORY)
    publication_skill = publication / skill_path
    publication_skill.parent.mkdir(parents=True)
    shutil.copy2(upstream / skill_path, publication_skill)
    publication_manifest = PublicationManifest(
        producer=PublicationProducer.EXPORTED_SKILL,
        receipt_sha256=installed.id.rsplit(":", 1)[-1],
        paths=(
            PublicationManifestPath(
                path=skill_path,
                role=PathRole.EXPORTED_SKILL,
                sha256=hashlib.sha256(publication_skill.read_bytes()).hexdigest(),
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
        repository=publication,
        manifests=(publication_manifest,),
        capability_decision=publication_decision,
        forge=forge,
        now=lambda: NOW,
        receipt_verifier=_ReceiptVerifier(publication_manifest),
        remote_transport=_LocalRemoteTransport(publication, remote),
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
    assert removed.grant_mutation is not None
    assert removed.grant_mutation.action == "remove"
    assert removed.subscription_mutation is not None
    assert removed.subscription_mutation.action == "remove"
    final_config = config_manager.load()
    assert tuple(final_config.profiles) == ("default", "preserved")
    assert final_config.profiles["default"].capability_grants == (unrelated_grant,)
    assert tuple(final_config.profiles["default"].subscriptions) == ("unrelated",)
    assert not (upstream / skill_path).exists()
    assert not checkout.exists()
    assert sentinel.read_text() == "unrelated operator state must survive\n"
