# Automatic Acquisition and Agent Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement deterministic repository/source capability delegation, automatic bounded document acquisition and proposal extraction, repository-backed agent bootstrap, and safely gated PR/direct-push publication.

**Architecture:** A contract-only commit establishes strict versioned models and injectable protocols. Five independently owned streams then implement capability resolution, source adapters, resumable source work, repository bootstrap, and publishing against those contracts before a single CLI/configuration fan-in. The maintained ontology, generated skills, documentation, CI, and a fixture-backed Canadian gold-miner workflow close the vertical slice.

**Tech Stack:** Python 3.12, Pydantic v2, PyYAML, pathlib, hashlib/canonical JSON, SQLite FTS5, Git subprocesses, curl-compatible HTTPS transport, argparse, pytest, uv, GitHub Actions, and octo-sts-rust.

**Spec:** `docs/superpowers/specs/2026-09-02-geas-automatic-acquisition-agent-bootstrap-design.md`

## Global Constraints

- Use Beads epic `geas-xh0` and children `geas-xh0.1` through `geas-xh0.9`; claim before implementation and record test/review evidence before closing.
- Write a behavioral test first and observe its expected failure before every production behavior change.
- Source text, repository declarations, exported skills, URLs, snippets, and model output are untrusted data and never grant authority.
- Existing version 1 trust rules retain only `repository.read`; they gain no source, model, delegation, or Git capability.
- Selector precedence remains exact digest, then path, then ref; equal-specificity deny wins.
- Delegated child depth is exactly `min(parent_remaining - 1, child_declared_depth)` and delegation only narrows capabilities, delegable capabilities, resources, expiry, and depth.
- `--yolo` grants invocation-scoped `repository.read` only.
- `source.extract` is local parsing/indexing authority and never implies `model.external`; existing model policy and budget gates remain mandatory.
- Every external fetch is HTTPS/default-port/credential-free, all-address public, redirect-rechecked, bounded, and authorized against both source intent and local capability scope.
- Discovery snippets remain leads; accepted evidence requires immutable archived bytes and exact selectors.
- Pull requests are the publication default. Direct push requires local `git.direct_push`, a writable exact ref, explicit `--direct-push`, confined owned paths, a fresh remote comparison, and an exact lease.
- Promotion or canonical semantic paths additionally require `knowledge.auto_promote`; a Git capability never blesses semantic contents.
- Explicit bootstrap commands authorize only their documented confined local mutations and announce them before mutation.
- Normal tests are offline and deterministic with temporary roots, local Git remotes, fake clocks/DNS/HTTP/model/forge boundaries, and literal fixtures.
- JSON receipts go to stdout; prompts, announcements, progress, and diagnostics go to stderr.
- Do not commit runtime stores, source blobs, SQLite caches, credentials, `.env`, logs, `.DS_Store`, or unrelated user changes.
- Each parallel stream owns only the files listed for it. Workers are not alone in the codebase and must not revert or rewrite other streams' changes.

---

### Task 1: Shared Versioned Contracts and Fakes (`geas-xh0.1`)

**Files:**
- Create: `src/research_agent/capabilities.py`
- Create: `src/research_agent/source_intent.py`
- Create: `src/research_agent/source_work.py`
- Create: `src/research_agent/bootstrap_models.py`
- Create: `src/research_agent/publishing.py`
- Create: `tests/fakes/__init__.py`
- Create: `tests/fakes/automatic_acquisition.py`
- Create: `tests/test_automatic_acquisition_contracts.py`

**Interfaces:**
- Produces: `Capability`, `CapabilitySubject`, `CapabilityResources`, `CapabilityGrant`, `CapabilityRequest`, `CapabilityDecision`, `DelegationEntry`, `DelegationManifest`, and `CapabilityEvaluator`.
- Produces: `DiscoveryKind`, `SourceDiscovery`, `SourceRefreshPolicy`, `SourceAssociations`, `SourceTemporalPolicy`, `SourceIntent`, `SourceCandidate`, and `SourceAdapter`.
- Produces: `SourceWorkPhase`, `SourceWorkItem`, `SourceCheckpoint`, `SourceUpdateReceipt`, and `SourceWorkStore`.
- Produces: `BootstrapPhase`, `ManagedPath`, `RepositoryInstallReceipt`, `RepositoryMutationReceipt`, and `RepositoryBootstrapService`.
- Produces: `PathRole`, `PublishMode`, `PublishRequest`, `PublishResult`, and `RepositoryPublisher`.
- Consumes: `StrictModel`, `canonical_json`, and content-derived identities from `research_agent.models`.

- [ ] **Step 1: Write failing strict-contract tests**

```python
def test_capability_grant_rejects_non_delegable_capability() -> None:
    with pytest.raises(ValidationError, match="delegable_capabilities"):
        CapabilityGrant(
            decision="allow",
            subject=_subject(),
            capabilities=(Capability.REPOSITORY_READ,),
            delegable_capabilities=(Capability.SOURCE_FETCH,),
            resources=CapabilityResources(),
            max_delegation_depth=1,
            expires_at=None,
            created_at=NOW,
            created_via="manual",
        )

def test_source_work_identity_changes_with_authority_receipt() -> None:
    left = _work(capability_decision_sha256="1" * 64)
    right = _work(capability_decision_sha256="2" * 64)
    assert left.id != right.id
```

Add literal tests for extra-field rejection, sorted/unique tuples, explicit empty/null serialization, unsupported versions, timezone-aware dates, safe refs/paths/URLs, canonical IDs, phase ordering, and path-role validation.

- [ ] **Step 2: Run the contract tests and observe missing imports**

Run: `uv run pytest tests/test_automatic_acquisition_contracts.py -v`

Expected: collection fails because the five contract modules do not exist.

- [ ] **Step 3: Add strict models and protocols without live implementations**

```python
class Capability(str, Enum):
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

class CapabilityEvaluator(Protocol):
    def evaluate(self, request: CapabilityRequest) -> CapabilityDecision: ...

class SourceAdapter(Protocol):
    adapter_id: str
    version: str
    def discover(self, intent: SourceIntent) -> tuple[SourceCandidate, ...]: ...
    def fetch(self, candidate: SourceCandidate, *, prior: SourceCheckpoint | None) -> SourceCheckpoint: ...
```

Keep fakes explicit-deny by default. Fake adapters record calls and return only fixture values configured by a test; fake clocks never consult wall time.

- [ ] **Step 4: Run contract tests and the existing model suite**

Run: `uv run pytest tests/test_automatic_acquisition_contracts.py tests/test_models.py -v`

- [ ] **Step 5: Claim/close the Bead and commit**

Run: `bd update geas-xh0.1 --claim`, then record the test command/result and `bd close geas-xh0.1` after review.

Commit: `feat: define automatic acquisition contracts`

---

### Task 2: Capability Grants, Configuration Migration, and Delegation (`geas-xh0.2`)

**Files:**
- Modify: `src/research_agent/capabilities.py`
- Modify: `src/research_agent/user_config.py`
- Modify: `src/research_agent/ontology_trust.py`
- Modify: `src/research_agent/repository_catalog.py`
- Modify: `src/research_agent/ontology_resolution.py`
- Create: `tests/test_capabilities.py`
- Modify: `tests/test_user_config.py`
- Modify: `tests/test_ontology_trust.py`
- Modify: `tests/test_repository_catalog.py`
- Modify: `tests/test_ontology_resolution.py`

**Interfaces:**
- Consumes: Task 1 capability models and evaluator protocol.
- Produces: `DeterministicCapabilityEvaluator(grants, manifests, *, clock, yolo=False)`; `load_delegation_manifest()`; version 1 compatibility mapping; strict version 2 user config; verified delegation metadata on `ResolvedRepositoryCatalog`.
- Produces compatibility: existing `evaluate_trust()` and catalog authorization delegate to `repository.read` decisions without changing four-choice snapshot behavior.

- [ ] **Step 1: Write failing direct-rule and version-migration tests**

```python
def test_v1_trust_rule_grants_repository_read_only(tmp_path: Path) -> None:
    config = UserConfigManager(tmp_path / "config.yaml").load()
    grant = config.profile()[1].effective_capability_grants()[0]
    assert grant.capabilities == (Capability.REPOSITORY_READ,)
    assert grant.delegable_capabilities == ()

def test_equal_specificity_deny_wins_for_one_atomic_capability() -> None:
    decision = evaluator(_allow(path="ontology/a"), _deny(path="ontology/a")).evaluate(
        _request(Capability.SOURCE_FETCH, path="ontology/a")
    )
    assert decision.allowed is False
```

Assert v1 load does not rewrite bytes, explicit v2 update writes every field, exact digest/path/ref precedence, target-local deny, intermediate local intersection, dirty branch-only denial, expiry boundaries, unknown capability failure, and `--yolo` refusal for every non-read capability.

- [ ] **Step 2: Run focused tests and observe missing evaluator behavior**

Run: `uv run pytest tests/test_capabilities.py tests/test_user_config.py tests/test_ontology_trust.py -v`

- [ ] **Step 3: Implement direct grants and explicit v1/v2 configuration policy**

`GeasUserConfig.from_yaml()` accepts version 1 or 2. Version 1 keeps `trust_rules` as the serialized source and exposes read-only compatibility grants in memory. `UserConfigManager.replace(..., upgrade_version=True)` is the only path that writes version 2 capability grants. Duplicate normalized grant selectors fail validation.

- [ ] **Step 4: Write failing pinned-manifest and depth tests**

```python
def test_one_hop_consumes_the_only_delegation_edge() -> None:
    decision = _evaluate_chain(root_depth=1, child_declared_depth=8)
    assert decision.allowed
    assert decision.effective_remaining_depth == 0

def test_child_depth_is_parent_minus_one_intersected_with_declaration() -> None:
    decision = _evaluate_chain(root_depth=4, child_declared_depth=2)
    assert decision.effective_remaining_depth == 2
```

Add failures for bad catalog hash/size/name, symlink, unsorted entries, undeclared child repository, missing `trust.delegate`, missing delegable capability, cycles, repeated identities, attempted widening, expired intermediate, multiple valid chains, and deterministic lexical chain selection.

- [ ] **Step 5: Implement catalog-pinned delegation and canonical decisions**

Extend `RepositoryCatalog` with optional `delegations` metadata fixed to `geas-delegations.yaml`. Verify bytes before parsing. Traverse only sorted manifest entries, decrement depth before the child, intersect all bounds, apply specificity-resolved local decisions at every context, and hash the normalized request/chain/effective result into the receipt.

- [ ] **Step 6: Switch existing repository reads to the evaluator**

Keep `TrustRule`, `TrustDecision`, and interactive snapshot APIs as compatibility surfaces. `ontology_resolution` and subscription authorization issue `Capability.REPOSITORY_READ` requests. No existing caller receives source, model, or Git authority from legacy trust or `--yolo`.

- [ ] **Step 7: Run focused and compatibility suites**

Run: `uv run pytest tests/test_capabilities.py tests/test_user_config.py tests/test_ontology_trust.py tests/test_repository_catalog.py tests/test_ontology_resolution.py tests/test_ontology_subscriptions.py -v`

- [ ] **Step 8: Record Bead evidence and commit**

Commit: `feat: add delegated capability authority`

---

### Task 3: Source Intent and Bounded Acquisition Adapters (`geas-xh0.3`)

**Files:**
- Modify: `src/research_agent/source_intent.py`
- Modify: `src/research_agent/remote_acquisition.py`
- Create: `src/research_agent/web_sources.py`
- Modify: `src/research_agent/discovery_acquisition.py`
- Create: `tests/test_source_intent.py`
- Create: `tests/test_web_sources.py`
- Modify: `tests/test_remote_acquisition.py`
- Modify: `tests/test_discovery_acquisition.py`
- Create: `tests/fixtures/web_sources/feed.xml`
- Create: `tests/fixtures/web_sources/sitemap.xml`
- Create: `tests/fixtures/web_sources/news.html`
- Create: `tests/fixtures/web_sources/financials.json`

**Interfaces:**
- Consumes: Task 1 source contracts and `CapabilityEvaluator`.
- Produces: `ConditionalHttpsTransport`, `DirectUrlAdapter`, `FeedAdapter`, `SitemapAdapter`, `HtmlDiscoveryAdapter`, `MojeekSourceAdapter`, and `GitHubRepositorySourceAdapter`.
- Preserves: `PinnedHttpsFetcher` and `LicenseGatedAcquirer` public behavior for existing Unpaywall callers.

- [ ] **Step 1: Write failing source-intent validation and no-I/O tests**

```python
def test_direct_url_materialization_performs_no_network_io() -> None:
    adapter = DirectUrlAdapter(transport=FailIfCalledTransport())
    candidates = adapter.discover(_intent("https://issuer.example/report.pdf"))
    assert [item.locator for item in candidates] == ["https://issuer.example/report.pdf"]

def test_intent_cannot_broaden_local_host_scope() -> None:
    with pytest.raises(SourceAuthorizationError, match="host"):
        authorize_candidate(_candidate("https://other.example/a"), _host_decision("issuer.example"))
```

Cover discovery kind, path/media/glob validation, sorted associations, refresh bounds, required/priority fields, control characters, credentials, non-HTTPS, and private literal addresses.

- [ ] **Step 2: Run tests and observe missing adapter behavior**

Run: `uv run pytest tests/test_source_intent.py -v`

- [ ] **Step 3: Implement strict intent parsing and candidate normalization**

Direct URL discovery is pure. Feed/sitemap/HTML/Mojeek enumeration requires `source.discover`; retrieving their discovery bytes additionally requires `source.fetch`. Every emitted child is separately normalized and later authorized.

- [ ] **Step 4: Write failing conditional-transport security tests**

Use injected DNS and HTTP fixtures to cover all-public IPv4/IPv6, mixed public/private fail-closed, re-resolution before redirects, unsafe redirects, credentials, port changes, redirect limits, connection/read timeout, compressed and decoded size ceilings, content sniffing, `ETag`, `Last-Modified`, `200`, `304`, `Retry-After`, and sanitized response metadata.

- [ ] **Step 5: Implement the transport and adapter set**

```python
class ConditionalHttpsTransport:
    def fetch(
        self,
        request: SourceFetchRequest,
        *,
        prior: SourceValidator | None = None,
    ) -> SourceFetchResult: ...
```

Authorize each request and redirect before I/O, reject any non-global resolved address, pin the selected address, enforce both wire and decoded limits, and expose only allowlisted conditional/cache metadata. Represent unsupported/denied/rate-limited routes as typed constraints; never evade access controls.

- [ ] **Step 6: Implement deterministic feed/sitemap/HTML/JSON/XML/PDF/text behavior**

Deduplicate candidates by normalized locator, then sort by locator bytes. XML parsing disables external entities. HTML enumeration ignores scripts/forms and resolves only matching links. Parsing source documents remains delegated to `ParsedDocumentManager`; adapters do not create evidence.

- [ ] **Step 7: Adapt GitHub acquisition behind `SourceAdapter`**

Keep legacy `GitHubDiscoveryAcquirer` and `RepositorySnapshot` results, but make the new adapter return the same immutable README source through the source protocol. Existing GitHub tests must remain byte-compatible.

- [ ] **Step 8: Run focused suites and commit**

Run: `uv run pytest tests/test_source_intent.py tests/test_web_sources.py tests/test_remote_acquisition.py tests/test_discovery_acquisition.py -v`

Commit: `feat: add bounded web source adapters`

---

### Task 4: Resumable Source Work, Libraries, and Extraction (`geas-xh0.4`)

**Files:**
- Modify: `src/research_agent/source_work.py`
- Modify: `src/research_agent/ontology_build.py`
- Modify: `src/research_agent/ontology_config.py`
- Modify: `src/research_agent/parsing.py`
- Modify: `src/research_agent/library.py`
- Create: `tests/test_source_work.py`
- Create: `tests/test_ontology_update.py`
- Modify: `tests/test_ontology_build.py`
- Modify: `tests/test_source_library.py`
- Modify: `tests/test_parsing.py`

**Interfaces:**
- Consumes: Task 1 contracts; Task 2 and 3 live implementations only through protocols.
- Produces: `ImmutableSourceWorkStore`, `SourceWorkCoordinator.run_due(...)`, `OntologyUpdateService.update(name, *, now)`, and generic parsed-source selection for ontology extraction.
- Preserves: existing `OntologyBuilder.run()` resume/finalization behavior and GitHub compatibility.

- [ ] **Step 1: Write failing transition, identity, and interruption tests**

```python
def test_resume_reuses_completed_fetch_after_interruption(tmp_path: Path) -> None:
    first = coordinator(tmp_path, fail_after=SourceWorkPhase.ARCHIVED)
    with pytest.raises(InjectedInterruption):
        first.run_due((_intent(),))
    second = coordinator(tmp_path)
    receipt = second.run_due((_intent(),))
    assert second.adapter.fetch_calls == 0
    assert receipt.completed_phases[-1] == SourceWorkPhase.FINALIZED
```

Test every legal and illegal transition, atomic checkpoint replacement, authority/parser/validator/bundle identity incompatibility, duplicate bytes, immutable successor versions, `304` refresh observations, and constraint paths that never write a blob.

- [ ] **Step 2: Run source-work tests and observe missing coordinator behavior**

Run: `uv run pytest tests/test_source_work.py -v`

- [ ] **Step 3: Implement work store and coordinator through indexed phase**

Persist immutable work/checkpoint records in `ImmutableStore`; use a small atomic index only to locate the current work identity. Run authorize, fetch, archive, parse, structure/citations/threat scan, and deterministic full library rebuild over the affected selected versions. SQLite is never checkpoint authority.

- [ ] **Step 4: Write failing anchor/extraction and due-scheduling tests**

Assert exact external anchor selection, local extraction without `model.external`, external-model denial before the provider call, existing model-policy/budget denial after capability allow, priority then UTF-8 ordering, fake-clock due/not-due boundaries, request/byte/time caps, required/optional constraint finalization, and finalization reserve.

- [ ] **Step 5: Complete proposal-only extraction and temporal behavior**

Use existing anchor selection and `ExtractionManager`; source work supplies generic parsed receipt/source identities rather than `RepositorySnapshot`. Record publication/observation/valid/recorded times and deterministic supersession relations without overwriting earlier observations.

- [ ] **Step 6: Add `source_intent` and explicit update defaults**

Extend strict `OntologyBuildConfig` with `source_intent: tuple[SourceIntent, ...] = ()`. Add explicit request/byte/depth/refresh/finalization defaults to ontology config serialization. Absence must preserve existing builds exactly.

- [ ] **Step 7: Migrate GitHub README work to the coordinator**

Keep current discovery and ranking behavior, but translate acquired repository README sources into `SourceWorkItem`s. Proposal compatibility and library filters use generic immutable parsed-source identities.

- [ ] **Step 8: Run focused suites and commit**

Run: `uv run pytest tests/test_source_work.py tests/test_ontology_update.py tests/test_ontology_build.py tests/test_source_library.py tests/test_parsing.py tests/test_discovery_acquisition.py -v`

Commit: `feat: automate resumable ontology source work`

---

### Task 5: Repository-Backed Agent Bootstrap Lifecycle (`geas-xh0.5`)

**Files:**
- Modify: `src/research_agent/bootstrap_models.py`
- Create: `src/research_agent/repository_bootstrap.py`
- Modify: `src/research_agent/agent_skills.py`
- Modify: `src/research_agent/catalog_skill_export.py`
- Modify: `src/research_agent/builtin_skills/geas/SKILL.md`
- Modify: `src/research_agent/builtin_skills/geas/references/cli.md`
- Modify: `src/research_agent/builtin_skills/geas/references/skills.md`
- Create: `tests/test_repository_bootstrap.py`
- Modify: `tests/test_builtin_geas_skill.py`
- Modify: `tests/test_catalog_skill_lifecycle.py`
- Modify: `tests/test_agent_skills.py`

**Interfaces:**
- Consumes: Task 1 bootstrap/capability protocols and existing subscription, artifact, and skill managers.
- Produces: `RepositoryBootstrapManager.install()`, `.update()`, `.remove()`, durable ownership receipts/journals, repository-trust grant construction, and bootstrap-aware skill manifest version.

- [ ] **Step 1: Write failing install plan and announcement tests**

```python
def test_install_announces_every_mutation_before_first_write(tmp_path: Path) -> None:
    events: list[str] = []
    manager = _manager(tmp_path, announce=events.append)
    receipt = manager.install(_request(trust="read_only"))
    assert events[0].startswith("Geas will bind repository")
    assert receipt.completed_phases[0] == BootstrapPhase.VERIFIED
```

Test remote/ref/catalog normalization, exact checkout verification, `--read-only`, trust-repository snapshot scope/depth, current-worktree binding, artifact unavailable, no forced software installation, staged failure rollback, post-commit resumption, and unrelated-file preservation.

- [ ] **Step 2: Run bootstrap tests and observe the missing service**

Run: `uv run pytest tests/test_repository_bootstrap.py -v`

- [ ] **Step 3: Implement staged install composition**

Compose `SubscriptionManager`, catalog verification/authorization, `OntologyArtifactManager`, `install_builtin_geas_skill`, and `export_catalog_skill`. `--trust-repository` records only current ref/path/source/child scopes, the four source capabilities, read/delegate, and default depth one; it records no model or Git capability.

- [ ] **Step 4: Add failing update/remove ownership tests**

Cover idempotent update, newly declared host denial, software provenance failure before ontology writes, modified managed path refusal, `.agents/skills` copy, `.geas/skills` ignored fallback, Codex/Claude/OpenCode link deduplication, journal recovery at every phase, exact trust/subscription removal, and `uv tool uninstall geas` as reported guidance only.

- [ ] **Step 5: Implement update, removal, and bootstrap skill rendering**

New skills remain statically readable. They show the project URL, operator-approved commit requirement, repository URL/ref/catalog/name/bundle digest, `geas list`, bounded query routes, update and removal commands, and hierarchical reference links. They never treat their own commit text as install authority.

- [ ] **Step 6: Run focused suites and commit**

Run: `uv run pytest tests/test_repository_bootstrap.py tests/test_builtin_geas_skill.py tests/test_catalog_skill_lifecycle.py tests/test_agent_skills.py tests/test_skill_lifecycle.py -v`

Commit: `feat: add repository agent bootstrap lifecycle`

---

### Task 6: Role-Classified Repository Publishing and Forge Automation (`geas-xh0.6`)

**Files:**
- Modify: `src/research_agent/publishing.py`
- Create: `src/research_agent/repository_publisher.py`
- Modify: `src/research_agent/pr_skill_sync.py`
- Modify: `.github/workflows/pr-skill-regeneration.yml`
- Modify: `.github/workflows/pr-skill-writeback.yml`
- Create: `docs/GITHUB_APP_AUTOMATION.md`
- Create: `tests/test_repository_publisher.py`
- Modify: `tests/test_pr_skill_sync.py`
- Modify: `tests/test_promotion.py`

**Interfaces:**
- Consumes: Task 1 publishing/capability/bootstrap receipt protocols.
- Produces: `classify_managed_path()`, `GitRepositoryPublisher.publish()`, exact-lease push, deterministic PR identity, auto-merge eligibility, and protected workflow evaluation.

- [ ] **Step 1: Write failing role-matrix tests**

Use a literal table covering every spec row and mode. Assert runtime/unknown/operator paths are never staged, proposals cannot reach a canonical ref, and promotion/accepted/source-card/policy paths require `knowledge.auto_promote` in addition to the Git capability.

- [ ] **Step 2: Run publisher tests and observe missing classification behavior**

Run: `uv run pytest tests/test_repository_publisher.py -v`

- [ ] **Step 3: Implement path classification and local/PR publication**

Classify from normalized repository-relative paths plus strict manifests, not receipt claims. `none` preserves local changes. PR mode derives branch/title/body/commit from receipt hashes, stages only owned classified paths, pushes with a lease, and creates or updates one PR through an injected forge client.

- [ ] **Step 4: Write failing direct-push and protected-workflow tests**

Cover missing `git.direct_push`, missing explicit flag, non-branch ref, dirty/unowned changes, remote movement, lease failure, delegated capability not in both capability sets, fork PR, wrong workflow/ref/repository/head/artifact/path/App identity, and proof token exchange happens only after all read-only checks.

- [ ] **Step 5: Implement direct push and auto-merge gates**

Direct push requires a fresh target object and `--force-with-lease=<ref>:<expected>`, never an unqualified force. Artifact auto-merge requires `git.auto_merge`; semantic paths also require `knowledge.auto_promote` and existing promotion verification. Keep privileged code on protected main and never execute PR bytes with the STS token.

- [ ] **Step 6: Update the protected workflow and exact STS documentation**

Document the `geas-pr-skill-sync` identity with repository IDs `228616596`/`1320458746`, workflow-run claim, `contents: write` and `pull_requests: write`, repository `geas`, ordinary read token separation, rulesets, fork behavior, rotation, and removal.

- [ ] **Step 7: Run focused suites and commit**

Run: `uv run pytest tests/test_repository_publisher.py tests/test_pr_skill_sync.py tests/test_promotion.py -v`

Commit: `feat: publish managed repository changes safely`

---

### Task 7: CLI and Configuration Fan-In (`geas-xh0.7`)

**Files:**
- Modify: `src/research_agent/cli.py`
- Modify: `src/research_agent/user_config.py`
- Modify: `src/research_agent/ontology_subscriptions.py`
- Modify: `src/research_agent/ontology_sync.py`
- Create: `tests/test_automatic_acquisition_cli.py`
- Modify: `tests/test_repository_catalog_cli.py`
- Modify: `tests/test_skill_cli.py`
- Modify: `tests/test_ontology_sync.py`

**Interfaces:**
- Consumes: Tasks 2–6 live services and contracts.
- Produces CLI: `ontology-update NAME`; `repository-install NAME URL`; `repository-install --current-repository`; `repository-update NAME`; `repository-remove NAME`; `--trust-repository`; `--read-only`; `--delegate-depth`; `--link`; `--publish {pull-request,none}`; and `--direct-push`.

- [ ] **Step 1: Write failing exact parser and dispatch tests**

```python
def test_repository_install_parser_defaults_to_pull_request_publication() -> None:
    args = build_parser().parse_args([
        "repository-install", "gold", "https://github.com/example/gold.git",
        "--trust-repository", "--link",
    ])
    assert args.publish == "pull-request"
    assert args.direct_push is False
```

Test all command forms, mutual exclusions, full refs, config profile, stdout/stderr separation, announcement before write, sorted receipts, noninteractive denial, and no downstream calls after every failed gate.

- [ ] **Step 2: Run CLI tests and observe missing commands**

Run: `uv run pytest tests/test_automatic_acquisition_cli.py tests/test_repository_catalog_cli.py tests/test_skill_cli.py -v`

- [ ] **Step 3: Wire thin handlers and factories**

Handlers normalize arguments, load the selected trusted profile, construct the evaluator/adapters/services, invoke one domain method, serialize one strict receipt to stdout, and return actionable non-zero exits. They never implement policy in `cli.py`.

- [ ] **Step 4: Gate legacy push settings**

`ontology_git.push_on_update` and `ontology-sync --push` no longer imply authority. Preserve parsing compatibility, but require an exact `git.direct_push` decision and explicit direct-push invocation before remote writes; forward the requested commit message through the safe publisher.

- [ ] **Step 5: Run CLI and cross-stream suites**

Run: `uv run pytest tests/test_automatic_acquisition_cli.py tests/test_repository_catalog_cli.py tests/test_repository_subscription_end_to_end.py tests/test_skill_cli.py tests/test_ontology_sync.py tests/test_capabilities.py tests/test_source_work.py tests/test_repository_bootstrap.py tests/test_repository_publisher.py -v`

- [ ] **Step 6: Commit**

Commit: `feat: expose automatic repository workflows in geas`

---

### Task 8: Maintained Sample, Generated Skills, Documentation, and CI (`geas-xh0.8`)

**Files:**
- Modify: `README.md`
- Modify: `docs/QUICKSTART_ONTOLOGY.md`
- Modify: `docs/REPOSITORY_ONTOLOGIES.md`
- Modify: `docs/AGENT_SKILLS.md`
- Modify: `docs/PROMOTIONS.md`
- Modify: `docs/SOURCE_OF_TRUTH.md`
- Modify: `docs/NEXT_PHASE.md`
- Modify: `ontology/open-source-research-agents/build.yaml`
- Create: `ontology/open-source-research-agents/geas-delegations.yaml`
- Modify: `geas.yaml`
- Modify: `.agents/skills/geas/**`
- Modify: `.agents/skills/open-source-research-agents/**`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_repository_ontology_docs.py`
- Modify: `tests/test_maintained_sample_catalog.py`
- Modify: `tests/test_demo_ontology.py`

**Interfaces:**
- Consumes: Task 7 CLI and every implemented schema.
- Produces: checked examples, current generated skills, a catalog-pinned sample source/delegation declaration, and parallel CI jobs.

- [ ] **Step 1: Write failing executable documentation/sample tests**

Parse every command block through the real parser. Load sample YAML through strict models, verify catalog hashes, run the sample demo twice, generate skills twice, and assert byte identity plus source/repository lookup and install/update/remove guidance.

- [ ] **Step 2: Run docs/sample tests and observe stale behavior**

Run: `uv run pytest tests/test_repository_ontology_docs.py tests/test_maintained_sample_catalog.py tests/test_demo_ontology.py -v`

- [ ] **Step 3: Update operator and agent documentation**

Cover static use without Geas, optional operator-approved pinned installation, config init, repository full/read-only trust, one-hop and overridden delegation, source intent, ontology update, PR default, explicit direct push, App auto-merge, knowledge boundary, receipts, rollback, and removal. Mark Common Crawl/browser automation as future behavior.

- [ ] **Step 4: Update the maintained sample and refresh hashes**

Use fixture-safe or disabled-by-default source intent so normal tests remain offline. Add the delegation manifest to the exact catalog inventory, run `geas catalog-refresh`, verify the catalog, rebuild deterministic projections/skills through Geas, and commit only declared generated artifacts.

- [ ] **Step 5: Split CI into deterministic parallel jobs**

Run capability/security, acquisition/work, bootstrap/publishing, maintained-demo/docs, and full-suite fan-in jobs. PR code has read-only permissions; privileged write-back remains the separate protected workflow.

- [ ] **Step 6: Run maintained checks and commit**

Run: `uv run pytest tests/test_repository_ontology_docs.py tests/test_maintained_sample_catalog.py tests/test_demo_ontology.py tests/test_builtin_geas_skill.py tests/test_pr_skill_sync.py -v`

Commit: `docs: publish automatic geas repository workflow`

---

### Task 9: Canadian Gold-Miner Integration, Security Review, and Release Gate (`geas-xh0.9`)

**Files:**
- Create: `tests/fixtures/automatic_acquisition/gold/geas.yaml`
- Create: `tests/fixtures/automatic_acquisition/gold/ontology/build.yaml`
- Create: `tests/fixtures/automatic_acquisition/gold/ontology/bundle.yaml`
- Create: `tests/fixtures/automatic_acquisition/gold/ontology/library.yaml`
- Create: `tests/fixtures/automatic_acquisition/gold/ontology/geas-delegations.yaml`
- Create: `tests/fixtures/automatic_acquisition/gold/http/issuer-feed.xml`
- Create: `tests/fixtures/automatic_acquisition/gold/http/news.html`
- Create: `tests/fixtures/automatic_acquisition/gold/http/financials.json`
- Create: `tests/test_automatic_acquisition_integration.py`
- Modify only as findings require: files owned by Tasks 2–8, in one reviewed fix wave.

**Interfaces:**
- Consumes: all preceding tasks.
- Produces: one offline end-to-end acceptance contract and final review evidence.

- [ ] **Step 1: Write the failing end-to-end acceptance test**

The test creates a local bare ontology remote and fake public source transport, subscribes and grants one-hop trust, ingests issuer news/regulatory filing/financial JSON, records one `304` and one typed constraint, builds exact anchors/library, creates fixture-model proposals, interrupts/resumes without duplicate calls, exports a repository skill, produces a deterministic PR receipt, and removes only owned state.

- [ ] **Step 2: Run the integration test and verify the first missing cross-stream behavior**

Run: `uv run pytest tests/test_automatic_acquisition_integration.py -v -x`

Expected: fail at the earliest incomplete fan-in behavior, not because of live network, credentials, clock, or model access.

- [ ] **Step 3: Fix cross-stream contract gaps with focused red/green tests**

For each failure, add or narrow the responsible behavioral test first, observe failure, implement the smallest correction, and rerun that focused suite before returning to the end-to-end test. Do not add test-only production hooks or weaken a rejection path.

- [ ] **Step 4: Run security and maintained integration suites**

Run: `uv run pytest tests/test_automatic_acquisition_integration.py tests/test_capabilities.py tests/test_web_sources.py tests/test_source_work.py tests/test_repository_bootstrap.py tests/test_repository_publisher.py tests/test_promotion.py tests/test_demo_ontology.py -v`

- [ ] **Step 5: Run full repository quality gates**

Run:

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest -q
demo_root=$(mktemp -d /tmp/geas-demo.XXXXXX)
./ontology/open-source-research-agents/demo.sh "$demo_root"
uv run geas ontology-build ontology/open-source-research-agents/build.yaml --root /tmp/geas-check --check
uv run geas projection-benchmark --tier smoke
git diff --check
```

- [ ] **Step 6: Request final whole-branch review and remediate once**

Review the complete merge-base-to-HEAD package for spec compliance, security boundaries, deterministic behavior, tests, docs, generated bytes, and deferred Beads findings. Send all Critical/Important findings to one fix worker, then one scoped re-review.

- [ ] **Step 7: Close Beads, commit, push, and update PR #21**

Close completed children and epic only after evidence is recorded. Pull/rebase safely, push the implementation branch, confirm it is even with origin, and update the PR body with architecture, migrations, commands, test evidence, and security notes.

Commit: `test: verify automatic acquisition workflow`
