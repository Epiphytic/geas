# Persistent research knowledge system

An early implementation of an agent-maintained, ontology-backed research
knowledge base. Its durable product is a versioned graph of claims, evidence,
concepts, disagreements, and source-threat observations—not a periodically
regenerated report.

This repository provides the deterministic control plane, offline acquisition,
open scholarly discovery, and a persistent SQLite knowledge-query vertical
slice:

- strict records for sources, evidence, claims, and threat observations;
- strict records for query plans, discovery, acquisition, access constraints,
  and coverage;
- an immutable, content-addressed local store;
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
- format-neutral original preservation with versioned text derivations for
  text, JSON, HTML, XML, OpenDocument/OpenXML office files, and PDF;
- immutable document/page/section/heading/block anchors with exact offsets,
  hierarchy, and deterministic full-text projection;
- a tool-free client for local DeepSeek and optional external providers;
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
cause deterministic quarantine or denial according to
[`config/source-policy.yaml`](config/source-policy.yaml). See
[SECURITY.md](SECURITY.md) for implemented guarantees and remaining deployment
work.

## Local model and optional frontier providers

The default provider is the local OpenAI-compatible DeepSeek service discovered
on this host:

```text
http://127.0.0.1:8000/v1
model: deepseek-v4-flash
```

[`config/providers.toml`](config/providers.toml) also defines opt-in OpenAI and
Z.AI providers. External calls require `OPENAI_API_KEY` or `ZAI_API_KEY`; no key
is needed for the local endpoint. Provider credentials are read only from
environment variables and must never be committed.

## Quick start

Python 3.12 and `uv` are recommended:

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run research-agent providers
uv run research-agent model-smoke
uv run research-agent projection-benchmark --tier smoke
```

Create an immutable store and archive a source:

```bash
uv run research-agent store-init --root data
uv run research-agent source-add README.md --root data
```

Run the offline research slice over one or more operator-selected directories:

```bash
uv run research-agent research-local \
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
uv run research-agent policy-check \
  --workflow-id workflow:example \
  --source-version source:sha256:example \
  --stage extraction
```

Global options such as `--policy` and `--providers` go before the subcommand.
The `data/` directory is intentionally ignored by Git.

Run bounded Mojeek discovery using `MOJEEK_API_KEY` from the ignored `.env`
file:

```bash
uv run research-agent discover-mojeek \
  "ontology-backed research agents" \
  --result-limit 10
```

This command is discovery-only. It retains the query plan, aggregate run record,
and response hashes. Normalized hits are not persisted until the operator
confirms that the Mojeek subscription has storage rights. Search hits and
snippets are never evidence.

Run authenticated OpenAlex discovery using `OPENALEX_API_KEY` from the same
ignored file:

```bash
uv run research-agent --env-file .env discover-openalex \
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
uv run research-agent discover-europe-pmc \
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
contact from the ignored `.env` file:

```bash
uv run research-agent resolve-unpaywall \
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
uv run research-agent parse-document paper.pdf \
  --license CC-BY-4.0 \
  --root data
```

Acquire a previously resolved, deterministically licensed manifestation:

```bash
uv run research-agent acquire-open-access \
  10.1289/ehp.1104912 \
  --root data
```

Original bytes remain immutable. Parsed text is a separate quarantined,
content-addressed derivation with parser provenance and deterministic threat
scanning. Stable structural anchors are generated automatically and can be
searched with `knowledge-query --kind anchor`. See
[docs/PARSING.md](docs/PARSING.md) and
[docs/STRUCTURAL_DERIVATIONS.md](docs/STRUCTURAL_DERIVATIONS.md).

Capture canonical state and detect later ontology, record, blob, or SQLite
projection drift:

```bash
uv run research-agent truth-snapshot \
  --root data \
  --created-by operator:example

uv run research-agent projection-check \
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
uv run research-agent deposit-add paper.pdf \
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
docs/          intelligence-source research
intelligence/  machine-readable upstream source registry
ontology/      starter persistent-knowledge schema
src/           models, planning, connectors, store, policy, workflow, providers, CLI
tests/         security-invariant and behavior tests
```
