# Geas

Geas builds and maintains inspectable research knowledge: immutable source
libraries, evidence-linked ontologies, explicit disagreements and gaps, and
deterministic search indexes. Its durable product is reusable knowledge, not a
one-time report.

The project is developed at
[`Epiphytic/geas`](https://github.com/Epiphytic/geas). Its CLI is `geas`.

## Common use cases

| You want to… | Use… | Result |
|---|---|---|
| Research a topic into durable, reusable knowledge | `ontology-init`, then `ontology-build` | A resumable source library, reviewable ontology proposals, a deterministic SQLite projection, and an agent-readable topic view |
| Give an agent precise context without conventional RAG | `library-query`, `library-context`, or `knowledge-query` | Bounded exact text fragments or evidence-linked claims, with source identities and truncation made explicit |
| Search a corpus before deciding on an ontology | `parse-document` and `library-build` | An ontology-independent, immutable, searchable source library |
| Keep an existing topic current | rerun `ontology-build`, optionally with `--refresh` | New source versions and proposals without discarding completed discovery, acquisition, or extraction work |
| Preserve competing conclusions | maintained controversies plus `topic-show` | Dissent stored as first-class positions linked to claims and evidence |
| Track hostile or unreliable inputs | deterministic scanning, source policy, and `knowledge-audit` | Version-specific threat observations and a maintained tainted-source index |
| Research selected local repositories or documents | `research-local`, `source-add`, or `deposit-add` | Content-addressed sources with provenance, parsed structure, and exact anchors |
| Discover scholarly material and acquire eligible open access copies | Crossref, OpenAlex, Europe PMC, and Unpaywall commands | Normalized discovery metadata, license-aware resolution, and immutable parsed documents |
| Review and promote model-generated knowledge through Git | `promotion-stage`, `promotion-verify`, and `promotion-apply` | A patch/PR/MR workflow in which models propose but cannot publish canonical truth |
| Detect ontology/database drift | `truth-snapshot`, `truth-check`, and `projection-check` | A reproducible authority boundary between Git/immutable records and disposable SQLite indexes |

Start with the [use-case guide](docs/USE_CASES.md) to choose a workflow, or use
the [end-to-end getting-started guide](docs/GETTING_STARTED.md) for building,
provider setup, repository ingestion, agent use, and expert exports. You can
also print a path-aware walkthrough with
`uv run geas setup-guide --format markdown`. Use
the [executable ontology quick start](docs/QUICKSTART_ONTOLOGY.md) to build a
topic immediately. The [documentation index](docs/README.md) maps operational,
security, model, provenance, and architecture guides.

### What Geas is not

Geas is not a chat UI, a report generator, or an embedding database. Reports
can be projected from a snapshot, but canonical knowledge remains a versioned
graph of concepts, claims, exact evidence, provenance, disagreements, gaps, and
source-threat observations. Natural-language retrieval is compiled into
inspectable full-text, graph, temporal, provenance, and contradiction queries;
embeddings are not authoritative.

### Current boundary

The strongest end-to-end path today is a local, single-user CLI that discovers
web and scholarly sources, resolves supported GitHub repositories, preserves
local and open-access documents, builds resumable ontology proposals, and
projects accepted knowledge into SQLite. Arbitrary repository-tree acquisition
and bounded traversal of links found inside repositories are not yet a complete
first-class workflow; known repositories already acquired into the store can be
selected by a source-library manifest. See
[source libraries](docs/SOURCE_LIBRARIES.md#manifest-selectors).

## Capabilities

This repository provides the deterministic control plane, offline acquisition,
open scholarly discovery, and a persistent SQLite knowledge-query vertical
slice:

- strict records for sources, evidence, claims, and threat observations;
- strict records for query plans, discovery, acquisition, access constraints,
  and coverage;
- an immutable, content-addressed local store;
- ontology-independent source-library manifests, snapshots, deterministic
  search, and bounded exact agent-context packages;
- a deterministic source-policy engine loaded from validated YAML;
- fixed workflow transitions that models cannot authorize;
- connector capability manifests and narrow discovery/acquisition contracts;
- deterministic query validation with controlled synonyms and budget clamps;
- path-confined local-file discovery and acquisition;
- Crossref and authenticated OpenAlex scholarly discovery with normalized DOI,
  PMID, PMCID, ORCID, ROR, and ISSN identities plus authorship, publication,
  open-access, citation, and cost metadata;
- reviewed knowledge-pack import with exact source selectors;
- deterministic indirect-prompt-injection scanning and topic-scoped tainted-source records;
- content-addressed, inspectable JSON record batches for larger claim sets;
- atomic SQLite projection builds with FTS5, hierarchy, provenance, dissent,
  gap, threat, and valid-time queries;
- deterministic JSON and Markdown topic views;
- deterministic cross-linked Obsidian-style Markdown vault projections;
- OS-standard per-user ontology profiles with modular secrets and optional
  private Git synchronization;
- format-neutral original preservation with versioned text derivations for
  text, JSON, HTML, XML, OpenDocument/OpenXML office files, and PDF;
- immutable document/page/section/heading/block anchors with exact offsets,
  hierarchy, and deterministic full-text projection;
- deterministic DOI, PMID, PMCID, arXiv, and public-URL references linked to
  exact anchors and exact discovery/open-access metadata matches;
- SHA-256-pinned maintained ontology bundles with source authorship, license,
  usage, rights, and provenance metadata;
- deterministic maintenance audits for tainted evidence, dissent quality,
  freshness deadlines, missing evidence, and retraction signals;
- proposal-only local DeepSeek extraction grounded in operator-selected exact
  anchors, with deterministic schema, scope, hierarchy, and quote validation;
- an executable maintained ontology of open-source research agents;
- resumable ontology workers capped at 30 minutes per invocation, with
  source-level work claims and cross-model proposal reuse;
- tool-free clients for local DeepSeek, external APIs, and bounded Codex or
  Claude Code one-shot ontology assembly;
- a starter LinkML ontology and a maintained upstream intelligence registry;
- tests for the principal prompt-injection security invariants.

Additional scholarly/open-web connectors, automated extraction proposals, and
scheduled gap refresh remain subsequent milestones. See
[STATE_OF_THE_ART.md](STATE_OF_THE_ART.md) for the design and research basis,
and [docs/NEXT_PHASE.md](docs/NEXT_PHASE.md) for the executable discovery and
acquisition plan. Accepted cost, licensing, and deployment choices are recorded
in [docs/OPERATOR_DECISIONS.md](docs/OPERATOR_DECISIONS.md). Canonical authority
and projection reconciliation are defined in
[docs/SOURCE_OF_TRUTH.md](docs/SOURCE_OF_TRUTH.md). User-deposit defaults and
the deployment-level authorization boundary are documented in
[docs/DEPOSITS.md](docs/DEPOSITS.md).

## Why this is not conventional RAG

Embeddings may eventually be used as a recall aid, but they are not the source
of truth. Retrieval should compile natural-language questions into inspectable
graph, full-text, temporal, provenance, and contradiction constraints. Every
answer must be reconstructable from versioned claims and exact evidence
fragments.

## Security boundary

Retrieved text and model output are data, never instructions. Models can
propose typed artifacts but receive no shell, network, secret, approval, or
database-write capabilities. Deterministic code validates records, evaluates
policy, advances the workflow, and commits immutable patches.

The reference workflow is:

```text
queued -> fetched -> quarantined -> extracted -> validated
       -> staged -> approved -> committed
```

Models cannot transition it. Confirmed or suspected hostile-source observations
cause deterministic quarantine or denial according to the managed
`source-policy.yaml` under the user configuration root. Its packaged template
is [`config/source-policy.yaml`](config/source-policy.yaml). See
[SECURITY.md](SECURITY.md) for implemented guarantees and remaining deployment
work.

## Local model and optional frontier providers

The default provider is the local OpenAI-compatible DeepSeek service discovered
on this host:

```text
http://127.0.0.1:8000/v1
model: deepseek-v4-flash
```

The managed `providers.toml` under the user configuration root defines model
routes. Its packaged template, [`config/providers.toml`](config/providers.toml),
includes opt-in OpenAI, Z.AI, Codex CLI, and Claude Code providers. API
providers require their named environment key. CLI one-shots reuse their own
authenticated subscription and are accounted as subscription-included calls.

Use `codex_oneshot` or `claude_oneshot` when ontology assembly needs a stronger
reasoning tier than the local extraction model. They receive only selected
source anchors and return proposal JSON; discovery and ontology writes remain
outside the coding agent.

## Quick start

Python 3.12 and `uv` are recommended:

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run geas providers
uv run geas setup-guide --format markdown
uv run geas model-smoke
uv run geas projection-benchmark --tier smoke
```

Create a configured ontology and validate it without network or model calls:

```bash
uv run geas config-init
uv run geas ontology-init \
  --topic "Example topic" \
  --concept-id concept:example

uv run geas ontology-build example \
  --root data/example \
  --check
```

This writes the ontology configuration in the OS-standard per-user Geas
directory. Generic ontology worker defaults come from `ontology_defaults` in
the OS-standard Geas `config.yaml`; an ontology inherits absent eligible fields
and overrides present ones. Edit its explicit `build.yaml` and `library.yaml`,
then rerun the second command without `--check` to build or resume. Explicit
workspace paths remain supported. See
[user configuration and Git sync](docs/USER_CONFIG.md)
and
[the ontology quick start](docs/QUICKSTART_ONTOLOGY.md) for credentials,
checkpointing, candidate review, promotion, and final projection.

Named ontology use checks the configured Git repository for updates by default,
but records a successful check for one hour so repeated commands do not contact
the remote. Publish changed, rebuildable databases and generated content as
verified private release assets, then hydrate them lazily on another machine:

```bash
uv run geas ontology-artifact-publish example \
  --source-library data/example/library.sqlite \
  --knowledge-projection data/example/query.sqlite \
  --generated-content data/example/obsidian \
  --published-by operator:example \
  --storage-rights-basis "authorized private storage"

uv run geas ontology-artifact-sync example
```

Only artifact roles whose embedded input revision changed are uploaded. See
[portable ontology artifacts](docs/PORTABLE_ONTOLOGY_ARTIFACTS.md) for global
and per-ontology freshness settings, lazy paths, rights checks, and the
non-canonical cache boundary.

Ontology acceptance defaults to `auto`: Git-backed profile ontologies use
Git-mediated acceptance on `refs/heads/main`, while non-Git ontologies remain
proposal-only. No HITL is hardcoded; a human or automation actor may merge the
proposal, but only the exact canonical-ref bytes—not the model—define
acceptance. See [Git-native promotion](docs/PROMOTIONS.md).

Create an immutable store and archive a source:

```bash
uv run geas store-init --root data
uv run geas source-add README.md --root data
```

Run the offline research slice over one or more operator-selected directories:

```bash
uv run geas research-local \
  "How should prompt injection be handled?" \
  --corpus docs \
  --concept concept:prompt-injection \
  --compiler-provider deepseek_local \
  --root data
```

The command emits the validated query plan, exact connector query, discovery
hits, acquisition attempts, source hashes, coverage gaps, and immutable record
hashes. Repeated `--term` options replace the conservative question-token
compiler. `--compiler-provider deepseek_local` uses the tool-free local
DeepSeek client to propose a plan; the same deterministic validator still
controls connector selection and execution. Result budgets above the configured
approval threshold require `--approve-budget`.

Evaluate deterministic policy against zero or more threat-observation JSON
records:

```bash
uv run geas policy-check \
  --workflow-id workflow:example \
  --source-version source:sha256:example \
  --stage extraction
```

Global options such as `--policy` and `--providers` go before the subcommand.
The `data/` directory is intentionally ignored by Git.

Run bounded Mojeek discovery using `MOJEEK_API_KEY` from the selected profile's
managed secret source:

```bash
uv run geas discover-mojeek \
  "ontology-backed research agents" \
  --result-limit 10
```

This command is discovery-only. It retains the query plan, aggregate run record,
and response hashes. Normalized hits are not persisted until the operator
confirms that the Mojeek subscription has storage rights. Search hits and
snippets are never evidence.

Run authenticated OpenAlex discovery using `OPENALEX_API_KEY` from the same
managed secret source:

```bash
uv run geas --env-file .env discover-openalex \
  "community water fluoridation neurodevelopment" \
  --concept concept:community-water-fluoridation \
  --term "community water fluoridation" \
  --term "fluoride neurodevelopment IQ" \
  --result-limit 20
```

OpenAlex metadata is persisted under CC0; linked content is not downloaded.
Every API request is transactionally reserved before network access and settled
from the response's reported cost. The checked-in policy caps OpenAlex at 10
requests per run and its US$1 daily API allowance. Raw responses and credentials
are never placed in the knowledge store.

Run Europe PMC lite bibliographic discovery without credentials:

```bash
uv run geas discover-europe-pmc \
  "community water fluoridation neurodevelopment" \
  --concept concept:community-water-fluoridation \
  --term "community water fluoridation" \
  --term "fluoride neurodevelopment IQ"
```

The connector persists normalized bibliographic metadata with an `unknown`
license label. It deterministically requests `resultType=lite`; abstracts, full
text, and raw responses are excluded and require a separate license-aware
acquisition path.

Resolve known DOIs to license-attributed OA manifestations with the project
contact from the managed secret source:

```bash
uv run geas resolve-unpaywall \
  10.1002/14651858.CD010856.pub3 \
  --root data
```

The contact identity is transport-only. Locations, versions, host types, and
reported licenses become immutable resolution records and searchable SQLite
projection rows. Only explicit CC0/public-domain, CC-BY, and CC-BY-SA license
families are automatically acquisition-eligible; `other-oa`, unknown,
noncommercial, and no-derivatives terms require operator review.

Preserve and parse an operator-selected document:

```bash
uv run geas parse-document paper.pdf \
  --license CC-BY-4.0 \
  --root data
```

Acquire a previously resolved, deterministically licensed manifestation:

```bash
uv run geas acquire-open-access \
  10.1289/ehp.1104912 \
  --root data
```

Original bytes remain immutable. Parsed text is a separate quarantined,
content-addressed derivation with parser provenance and deterministic threat
scanning. Stable structural anchors are generated automatically and can be
searched with `knowledge-query --kind anchor`. Citation identifiers and
relations are also generated automatically. Traverse one identifier exactly:

```bash
uv run geas identifier-show \
  doi 10.18653/v1/2024.naacl-long.347 \
  --database data/query.sqlite
```

Build and exercise the maintained open-source research-agent ontology:

```bash
demo_root=$(mktemp -d /tmp/geas-demo.XXXXXX)
./ontology/open-source-research-agents/demo.sh "$demo_root"
```

See [docs/CITATION_GRAPH.md](docs/CITATION_GRAPH.md),
[docs/MAINTAINED_ONTOLOGIES.md](docs/MAINTAINED_ONTOLOGIES.md),
[docs/MODEL_EXTRACTION.md](docs/MODEL_EXTRACTION.md),
[docs/PROMOTIONS.md](docs/PROMOTIONS.md),
[docs/PARSING.md](docs/PARSING.md) and
[docs/STRUCTURAL_DERIVATIONS.md](docs/STRUCTURAL_DERIVATIONS.md).

Capture canonical state and detect later ontology, record, blob, or SQLite
projection drift:

```bash
uv run geas truth-snapshot \
  --root data \
  --created-by operator:example

uv run geas projection-check \
  data/records/truth-snapshot/aa/snapshot.json \
  data/query.sqlite \
  --root data
```

SQLite is a rebuildable query projection. It is never a source for automatic
changes to canonical ontology or knowledge records.

The complete import, snapshot, query, and topic-export workflow is documented
in [docs/KNOWLEDGE_WORKFLOW.md](docs/KNOWLEDGE_WORKFLOW.md). Measured local
performance is recorded in [docs/BENCHMARKS.md](docs/BENCHMARKS.md).

The initial production target is a local single-user CLI with one serialized
canonical writer and million-claim scale testing. A different graph backend
requires measured evidence against [the workload contract](docs/WORKLOAD_TARGET.md).

Archive a user-provided source with explicit provenance:

```bash
uv run geas deposit-add paper.pdf \
  --deposited-by user:researcher \
  --method browser_save \
  --original-locator https://publisher.example/paper \
  --author "Ada Example" \
  --license CC-BY-4.0 \
  --usage-condition "Attribution required"
```

Deposit defaults are operator-configurable and individually overridable. The
initial version assumes the entire deployment is authorization-gated; it does
not enforce record- or branch-level ACLs. Rights fields default to unknown, and
valid NIP-01/NIP-94 events may be attached as file-bound cryptographic evidence.

External model use is separately controlled by a deterministic gate that binds
the provider, endpoint, model, operation, data class, content route, and exact
input hash. Automatic calls require a transactional budget reservation.
Subscription and enterprise-accounted services may be excluded from dollar
totals without bypassing call or token limits. See
[docs/MODEL_USE_POLICY.md](docs/MODEL_USE_POLICY.md) and
[docs/BUDGET_POLICY.md](docs/BUDGET_POLICY.md).

For CLI use, `--override-external-budget` creates a single-use approval bound
to the exact request and attributed to the local OS account. It cannot override
classification, routing, provider, accounting, or hard token safeguards. See
[docs/APPROVALS.md](docs/APPROVALS.md).

## Tainted-source intelligence

[`intelligence/sources.yaml`](intelligence/sources.yaml) catalogs maintained
feeds and repositories for influence-operation behaviors, claim reviews,
phishing, malware, domain abuse, and misleading marketing enforcement. Imported
items are attributed, time-scoped observations—not global truth labels.

The registry includes DISARM, DISINFOX, Data Commons ClaimReview, MISP,
URLhaus, PhishTank, Spamhaus DBL, FDA and FTC resources, and several
supplementary sources. Licensing, access, scope, staleness, and false-positive
caveats are recorded per source. The research and selection rationale are in
[docs/THREAT_INTELLIGENCE_SOURCES.md](docs/THREAT_INTELLIGENCE_SOURCES.md).

## License

Repository software and original project material are licensed under
[Apache License 2.0](LICENSE). Explicitly licensed ontology material,
third-party sources, user deposits, and acquired content retain their own
terms. See [docs/LICENSING.md](docs/LICENSING.md).

## Repository map

```text
config/        trusted provider, vocabulary, and deterministic policy configuration
docs/          operator guides, architecture, policy, and research notes
intelligence/  machine-readable upstream source registry
ontology/      maintained ontologies, source cards, and candidate bundles
src/           models, planning, connectors, store, policy, workflow, providers, CLI
tests/         security-invariant and behavior tests
```
