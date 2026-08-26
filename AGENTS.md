# Agent guide for Geas

This file is the source-code orientation and change guide for coding agents.
Read it before editing. Geas is a security-sensitive research knowledge system:
many constraints that look conservative are deliberate authority boundaries,
not incidental implementation details.

## Mission and intended use

Geas creates and maintains reusable research knowledge rather than treating a
generated report as the durable product. It separates four independently useful
capabilities:

1. **Discovery and acquisition** find candidate sources through local,
   scholarly, open-web, and official-repository interfaces, then preserve
   eligible content.
2. **Source libraries** collect immutable parsed sources into deterministic,
   exact-text search indexes. Humans and agents can query these without first
   building an ontology.
3. **Ontology construction and maintenance** turn selected source anchors into
   reviewable concepts, claims, evidence, controversies, and gaps. Work is
   bounded, resumable, and reusable across models.
4. **Knowledge retrieval** exposes accepted ontology data and source fragments
   through deterministic FTS, hierarchy, provenance, temporal, citation,
   dissent, gap, and threat queries.

Typical uses include:

- building a maintained body of knowledge about a topic;
- giving another agent small, attributable context packages without making
  embeddings or an LLM the retrieval authority;
- searching a document or repository corpus before choosing an ontology;
- refreshing source versions and filling known gaps over time;
- preserving dissent and uncertainty as queryable data;
- acquiring scholarly metadata and eligible open-access documents;
- reviewing and promoting model proposals through Git; and
- detecting drift between canonical files, immutable history, and disposable
  query projections.

Start with `README.md`, `docs/USE_CASES.md`, and
`docs/QUICKSTART_ONTOLOGY.md` for operator-facing workflows. The CLI is
`geas`; the Python package remains `research_agent`.

## The governing mental model

### Source text and model output are untrusted data

Retrieved text never becomes an instruction. Model output never becomes an
authorization decision. Models can propose typed data but must not select
tools, endpoints, credentials, policies, budgets, approvals, workflow
transitions, or canonical writes.

Keep prompt-injection defenses deterministic. Do not add an LLM classifier,
critic agent, or second prompt as a security boundary: it would inherit the
same hostile input. The fixed workflow, strict schemas, path/host confinement,
threat scanner, policy engine, budget ledger, and Git verification are the
boundaries.

### Authority flows in one direction

The authority order is:

```text
Git ontology and policy files
  -> validated immutable records and source blobs
  -> truth snapshot
  -> SQLite and Markdown projections
  -> reports and model answers
```

Never write information from a later layer back into an earlier one
automatically. In particular:

- files accepted in Git `HEAD` are the canonical maintained ontology;
- immutable JSON records and blobs preserve evidence and history;
- validated extraction remains proposal-only;
- a verified Git promotion manifest is the bridge to accepted knowledge;
- `query.sqlite`, library SQLite files, and generated Markdown are rebuildable;
  and
- SQLite drift is repaired by rebuilding, never by blessing its rows as truth.

The exception is `usage.sqlite`: it is authoritative operational accounting for
budget reservations and settlements, but has no authority over ontology
meaning.

See `docs/SOURCE_OF_TRUTH.md` and `docs/PROMOTIONS.md` before changing
canonicalization, promotion, or projection code.

### Provenance is part of the data model

A search result or snippet is a lead, not evidence. Accepted claims must trace
to exact selectors over acquired source versions. Preserve:

- immutable source bytes and content hashes;
- parsed-text derivation identity and parser provenance;
- structural anchors, offsets, and exact-text hashes;
- authors, publisher, license, usage conditions, rights basis, and acquisition
  provenance when known, with `unknown` as the safe default;
- normalized identifiers and conservative citation relations; and
- observation, valid, recorded, and refresh times where the schema provides
  them.

Do not silently infer permissions from “open access,” a URL, a snippet, or a
vague license class.

### Dissent, gaps, and tainted sources are first-class

Do not flatten competing positions into a single synthetic answer.
`Controversy` records connect distinct claims; `KnowledgeGap` records keep
missing or stale knowledge actionable. Threat observations are version- and
topic-scoped evidence, not timeless global domain labels. Keep suspected or
confirmed hostile sources queryable as threat data while preventing them from
supporting accepted claims.

### Long work must be bounded and resumable

An ontology worker should normally run for no more than 30 minutes. Completed
discovery, acquisition, parsing, extraction proposals, and candidates must
survive interruption. A resumed worker may use a different provider, model,
reasoning effort, or output ceiling without discarding compatible earlier
work. Prefer checkpoints and small deterministic units over one enormous model
request.

Configuration caps and limits belong in explicit per-ontology configuration.
`ontology-init` must continue to write every default, including null and empty
values, so operators can inspect and change behavior without discovering
hidden application defaults.

## Repository map

### Entry points and foundational records

- `pyproject.toml` defines the `geas` executable and the
  `research_agent` package.
- `src/research_agent/cli.py` is the CLI composition root. It parses commands,
  loads trusted configuration and allowlisted secrets, constructs components,
  renders JSON to stdout, and assigns exit codes. Keep domain logic in the
  modules below rather than growing command handlers unnecessarily.
- `src/research_agent/models.py` holds shared strict Pydantic records,
  content-derived IDs, canonical JSON, source/evidence/claim records, threat
  records, provider configuration, and model parameters.
- `src/research_agent/store.py` is the content-addressed blob and immutable
  record store. Data must already be validated before crossing this boundary.
- `src/research_agent/workflow.py` defines the fixed state machine. Models must
  never choose or bypass its transitions.

`StrictModel` rejects extra fields. Preserve that fail-closed behavior for
internal and model-facing protocols. Generate identities from canonical
content rather than mutable counters or database row IDs.

### Discovery, planning, and acquisition

- `src/research_agent/discovery.py` defines connector capabilities, query
  plans, discovery hits, acquisition attempts, access constraints, open-access
  resolutions, coverage records, and connector manifests.
- `src/research_agent/planning.py` validates deterministic or model-proposed
  query plans against trusted vocabulary, connector capabilities, and limits.
- `src/research_agent/research.py` executes discovery and the offline local
  research slice.
- `src/research_agent/connectors/` contains narrow connector implementations:
  `local_file.py`, `crossref.py`, `openalex.py`, `europe_pmc.py`, `mojeek.py`,
  and `unpaywall.py`.
- `src/research_agent/discovery_acquisition.py` resolves supported GitHub
  discovery results through official APIs and records immutable repository
  snapshots.
- `src/research_agent/remote_acquisition.py` performs license-gated,
  public-network-confined document fetching.
- `src/research_agent/operator_policy.py` loads discovery priority, retention,
  credential-name, storage-rights, and request/cost constraints.
- `src/research_agent/identifiers.py` normalizes DOI, PMID, PMCID, ORCID, ROR,
  and ISSN values.

Connector transports use fixed HTTPS hosts, reject unsafe redirects and
credential-bearing or private destinations, and avoid persisting raw response
bodies unless policy explicitly allows it. Keep transport, normalization, and
record persistence separate. External response models may tolerate irrelevant
upstream fields; internal records must remain strict.

Brave is present only as disabled policy configuration; there is no implemented
Brave connector. Mojeek discovery results are transient under the current
unconfirmed storage rights. Do not represent either as more complete than it
is.

### Deposits, parsing, structure, and citations

- `src/research_agent/deposits.py` archives user-provided files with explicit
  provenance, handling, permission, and optional Nostr ownership evidence.
- `src/research_agent/parsing.py` preserves originals and creates versioned
  inert-text derivations for supported document formats.
- `src/research_agent/sandbox.py` wraps native parsers in Bubblewrap. Failure to
  establish the sandbox must fail closed; never add an unsandboxed fallback.
- `src/research_agent/structure.py` derives deterministic document/page/
  section/heading/block anchors and exact ranges.
- `src/research_agent/citations.py` derives normalized identifier nodes and
  conservative exact-range citation relations.

Original bytes and parsed text are different immutable artifacts. Parsing does
not upgrade trust. Structural and citation derivation is local, deterministic,
and tool-free.

### Knowledge, ontologies, and model proposals

- `src/research_agent/knowledge.py` defines concepts, controversies, gaps,
  topic/source associations, reviewed knowledge packs, the deterministic
  threat scanner, and accepted import validation.
- `src/research_agent/bundles.py` imports maintained, path-confined,
  SHA-256-pinned ontology bundles and source cards.
- `src/research_agent/extraction.py` builds anchor-grounded prompts, records
  redacted prompt/reasoning audit logs, validates model JSON, and stores
  proposal-only extraction records.
- `src/research_agent/providers.py` implements tool-free local/API clients and
  isolated Codex/Claude one-shot providers.
- `src/research_agent/model_policy.py` deterministically authorizes the exact
  provider, endpoint, model, operation, data class, input kind, and input hash
  before external model use.
- `src/research_agent/model_evaluation.py` measures, compares, finds, and
  slices validated proposals without changing their authority.
- `src/research_agent/candidate_bundles.py` renders validated proposals as
  reviewable repository-scoped candidate bundles.
- `src/research_agent/promotion.py` stages, verifies, and applies forge-neutral
  Git promotion manifests.
- `src/research_agent/ontology_build.py` is the resumable orchestration loop:
  seed import, gap/query discovery, repository acquisition, threat handling,
  anchor selection, model extraction, candidate writing, audit, truth capture,
  projection, and topic export.
- `src/research_agent/audit.py` performs model-free checks for missing or
  tainted evidence, dissent quality, freshness, resolution consistency, and
  retraction signals.

Model prompts must say exactly which strict JSON schema is required and must
not ask for fields the validator forbids. A truncated response is not a schema
failure: surface output-token exhaustion with recommendations to raise the
configured limit, choose a capable model, or split source extraction. Do not
silently shrink ontology scope to fit a model.

Codex and Claude one-shots are ontology proposal generators, not general coding
agents inside the build. Keep their workspace empty, tools denied, search
disabled, and output schema constrained. All results still pass the same
grounding validator.

### Libraries, projection, and retrieval

- `src/research_agent/library.py` defines inspectable source-library manifests,
  immutable snapshots, deterministic FTS query compilation, exact anchor hits,
  and bounded agent context packages.
- `src/research_agent/projection.py` atomically builds the knowledge SQLite
  projection and implements deterministic knowledge, hierarchy, provenance,
  dissent, temporal, gap, threat, anchor, citation, and proposal queries.
- `src/research_agent/render.py` creates deterministic agent-readable Markdown
  topic views.
- `src/research_agent/truth.py` inventories canonical Git files and immutable
  state, creates/verifies truth snapshots, and stamps/checks SQLite projections.
- `src/research_agent/benchmark.py` and `src/research_agent/workload.py` define
  reproducible workload tiers and projection performance measurements.

Natural-language retrieval currently compiles to deterministic lexical FTS,
typed SQL parameters, hierarchy traversal, and exact filters. Do not introduce
LLM ranking as an invisible retrieval step. Embeddings may become an explicit
recall aid, but cannot become the source of truth or remove provenance and
threat context.

### Policy, approvals, budgets, and secrets

- `src/research_agent/policy.py` evaluates structured threat observations for a
  fixed workflow stage.
- `src/research_agent/budget.py` transactionally reserves worst-case model/API
  use and settles actual usage.
- `src/research_agent/approvals.py` binds expiring single-use overrides to an
  authenticated local principal and exact request.
- `src/research_agent/secrets.py` parses only explicitly allowlisted `.env`
  names without shell interpolation.

Subscription or enterprise-accounted calls may be excluded from dollar totals,
but never from call caps, token limits, routing, or audit. Reserve before
network I/O. On interruption, an unsettled reservation remains conservatively
charged.

## Trusted configuration

Live operator configuration is installed in the OS-standard Geas user config
root (`~/.config/geas` on Linux without an XDG override). Checked-in `config/`
files and `src/research_agent/default_config/` are synchronized packaged
templates and maintained-demo inputs, not the normal live CLI defaults after
`config-init`. Configuration is versioned because it changes system behavior:

- `providers.toml` (template `config/providers.toml`): provider kind, fixed endpoint, model, key variable,
  external/local status, output capacity, and context capacity.
- `model-policy.yaml` (template `config/model-policy.yaml`): which external provider/model/operation/data
  combinations are allowed.
- `budget-policy.yaml` (template `config/budget-policy.yaml`): automatic envelopes, account treatment, and hard
  usage caps.
- `research-policy.yaml` (template `config/research-policy.yaml`): connector order, credential names, retention,
  storage rights, and request/cost limits.
- `source-policy.yaml` (template `config/source-policy.yaml`): deterministic action by threat status, severity,
  and workflow stage.
- `deposit-policy.yaml` (template `config/deposit-policy.yaml`): provenance and handling defaults for user
  deposits.
- `query-vocabulary.yaml` (template `config/query-vocabulary.yaml`): allowed query concepts, connectors, fields,
  and synonyms.
- `truth-policy.yaml` (template `config/truth-policy.yaml`): canonical ontology/policy/schema inventory and
  required drift action.
- `workload-policy.yaml` (template `config/workload-policy.yaml`): deployment concurrency and benchmark tiers.

When adding a provider or connector, update its implementation, trusted config,
CLI composition, documentation, and tests together. A config entry alone is not
an implementation. Never let retrieved content select or rewrite these files.

## Canonical and generated data

- `ontology/research-knowledge.yaml` is the starter LinkML ontology.
- `ontology/open-source-research-agents/` is the maintained working example. It
  contains a canonical seed `bundle.yaml`, explicit `build.yaml` and
  `library.yaml`, hash-pinned source cards, model-evaluation notes, a
  deterministic tainted-source index, generated candidate bundles, and an
  executable `demo.sh`.
- `intelligence/sources.yaml` is the maintained registry of upstream
  tainted-information intelligence sources and their scope/licensing caveats.
- `data/` is the default ignored runtime root. It normally contains
  `blobs/sha256/`, `records/`, SQLite files, checkpoints, JSONL operational
  logs, redacted prompt/reasoning logs, truth snapshots, and generated topic
  views.

Maintained ontology paths included by `config/truth-policy.yaml` are canonical
only as bytes in Git `HEAD`. Dirty or untracked candidate files must not be
treated as accepted seeds. If a source card changes, update its declared hash
and every exact evidence quote affected by the edit. Preserve stable concept
and project IDs across display-name or repository renames when they denote the
same entity.

Do not commit `.env`, runtime stores, model logs, SQLite projections, or private
source material. `.env` is intentionally ignored.

## How to make changes

### Add or change a record

1. Put the strict schema in the domain module that owns the concept.
2. Reject extra fields and validate IDs, paths, ranges, and cross-references.
3. Define canonical serialization and content-derived identity.
4. Write through `ImmutableStore`; do not mutate an existing record.
5. Add projection schema/build/query support if the record must be searchable.
6. Add the schema file to `config/truth-policy.yaml` if it is new.
7. Test validation failures, idempotence, persistence, and drift behavior.

### Add a connector

1. Define its capability and normalized records in `discovery.py` if needed.
2. Implement a narrow transport protocol and a deterministic normalizer under
   `connectors/`.
3. Pin hosts, reject redirects/private destinations as appropriate, redact
   credentials, bound response size/time, and avoid raw retention by default.
4. Declare its policy, storage rights, credential variable, request limit, and
   cost handling.
5. Wire it into the CLI or orchestrator only through trusted configuration.
6. Use fixtures and fake transports in tests; do not require live network calls.
7. Document whether results are transient leads, persistent metadata, or
   acquisition-eligible evidence.

### Change model extraction

1. Keep anchor selection outside the model.
2. Update the strict output schema and prompt together.
3. Bump the extraction-validator contract version when compatibility changes.
4. Validate exact quotes, source ranges, hierarchy, scope, and references.
5. Log only deterministically redacted prompts/reasoning; never raw secrets or
   private payloads.
6. Preserve proposal-only review state and zero commit authority.
7. Test valid, malformed, extra-field, truncated, hostile, and tool-attempt
   outputs.

### Change ontology orchestration

Preserve:

- explicit configuration and validation before live calls;
- one model request at a time by default;
- per-run time budgets and finalization reserve;
- atomic checkpoints and source work claims;
- reuse keyed to immutable source and validator compatibility;
- progress on stderr, result JSON on stdout, and structured JSONL logs;
- deterministic finalization after recoverable model failure; and
- non-zero, actionable token-exhaustion reporting.

Do not hide a slow or incomplete worker behind an unlimited timeout.

### Change retrieval or SQLite

Treat SQL schemas and queries as projections over a selected truth snapshot.
Use parameterized SQL. Return the compiled query/filter, snapshot identity,
limits, and truncation where applicable. Preserve exact source IDs, offsets,
provenance, and threat observations in result paths. Build to a temporary
database, validate, stamp, and atomically replace the target.

## Testing and verification

Use Python 3.12 and `uv`. Before committing:

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest -q
git diff --check
```

Useful focused checks:

```bash
uv run geas --help
uv run geas providers
uv run geas ontology-build \
  ontology/open-source-research-agents/build.yaml \
  --root /tmp/geas-check \
  --check
uv run geas projection-benchmark --tier smoke
```

Tests mirror modules under `tests/test_*.py`. Keep all normal tests offline and
deterministic using fixtures, fake clocks/transports/model clients, and
temporary roots. For security boundaries, test the rejection path as carefully
as the success path. Assert that forbidden writes or network/model calls did
not happen—not only that an error was raised.

The maintained ontology demo is an important integration contract:

```bash
demo_root=$(mktemp -d /tmp/geas-demo.XXXXXX)
./ontology/open-source-research-agents/demo.sh "$demo_root"
```

It exercises bundle import, deterministic threat handling, audit, truth
snapshot, projection, retrieval, export, and drift checking.

## Coding conventions and review checklist

- Prefer small typed domain components over logic embedded in CLI handlers.
- Use `Path.resolve()` plus explicit containment checks for filesystem
  boundaries; reject traversal and unsafe symlinks.
- Keep network I/O behind injectable transport protocols.
- Use explicit UTC timestamps and injectable clocks when time affects results.
- Sort unordered inputs before hashing, serializing, indexing, or returning
  them.
- Make repeat operations idempotent when content and configuration are
  unchanged.
- Preserve append-only history; create successor records instead of mutation.
- Keep stdout machine-readable JSON and operational progress on stderr.
- Never include secrets, source excerpts, raw model responses, or unredacted
  reasoning in operational logs or exceptions.
- Do not weaken a fail-closed path to make a test or unsupported host pass.
- Update operator docs when a CLI, configuration field, supported boundary, or
  workflow changes.
- Update hash-pinned ontology metadata when maintained source bytes change.

Before declaring a change complete, ask:

1. Does untrusted source or model text gain any new authority?
2. Can the result be traced to exact immutable evidence?
3. Does interruption leave a safe, resumable state?
4. Are budget, license, storage-rights, and data-routing gates still enforced?
5. Is canonical truth still distinguishable from proposals and projections?
6. Are dissent, gaps, and threat context preserved rather than flattened?
7. Are the success, rejection, idempotence, and drift paths tested?

## Current implementation boundaries

Do not document these as complete without implementing them:

- arbitrary repository-tree acquisition and bounded traversal of links found
  inside repositories;
- a Brave Search connector;
- scheduled deployment orchestration around bounded workers;
- record- or branch-level access control inside one deployment;
- embeddings as an implemented retrieval layer;
- generalized ontology candidate generation from every source-library format;
- production-wide process/network isolation beyond the implemented tool-free
  clients and parser/one-shot controls; and
- automated repository approval policy (forge rules remain out of scope).

Plans and research rationale live in `docs/NEXT_PHASE.md` and
`STATE_OF_THE_ART.md`. Keep implemented behavior, documented behavior, and
future intent clearly separated.
