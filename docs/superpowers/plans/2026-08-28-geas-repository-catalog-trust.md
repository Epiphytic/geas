# Git-Connected Ontology Catalogs and Trust Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make strict `geas.yaml` catalogs, scoped repository trust, immutable snapshots, named Git subscriptions, catalog-aware skill export, and the maintained sample work through one deterministic public workflow.

**Architecture:** Add focused catalog, trust, and resolver modules around the existing strict Pydantic models, user configuration manager, Git synchronization manager, artifact manager, and skill lifecycle. Catalog integrity is verified before trust evaluation; catalog-aware resolution then augments profile ontologies without changing the Git-to-truth-to-projection authority direction.

**Tech Stack:** Python 3.12, Pydantic v2, PyYAML, hashlib/canonical JSON, pathlib, Git subprocesses, argparse, pytest, uv, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-28-geas-repository-catalog-trust-design.md`

## Global Constraints

- Use beads issue `geas-8pz` and its children as the task ledger; claim before implementation, append test/review evidence, and close only after review.
- Write a failing behavioral test and observe the expected failure before each production change.
- `geas.yaml` is the only catalog filename; strict schema version is `1`; bundle format is `geas-ontology-bundle/1`.
- Integrity validation always precedes authorization and neither durable trust nor `--yolo` bypasses hashes, confinement, symlink checks, Git identity/ref checks, artifact compatibility, policy, budgets, approvals, or promotion.
- Repository catalog data may identify ontology inputs only; it cannot configure providers, credentials, policies, budgets, approvals, endpoints, commands, or canonical writes.
- JSON receipts go to stdout; progress, prompts, and diagnostics go to stderr or injected prompt I/O.
- All state changes use confined atomic replacement; normal tests are offline and deterministic with fake Git/network/model boundaries.
- Preserve backward compatibility for `ontology_directory`, `ontology_git`, `ontology_git.branch`, `ontology-list`, and existing single-repository `ontology-sync` calls.
- Keep ontology workers bounded to 30 minutes; current official primary sources only; model output remains proposal-only.
- Do not commit runtime stores, acquired private material, `.env`, logs, SQLite files, `.DS_Store`, or the user's unrelated AGENTS/CLAUDE/BEADS changes.

---

### Task 1: Strict Catalog Integrity and Nested Discovery (`geas-8pz.1`)

**Files:**
- Create: `src/research_agent/repository_catalog.py`
- Create: `tests/test_repository_catalog.py`
- Modify: `src/research_agent/ontology_catalog.py`

**Interfaces:**
- Produces: `CatalogFile`, `CatalogOntology`, `RepositoryCatalog`, `VerifiedCatalogOntology`, `ResolvedRepositoryCatalog`; `load_catalog(path: Path) -> RepositoryCatalog`; `verify_catalog(path: Path, *, names: Sequence[str] = ()) -> Sequence[VerifiedCatalogOntology]`; `refresh_catalog(path: Path, *, names: Sequence[str] = ()) -> RepositoryCatalog`; `discover_catalogs(start: Path) -> Sequence[Path]`; `resolve_repository_catalog(start: Path) -> ResolvedRepositoryCatalog`.
- Consumes: `StrictModel` and the existing canonical JSON serializer from `research_agent.models`.

- [ ] **Step 1: Write failing strict-schema and digest-vector tests**

```python
def test_catalog_digest_is_portable_and_metadata_sensitive(tmp_path: Path) -> None:
    ontology = _catalog_ontology(tmp_path, description="first")
    verified = verify_catalog(tmp_path / "geas.yaml")
    assert verified[0].bundle_sha256 == ontology["bundle_sha256"]
    moved = tmp_path / "moved"
    shutil.copytree(tmp_path / "ontology", moved / "ontology")
    _write_catalog(moved / "geas.yaml", ontology)
    assert verify_catalog(moved / "geas.yaml")[0].bundle_sha256 == ontology["bundle_sha256"]
    ontology["description"] = "second"
    _write_catalog(moved / "geas.yaml", ontology)
    with pytest.raises(ValueError, match="bundle digest"):
        verify_catalog(moved / "geas.yaml")
```

Add literal fixtures for extra fields, unsorted/duplicate files, absolute/parent/control-character paths, catalog/directory/file symlinks, missing/non-regular files, size/hash/bundle mismatches, and an undeclared build/library/source-card input.

- [ ] **Step 2: Run catalog tests and confirm the missing-module failure**

Run: `uv run pytest tests/test_repository_catalog.py -v`

Expected: collection fails because `research_agent.repository_catalog` does not exist.

- [ ] **Step 3: Implement strict models, canonical digest, verification, and atomic refresh**

```python
class CatalogFile(StrictModel):
    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)

class CatalogOntology(StrictModel):
    name: str
    description: str = Field(min_length=1)
    path: Path
    files: Sequence[CatalogFile]
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

class RepositoryCatalog(StrictModel):
    version: Literal[1] = 1
    ontologies: Sequence[CatalogOntology]

def ontology_bundle_sha256(entry: CatalogOntology) -> str:
    payload = {
        "description": entry.description,
        "files": [item.model_dump(mode="json") for item in entry.files],
        "format": "geas-ontology-bundle/1",
        "name": entry.name,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()
```

Normalize paths lexically before `resolve()`, reject every symlink component, require ascending UTF-8 inventory order, verify exact bytes/size, and scan known transitive YAML path fields only to prove repository inputs are declared. Refresh recalculates existing records only and replaces `geas.yaml` atomically.

- [ ] **Step 4: Write failing Git-root ancestor merge tests**

```python
def test_nested_catalogs_merge_complete_entries_from_root_to_cwd(git_repo: Path) -> None:
    root_entry = _entry("shared", "ontology/root")
    inner_entry = _entry("shared", "ontology/inner")
    _write_catalog(git_repo / "geas.yaml", root_entry, _entry("root-only", "ontology/a"))
    _write_catalog(git_repo / "service" / "geas.yaml", inner_entry)
    result = resolve_repository_catalog(git_repo / "service" / "api")
    assert [item.name for item in result.ontologies] == ["root-only", "shared"]
    assert result.by_name("shared").ontology_path == git_repo / "service/ontology/inner"
```

Also assert no sibling/descendant/above-root recursion, malformed outer failure, non-Git empty discovery, deterministic Git identity/ref/commit receipts, and machine-local identity when origin is absent.

- [ ] **Step 5: Implement direct-ancestor discovery and complete-entry merge**

Use `git -C START rev-parse --show-toplevel`, build only the root-to-CWD ancestor chain, parse every present `geas.yaml`, verify winning entries at their declaring paths, normalize HTTPS/SSH GitHub origins to one credential-free identity, and expose dirty status for each declared inventory.

- [ ] **Step 6: Run focused tests and commit**

Run: `uv run pytest tests/test_repository_catalog.py tests/test_ontology_catalog.py -v`

Commit: `feat: add deterministic repository ontology catalogs`

---

### Task 2: Scoped Trust, Prompt Decisions, and Immutable Snapshots (`geas-8pz.2`)

**Files:**
- Create: `src/research_agent/ontology_trust.py`
- Create: `tests/test_ontology_trust.py`
- Modify: `src/research_agent/user_config.py`
- Modify: `tests/test_user_config.py`

**Interfaces:**
- Consumes: `VerifiedCatalogOntology` and repository identity/ref/dirty metadata from Task 1.
- Produces: `TrustRule`, `TrustContext`, `TrustDecision`, `TrustPrompt` protocol, `evaluate_trust(context: TrustContext, rules: Sequence[TrustRule]) -> TrustDecision`; `authorize_repository_catalog(catalog: ResolvedRepositoryCatalog, profile: GeasProfile, *, yolo: bool, prompt: TrustPrompt | None) -> Sequence[AuthorizedOntology]`; `install_snapshot(ontology: VerifiedCatalogOntology, *, manager: UserConfigManager, profile_name: str) -> InstalledOntologySnapshot`; `remove_snapshot(snapshot: InstalledOntologySnapshot, *, manager: UserConfigManager, profile_name: str) -> SnapshotRemovalReceipt`.

- [ ] **Step 1: Write failing rule-specificity and dirty-ref tests**

```python
@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        ((_rule(True, refs="*", paths="*", digests="*"),), True),
        ((_rule(True, refs=("refs/heads/main",), paths="*", digests="*"),), True),
        ((_rule(True, refs="*", paths=("ontology/a",), digests="*"),), True),
        ((_rule(True, refs="*", paths="*", digests=(DIGEST,)),), True),
    ],
)
def test_trust_rule_scopes_resolve(rules: Sequence[TrustRule], expected: bool) -> None:
    assert evaluate_trust(CONTEXT, rules).allowed is expected

def test_equal_specificity_deny_wins() -> None:
    rules = (_rule(True, paths=("ontology/a",)), _rule(False, paths=("ontology/a",)))
    assert evaluate_trust(CONTEXT, rules).allowed is False
```

Assert score ordering `digest=4`, `path=2`, `ref=1`, duplicate selector rejection, normalized refs/ref sets/commit IDs, changed origin invalidation, and that ref-only allow does not cover dirty declared bytes.

- [ ] **Step 2: Run trust tests and confirm missing interfaces**

Run: `uv run pytest tests/test_ontology_trust.py tests/test_user_config.py -v`

Expected: collection fails on missing trust types.

- [ ] **Step 3: Add strict trusted configuration and deterministic evaluator**

```python
class TrustRule(StrictModel):
    decision: Literal["allow", "deny"]
    repository: str
    refs: Literal["*"] | Sequence[str]
    paths: Literal["*"] | Sequence[Path]
    bundle_sha256: Literal["*"] | Sequence[str]
    created_at: datetime
    created_via: Literal["interactive", "manual"]

def _specificity(rule: TrustRule) -> int:
    return (0 if rule.bundle_sha256 == "*" else 4) + (0 if rule.paths == "*" else 2) + (0 if rule.refs == "*" else 1)
```

Add `trust_rules` and immutable `installed_ontologies` to `GeasProfile`; preserve strict validation and atomic config writes through `UserConfigManager.replace(config)`.

- [ ] **Step 4: Write failing tests for four prompts, noninteractive mode, and yolo**

Use an injected prompt fake whose responses are `1`, `2` plus per-entry booleans, `3` plus selected names, and `4`. Assert choices 3/4 persist a deny, choice 1 persists wildcard allow, choice 2 persists exact selected selectors, stdout remains one JSON object, and `--yolo` leaves config bytes identical. For every integrity-corrupt fixture, assert neither prompt nor snapshot/config writer is called.

- [ ] **Step 5: Implement authorization and transactional snapshot lifecycle**

Snapshots live at `CONFIG_ROOT/snapshots/ONTOLOGY/BUNDLE_SHA256`; copy only verified inventory to a sibling temporary directory, re-run verification there, atomically rename, then atomically register. Exact removal validates the registered resolved destination, rejects symlinks, and removes only the digest directory plus empty direct parents.

- [ ] **Step 6: Run focused tests and commit**

Run: `uv run pytest tests/test_ontology_trust.py tests/test_user_config.py -v`

Commit: `feat: add scoped ontology trust and snapshots`

---

### Task 3: Named Subscriptions and Generic Git Refs (`geas-8pz.3`)

**Files:**
- Create: `src/research_agent/ontology_subscriptions.py`
- Create: `tests/test_ontology_subscriptions.py`
- Modify: `src/research_agent/user_config.py`
- Modify: `src/research_agent/ontology_sync.py`
- Modify: `tests/test_ontology_sync.py`

**Interfaces:**
- Produces: `OntologySubscription`, `NormalizedProfile`; `GeasProfile.normalized_subscriptions() -> dict[str, OntologySubscription]`; `OntologyRepositoryManager` operating on `active_ref`; `SubscriptionManager.subscribe`, `.unsubscribe`, `.sync`.
- Consumes: catalog verification from Task 1 and config atomic replacement from Task 2.

- [ ] **Step 1: Write failing configuration normalization and ref tests**

```python
def test_legacy_git_profile_normalizes_to_primary_subscription() -> None:
    profile = GeasProfile(ontology_git=OntologyGitConfig(url=URL, branch="release/v1"))
    subscription = profile.normalized_subscriptions()["primary"]
    assert subscription.active_ref == "refs/heads/release/v1"
    assert subscription.checkout == Path("ontologies")
    assert subscription.catalog == Path("geas.yaml")
```

Use local bare remotes to cover full branch/tag refs and 40/64-character commit IDs, exact fetched object verification, sorted multi-sync receipts, read-only tag/commit pushes, and compatibility of old single-repository calls.

- [ ] **Step 2: Run subscription/sync tests and observe expected failures**

Run: `uv run pytest tests/test_ontology_subscriptions.py tests/test_ontology_sync.py tests/test_user_config.py -v`

- [ ] **Step 3: Implement strict subscriptions and generic-ref Git synchronization**

```python
class OntologySubscription(StrictModel):
    url: str
    active_ref: str = "refs/heads/main"
    checkout: Path
    catalog: Path = Path("geas.yaml")
    remote: str = "origin"
    pull_before_update: bool = False
    push_on_update: bool = False
```

Fetch exact full refs into a private Geas tracking ref, peel to a commit, checkout branch refs with fast-forward-only integration, detach tags/commit IDs at the verified object, and permit pushes only for `refs/heads/*`.

- [ ] **Step 4: Write failing transactional subscribe/unsubscribe tests**

Assert input validation precedes writes, clone/fetch/catalog/trust failure restores byte-identical config, new temporary checkouts alone are removed, default unsubscribe preserves checkout, exact clean `--remove-checkout` succeeds, and dirty/origin-mismatched checkout is preserved with actionable failure.

- [ ] **Step 5: Implement `SubscriptionManager` transactions**

Validate name, credential-free remote URL, confined checkout/catalog paths and ref before staging config; synchronize into an exact temporary checkout for new subscriptions; verify catalog; invoke injected authorization; atomically install checkout/config. Sync requested names in sorted order and return individual success/failure receipts without losing successful siblings.

- [ ] **Step 6: Run focused tests and commit**

Run: `uv run pytest tests/test_ontology_subscriptions.py tests/test_ontology_sync.py tests/test_user_config.py -v`

Commit: `feat: support named ontology subscriptions`

---

### Task 4: Catalog-Aware Resolution and CLI Orchestration (`geas-8pz.4`)

**Files:**
- Create: `src/research_agent/ontology_resolution.py`
- Create: `tests/test_ontology_resolution.py`
- Create: `tests/test_repository_catalog_cli.py`
- Modify: `src/research_agent/cli.py`
- Modify: `src/research_agent/paths.py`
- Modify: `src/research_agent/ontology_catalog.py`

**Interfaces:**
- Produces: `OntologyCandidate`, `OntologySelection`, `resolve_ontology_catalog(*, user_config: GeasUserConfig, manager: UserConfigManager, cwd: Path, yolo: bool, prompt: TrustPrompt | None) -> OntologyCatalog`; `select_ontology(name: str, *, catalog: OntologyCatalog) -> OntologySelection`; CLI commands `list`, `catalog-verify`, `catalog-refresh`, `ontology-subscribe`, `ontology-unsubscribe`, and multi-name `ontology-sync`; global `--yolo`.
- Consumes: Tasks 1-3 catalog, authorization, snapshot, subscription, and configuration interfaces.

- [ ] **Step 1: Write failing profile/repository augmentation and ambiguity tests**

```python
def test_repository_catalog_augments_profile_without_shadowing(tmp_path: Path) -> None:
    result = resolve_ontology_catalog(profile=_profile("profile-only"), cwd=_repo("repo-only"))
    assert [item.name for item in result.candidates] == ["profile-only", "repo-only"]

def test_same_name_from_profile_and_repository_is_explicitly_ambiguous(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ambiguous ontology 'shared'"):
        select_ontology("shared", profile=_profile("shared"), cwd=_repo("shared"))
```

Assert `list` may show inert untrusted candidates, but all operational selections authorize before build/library/artifact parsing.

- [ ] **Step 2: Run resolver tests and observe missing behavior**

Run: `uv run pytest tests/test_ontology_resolution.py -v`

- [ ] **Step 3: Implement catalog-aware resolution**

Build candidates from every normalized subscription catalog, installed snapshot, legacy direct-child profile ontology, and local ancestor catalog. Sort by name/source, preserve repository/ref/commit/catalog/path/digest/trust metadata, and require explicit disambiguation instead of precedence across sources.

- [ ] **Step 4: Write failing CLI tests for exact parser and side effects**

Exercise the exact commands from the spec. Verify `geas list` and `geas ontology-list` equivalent payloads, nested-CWD merge, verification/refresh selection, four prompt flows, non-TTY refusal, global `--yolo`, subscribe rollback, sorted sync receipts, unsubscribe preservation/removal, stdout JSON, stderr progress, and absence of model/network/artifact/canonical operations after any gate failure.

- [ ] **Step 5: Wire parser and handlers through domain services**

Add `--yolo` globally and command signatures exactly:

```text
geas list [DIRECTORY]
geas catalog-verify [CATALOG]
geas catalog-refresh [CATALOG] [ONTOLOGY [ONTOLOGY]]
geas ontology-subscribe NAME URL [--ref REF] [--catalog PATH]
geas ontology-unsubscribe NAME [--remove-checkout]
geas ontology-sync [NAME [NAME]] [--pull] [--push]
```

Keep handlers thin, serialize strict receipts once, and route prompts/progress exclusively to stderr or injected TTY.

- [ ] **Step 6: Adapt named ontology paths and run focused CLI tests**

Replace `root / name` assumptions in build/library/artifact command resolution with `OntologySelection.ontology_directory`; propagate the declaring subscription to hydration/publishing. Run: `uv run pytest tests/test_repository_catalog_cli.py tests/test_ontology_resolution.py tests/test_ontology_catalog.py tests/test_ontology_sync.py -v`

- [ ] **Step 7: Commit**

Commit: `feat: resolve repository ontologies through the cli`

---

### Task 5: Skill Export and Update Provenance (`geas-8pz.5`)

**Files:**
- Modify: `src/research_agent/agent_skills.py`
- Modify: `src/research_agent/cli.py`
- Modify: `src/research_agent/builtin_skills/geas/references/cli.md`
- Modify: `src/research_agent/builtin_skills/geas/references/skills.md`
- Create: `tests/test_catalog_skill_lifecycle.py`

**Interfaces:**
- Consumes: `OntologySelection` and declaring subscription from Task 4.
- Produces: skill manifest provenance fields `repository_url`, `active_ref`, `ontology_commit`, `catalog_path`, `ontology_path`, `bundle_sha256`, and portable artifact identity; update re-resolves and verifies the same authority chain.

- [ ] **Step 1: Write failing export/update provenance tests**

Create an offline subscription checkout with a verified catalog and preseeded current-schema projection artifact. Export by ontology name and assert every provenance field, hierarchical links, repository URL and Geas lookup instructions. Change catalog bytes without matching hashes and assert update preserves the previous complete snapshot.

- [ ] **Step 2: Run lifecycle tests and observe provenance failure**

Run: `uv run pytest tests/test_catalog_skill_lifecycle.py tests/test_skill_end_to_end.py -v`

- [ ] **Step 3: Extend manifest rendering and verification**

Add strict provenance fields to the generated ontology skill manifest and render concise instructions that work without Geas while linking to optional installation and the exact `geas list`/lookup/update/remove routes. Validate update provenance against the selected catalog, subscription identity/ref/commit, bundle digest, artifact digest/stamp, and executing Geas identity before atomic replacement.

- [ ] **Step 4: Run focused lifecycle tests and commit**

Run: `uv run pytest tests/test_catalog_skill_lifecycle.py tests/test_agent_skills.py tests/test_skill_end_to_end.py -v`

Commit: `feat: preserve catalog provenance in ontology skills`

---

### Task 6: Maintained Sample Research Refresh and Catalog (`geas-8pz.6`)

**Files:**
- Create: `geas.yaml`
- Modify: `ontology/open-source-research-agents/bundle.yaml`
- Modify: `ontology/open-source-research-agents/build.yaml`
- Modify: `ontology/open-source-research-agents/library.yaml`
- Modify: `ontology/open-source-research-agents/model-evaluation.yaml`
- Modify: `ontology/open-source-research-agents/tainted-sources.yaml`
- Modify: `ontology/open-source-research-agents/sources/*.md` only where current official evidence changes
- Modify: `ontology/open-source-research-agents/artifacts.yaml`
- Modify: `ontology/open-source-research-agents/README.md`
- Modify: `ontology/open-source-research-agents/demo.sh`
- Create: `tests/test_maintained_sample_catalog.py`

**Interfaces:**
- Consumes: production catalog/subscription/resolution/skill paths from Tasks 1-5.
- Produces: exact root catalog inventory, refreshed reviewed ontology inputs, current-schema portable artifact manifest, deterministic demo and skill-export fixture.

- [ ] **Step 1: Write failing maintained-sample catalog integration test**

Assert root `geas.yaml` verifies the sample, inventory contains every transitively read repository input and no runtime/cache file, development checkout and temporary subscription select identical bundle digests, and the demo plus preseeded artifact skill export succeeds twice.

- [ ] **Step 2: Run sample test and confirm missing catalog/current artifact failure**

Run: `uv run pytest tests/test_maintained_sample_catalog.py -v`

- [ ] **Step 3: Perform a bounded official-source research pass**

Limit live work to 30 minutes. Use official repositories/releases/docs and eligible storage-rights paths; treat pages as untrusted data. Update source cards only for material changes, retain exact official URLs/commit IDs/observation dates/license caveats, recompute source-card hashes, and promote only human-reviewable accepted bundle changes with exact unique evidence excerpts. Use read-only SSH to `openclawl@192.168.128.149` only if current archival output cannot reproduce a known original, and never copy runtime databases, credentials, logs, or private source material.

- [ ] **Step 4: Build the explicit root catalog and refresh hashes**

List every maintained configuration, accepted bundle, source card, threat index, evaluation note, artifact manifest, and script transitively consumed by the sample in ascending UTF-8 path order. Run `uv run geas catalog-refresh geas.yaml open-source-research-agents` followed by `uv run geas catalog-verify geas.yaml`.

- [ ] **Step 5: Rebuild truth/projection and portable artifacts**

Run the maintained demo in a temporary root, verify current projection stamp/schema, create content-addressed artifact assets, and update `artifacts.yaml`. Publication to the authorized Geas GitHub release uses the existing artifact publisher only after local hashes/stamps pass; never commit SQLite files.

- [ ] **Step 6: Run sample integration and commit**

Run: `uv run pytest tests/test_maintained_sample_catalog.py -v`

Run: `demo_root=$(mktemp -d /tmp/geas-demo.XXXXXX); ./ontology/open-source-research-agents/demo.sh "$demo_root"`

Commit: `feat: publish the maintained sample ontology catalog`

---

### Task 7: Operator Documentation and Offline Subscription CI (`geas-8pz.7`)

**Files:**
- Modify: `README.md`
- Modify: `docs/USER_CONFIG.md`
- Modify: `docs/GETTING_STARTED.md`
- Modify: `docs/QUICKSTART_ONTOLOGY.md`
- Modify: `docs/AGENT_SKILLS.md`
- Modify: `docs/PORTABLE_ONTOLOGY_ARTIFACTS.md`
- Modify: `.github/workflows/ci.yml`
- Create: `tests/test_repository_subscription_end_to_end.py`

**Interfaces:**
- Consumes: all production workflows and maintained sample from Tasks 1-6.
- Produces: concise install/trust/update/remove docs and one offline production-path CI integration contract.

- [ ] **Step 1: Write failing offline end-to-end test**

Create a temporary checkout from the current repository, assign the public origin without fetching, isolate config/home, disable freshness fetches, register it as `geas-samples`, exercise durable trust and `--yolo` separately, list at root/nested paths, verify sample, run demo/projection and preseeded skill export twice, then unsubscribe and remove one exact installed snapshot. Assert config bytes do not change under yolo and no live network/model call occurs.

- [ ] **Step 2: Run end-to-end test and observe the uncovered workflow**

Run: `uv run pytest tests/test_repository_subscription_end_to_end.py -v`

- [ ] **Step 3: Add concise README and detailed operator guidance**

Include the approved commands verbatim:

```bash
geas ontology-subscribe geas-samples https://github.com/Epiphytic/geas.git \
  --ref refs/heads/main
geas list
geas skill-export open-source-research-agents --link
```

State that subscribe announces synchronization and asks for trust by default; explain four choices, process-only `--yolo`, nested `geas.yaml`, exact snapshot installation/removal, unsubscribe default preservation and `--remove-checkout`, legacy normalization, artifact/source-reference behavior, and optional Geas installation link.

- [ ] **Step 4: Add the offline GitHub Actions integration step**

Run the new end-to-end test separately after unit tests and before the maintained demo. The fixture must use the workflow checkout as its local object source and must not contact GitHub or an external model.

- [ ] **Step 5: Run docs/integration tests and commit**

Run: `uv run pytest tests/test_repository_subscription_end_to_end.py tests/test_catalog_skill_lifecycle.py tests/test_maintained_sample_catalog.py -v`

Commit: `test: verify offline ontology subscriptions in ci`

---

### Task 8: Whole-Branch Security Review and Verification (`geas-8pz.8`)

**Files:**
- Modify: only files required by findings.
- Test: all focused and full suites.

**Interfaces:**
- Consumes: Tasks 1-7 and every ruling recorded in the SDD/beads ledgers.
- Produces: reviewed, verified, committed, and pushed feature branch ready for PR review.

- [ ] **Step 1: Dispatch a whole-branch reviewer against the spec**

Review integrity-before-trust ordering, repository text authority, path/symlink confinement, generic ref verification, dirty-file handling, atomic rollback/removal scope, stdout/stderr separation, skill provenance, source/evidence integrity, rejection-side-effect assertions, compatibility, and every deferred finding.

- [ ] **Step 2: Fix all load-bearing review findings through one subagent and re-review**

Use one fix wave, rerun exact covering tests, and obtain a scoped review of the fix diff. Record any adjudicated residual finding and its cost if wrong in beads and the SDD ledger.

- [ ] **Step 3: Run required quality gates from a clean test environment**

Run:

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest -q
git diff --check
demo_root=$(mktemp -d /tmp/geas-final-demo.XXXXXX)
./ontology/open-source-research-agents/demo.sh "$demo_root"
```

Expected: every command exits zero; no unexpected tracked or runtime files appear.

- [ ] **Step 4: Close beads, commit exact feature files, and synchronize**

Close completed child issues and `geas-8pz`, run `bd preflight`, `bd dolt pull`, and `bd dolt push` where configured. Preserve unrelated user changes, commit only feature files and passive beads export when appropriate, then pull/rebase and push the feature branch. Confirm `git status --short --branch` reports the branch up to date with its upstream apart from the known preserved user files.
