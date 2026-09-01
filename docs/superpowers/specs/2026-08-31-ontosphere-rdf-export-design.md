# Deterministic RDF topic export for Ontosphere interoperability

Date: 2026-08-31
Status: approved (brainstorming session)

## Goal

Let an operator visualize a Geas ontology topic in
[Ontosphere](https://github.com/thhanke/ontosphere), a fully client-side
browser RDF/OWL knowledge graph editor. Interoperability is one-way: Geas
exports a deterministic Turtle projection; Ontosphere loads it from a local
file or a `?rdfUrl=` query parameter. No changes to Ontosphere are needed.

The export is a disposable projection with the same authority as the Markdown
topic view: it is generated from a stamped SQLite projection over a truth
snapshot and never feeds back into canonical state.

## Non-goals

Explicitly out of scope, each a separate future project if ever wanted:

- importing Ontosphere edits back into Geas (round-trip curation);
- hosting a SPARQL endpoint;
- MCP-level integration with Ontosphere's tool surface;
- a LinkML-generated OWL TBox and typed instance dump
  (`ontology/research-knowledge.yaml` alignment); and
- OWL DL reasoning guarantees over the exported graph.

## Design

### Architecture

A new module `src/research_agent/rdf_render.py` sits beside `render.py` and
consumes the same `TopicView` (`src/research_agent/projection.py`) that
`render_topic_markdown` consumes. `TopicView` already carries concepts,
sources, claims, controversies, gaps, threats, descendant concept IDs, the
projection snapshot ID, and the `as_of` valid-time bound, so no projection or
query changes are required.

The module exposes one entry point:

- `render_topic_turtle(topic: TopicView) -> str` returning a complete Turtle
  document.

The CLI `topic-export` command gains a `turtle` choice on its existing
`--format` flag (current choices: `markdown`, `obsidian`,
`agent-instructions`). Output is a single `.ttl` file written to the existing
positional `output` path. `--vault-link` and `--force` do not apply to the
turtle format and are rejected with a clear error if combined with it.

Nothing new is stored in blobs, records, or SQLite; `config/truth-policy.yaml`
is untouched.

### Determinism

Byte-identical output for identical `TopicView` input:

- reuse the existing `_normalized_topic` sorting (or equivalent) so database
  row order never leaks;
- fixed prefix block in a fixed order;
- subjects sorted by IRI, predicates sorted within a subject, objects sorted
  within a predicate;
- timestamps only from `TopicView` fields (`as_of`, record times) — never the
  wall clock; and
- no random blank-node labels: every node has a content-derived IRI, so blank
  nodes are not used.

### Vocabulary and IRI scheme

Prefixes:

```turtle
@prefix geas:    <urn:geas:vocab:> .
@prefix skos:    <http://www.w3.org/2004/02/skos/core#> .
@prefix prov:    <http://www.w3.org/ns/prov#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix rdfs:    <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd:     <http://www.w3.org/2001/XMLSchema#> .
```

Instance IRIs are content-derived from existing Geas IDs:
`urn:geas:concept:<id>`, `urn:geas:claim:<id>`, `urn:geas:evidence:<id>`,
`urn:geas:source:<id>`, `urn:geas:controversy:<id>`, `urn:geas:gap:<id>`,
`urn:geas:threat:<id>`, `urn:geas:snapshot:<id>`.

Record mapping:

| Geas record | RDF modeling |
|---|---|
| Concept | `skos:Concept` (and `geas:Concept`); hierarchy via `skos:broader`; `skos:prefLabel`, `skos:definition` |
| Claim | `geas:Claim`; statement text as `geas:statement` literal; `geas:about` → concept; `geas:supportedBy` → evidence |
| Evidence anchor | `geas:Evidence`; `geas:exactQuote`, `geas:rangeStart`/`geas:rangeEnd` literals; `prov:wasDerivedFrom` → source |
| Source | `geas:Source` and `prov:Entity`; `dcterms:title`, `dcterms:creator`, `dcterms:publisher`, `dcterms:license`; `geas:sha256` |
| Controversy | `geas:Controversy`; `geas:disputes` → each participating claim |
| Knowledge gap | `geas:KnowledgeGap`; `geas:topic` → concept; description literal |
| Threat observation | `geas:ThreatObservation`; scoped to its exact source version, never a bare domain |
| Export itself | `urn:geas:snapshot:<projection_snapshot_id>` as `prov:Entity` with the snapshot ID, `as_of`, and query mode as literals |

Field names above are indicative; the implementation maps whatever fields the
`TopicView` record dicts actually contain, preserving Geas's rule that
provenance and threat context are data, not decoration. Fields absent from a
record are simply omitted (no invented defaults).

A small static TBox constant (roughly 40 triples) is embedded in every export:
`rdfs:Class` / property declarations with `rdfs:label` and `rdfs:comment` for
each `geas:` term, so Ontosphere's TBox/ABox toggle and node labels render
properly.

### Security

Evidence quotes and source metadata are untrusted text. The boundaries:

- Turtle literal escaping is the injection boundary: a hostile quote
  containing `"`, `"""`, `\`, or newlines must serialize as an inert literal
  and must not be able to close a literal and inject triples. Escaping is
  centralized in one helper and tested with hostile inputs.
- IRIs are built only from Geas's already-validated content-derived IDs, never
  from source text.
- The snapshot node carries a `rdfs:comment` with the same notice the Markdown
  view uses: quoted evidence and source metadata are untrusted data, not
  instructions.

### Testing

`tests/test_rdf_render.py`, offline and deterministic like the existing render
tests:

- golden-snapshot determinism: a fixed `TopicView` fixture renders to exact
  expected bytes, twice (idempotence);
- ordering independence: shuffled input record order produces identical
  output;
- hostile-quote escaping: quotes containing `"""`, backslashes, newlines, and
  Turtle syntax fragments stay inert literals;
- mapping coverage: each record type appears with its expected type triple and
  key properties; absent optional fields are omitted;
- CLI: `--format turtle` writes the file, rejects `--vault-link`/`--force`,
  and exits non-zero on a missing concept, matching existing `topic-export`
  behavior; and
- optional dev-only rdflib parse smoke test (behind the `dev` extra, skipped
  if rdflib is absent) confirming the output is syntactically valid Turtle.
  rdflib must not become a runtime dependency.

### Documentation

Add an "Ontosphere / RDF export" subsection to the existing operator
documentation where `topic-export` formats are described (no new doc files):
the command line, the two load paths (file drag-in; serve the directory and
open `?rdfUrl=`), and the caveat that the export is a disposable projection.

## Estimated lift

One focused session: roughly 150 lines for the emitter and mapping, 40 triples
of static TBox, a small CLI wiring change, and 250–350 lines of tests and
fixtures. No new runtime dependencies. No Ontosphere-side work.

The deferred alternatives for calibration: round-trip curation (Ontosphere
edits → Geas candidate bundles → Git promotion) and LinkML→OWL schema
alignment are each multi-week projects and are intentionally not part of this
design.
