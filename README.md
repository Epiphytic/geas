# Persistent research knowledge system

An early implementation of an agent-maintained, ontology-backed research
knowledge base. Its durable product is a versioned graph of claims, evidence,
concepts, disagreements, and source-threat observations—not a periodically
regenerated report.

This repository currently provides the Phase 0 control plane and the M1 offline
research slice:

- strict records for sources, evidence, claims, and threat observations;
- strict records for query plans, discovery, acquisition, access constraints,
  and coverage;
- an immutable, content-addressed local store;
- a deterministic source-policy engine loaded from validated YAML;
- fixed workflow transitions that models cannot authorize;
- connector capability manifests and narrow discovery/acquisition contracts;
- deterministic query validation with controlled synonyms and budget clamps;
- path-confined local-file discovery and acquisition;
- a tool-free client for local DeepSeek and optional external providers;
- a starter LinkML ontology and a maintained upstream intelligence registry;
- tests for the principal prompt-injection security invariants.

Network connectors, graph persistence, lexical/faceted graph query, ontology
projection, gap ranking, and scheduled refresh are subsequent milestones. See
[STATE_OF_THE_ART.md](STATE_OF_THE_ART.md) for the design and research basis,
and [docs/NEXT_PHASE.md](docs/NEXT_PHASE.md) for the executable discovery and
acquisition plan.

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

## Repository map

```text
config/        trusted provider, vocabulary, and deterministic policy configuration
docs/          intelligence-source research
intelligence/  machine-readable upstream source registry
ontology/      starter persistent-knowledge schema
src/           models, planning, connectors, store, policy, workflow, providers, CLI
tests/         security-invariant and behavior tests
```
