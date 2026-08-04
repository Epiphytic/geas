# Source libraries

A source library is an ontology-independent, immutable selection of parsed
sources. It lets humans and agents search source text before, during, or without
ontology extraction. The manifest is inspectable YAML, immutable source and
derivation records remain authoritative, and the library's SQLite file is a
discardable query projection.

This is the primary entry point for
[searching sources without an ontology](USE_CASES.md#search-sources-without-building-an-ontology)
and for
[supplying bounded context to another agent](USE_CASES.md#supply-bounded-attributable-context-to-another-agent).

## Build and query a library

The maintained Geas research-agent ontology includes a library manifest that selects
all parsed sources in its runtime store:

```bash
uv run geas library-build \
  ontology/open-source-research-agents/library.yaml \
  --root data/open-source-research-agents \
  --database data/open-source-research-agents/library.sqlite
```

Search exact structural source anchors:

```bash
uv run geas library-show \
  --database data/open-source-research-agents/library.sqlite

uv run geas library-query \
  "citation retrieval and persistent knowledge" \
  --database data/open-source-research-agents/library.sqlite \
  --limit 25
```

Return a bounded agent context package:

```bash
uv run geas library-context \
  "citation retrieval and persistent knowledge" \
  --database data/open-source-research-agents/library.sqlite \
  --limit 25 \
  --max-characters 16000
```

The context response contains the compiled deterministic FTS expression,
library and snapshot IDs, exact source fragments, character offsets,
provenance, repository identity where known, threat-observation IDs, character
usage, and an explicit truncation flag. It does not ask a model to rank or
interpret sources.

## Manifest selectors

Selectors are an inclusive union:

```yaml
version: 1
id: library:network-engineering
title: Network engineering sources
description: Sources used by agents working on the selected repositories.
repositories:
  - Example/router
  - Example/network-controller
source_version_ids:
  - source:sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
source_uri_prefixes:
  - file:///srv/network-docs/
connector_ids:
  - connector:local-file
include_all_parsed_sources: false
```

Repository selectors currently select repositories that have already been
acquired into the immutable store. Explicit repository-tree acquisition and
bounded link-frontier traversal are separate discovery/acquisition work; a
manifest fails closed if a named repository or source version is absent.

## Ontology views over a library

An ontology configuration can select an immutable library snapshot and disable
new discovery:

```yaml
source_library_snapshot_id: source-library-snapshot:sha256:...
discovery_enabled: false
topic: Network engineering for Example repositories
scope_criteria:
  - architecture or operation of the selected network systems
  - protocols, configuration, troubleshooting, or security used by those systems
ontology_facets:
  - components and interfaces
  - protocols and data flows
  - configuration and operational invariants
  - failure modes and troubleshooting
  - security boundaries
competency_questions:
  - Which configuration controls BGP route selection?
  - Which component owns each network interface?
```

The current ontology candidate writer supports repository snapshots. Other
library source types are queryable now, but require a generalized candidate
bundle writer before they can independently produce maintained ontology
bundles.

## Bounded resumable workers

`ontology-build` is a resumable worker. `max_run_seconds` defaults to 1,800
seconds and is explicitly configurable per ontology. A clean time-budget
checkpoint exits successfully, reports remaining work, and can be resumed with
the same command. Completed immutable discovery, source parsing, proposals, and
candidates are retained.

Validator-compatible proposals for the same immutable source are reused even if
the next worker selects another provider, model, token ceiling, or reasoning
effort. Use `--reextract` only when already completed sources should be
reconsidered.

Before extraction, a worker acquires an expiring source claim under the runtime
root. Another worker skips the claimed source instead of duplicating its model
request. Claims are ownership-token bound and stale after the worker budget plus
a short cleanup allowance. State checkpoints are written under an exclusive
file lock and merge independent completed-query, proposal, candidate, and
source-status sets. Canonical ontology writes still go through separate Git
patch, PR, or MR promotion; repository merge policy remains out of scope.
