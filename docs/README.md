# Geas documentation

Choose the shortest path that matches the work:

- [Common use cases](USE_CASES.md) explains which Geas workflow to use and what
  is implemented today.
- [Build and use Geas end to end](GETTING_STARTED.md) covers installation,
  API/LLM setup, repository material, ontologies, agent retrieval, and
  project-specific expert exports.
- [Ontology quick start](QUICKSTART_ONTOLOGY.md) is the executable path from a
  new configuration to a resumable ontology build.
- [User configuration, shared ontologies, and Git sync](USER_CONFIG.md) covers
  OS-standard locations, team profiles, modular secrets, private Git, and
  Obsidian-style export.
- [Source libraries](SOURCE_LIBRARIES.md) covers ontology-independent source
  collections, exact search, and bounded agent context.
- [Knowledge workflow](KNOWLEDGE_WORKFLOW.md) covers acquisition, import,
  snapshot, projection, retrieval, and maintenance.
- [Maintained ontologies](MAINTAINED_ONTOLOGIES.md) defines reviewable,
  hash-pinned bundles.
- [Promotions](PROMOTIONS.md) describes patch, PR, and MR promotion without
  granting a model canonical write authority.

## Trust, provenance, and operations

- [Security](../SECURITY.md): deterministic prompt-injection boundary and
  remaining deployment work.
- [Source of truth](SOURCE_OF_TRUTH.md): canonical authority, SQLite
  projections, and drift handling.
- [Parsing](PARSING.md) and
  [structural derivations](STRUCTURAL_DERIVATIONS.md): original preservation,
  inert text extraction, and stable anchors.
- [Citation graph](CITATION_GRAPH.md): identifiers and reference traversal.
- [Licensing](LICENSING.md) and [deposits](DEPOSITS.md): rights metadata,
  defaults, and user-provided material.
- [Tainted-source intelligence](THREAT_INTELLIGENCE_SOURCES.md): upstream
  registries and their limitations.
- [Approvals](APPROVALS.md), [budget policy](BUDGET_POLICY.md), and
  [operator decisions](OPERATOR_DECISIONS.md): explicit operator controls.

## Models and scale

- [Model extraction](MODEL_EXTRACTION.md): proposal-only extraction, grounding,
  one-shot providers, and reasoning logs.
- [Model-use policy](MODEL_USE_POLICY.md): deterministic provider and data
  routing.
- [Benchmarks](BENCHMARKS.md) and [workload target](WORKLOAD_TARGET.md):
  measured projection behavior and intended deployment scale.
- [Next phase](NEXT_PHASE.md): implementation work that is not yet part of the
  supported boundary.

All examples assume execution from the repository root. The project and its CLI
are both named Geas; invoke the CLI as `geas`.
