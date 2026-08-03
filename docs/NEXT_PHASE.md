# Next phase: deterministic discovery and acquisition

## Objective

Build the first end-to-end research loop:

```text
question -> typed query plan -> deterministic discovery -> acquisition
         -> immutable source -> extraction proposal -> validation -> staged patch
```

The phase is complete when the system can research one bounded topic across
web indexes, scholarly indexes, structured APIs, archives, Git, and local files
without giving a model network credentials or canonical write access.

This phase deliberately separates **discovery** from **acquisition**. A search
hit says that an index reported a candidate. It is not evidence that the
candidate exists now, that the snippet is accurate, or that its contents support
a claim.

## Design constraints

- Natural-language input may be compiled into a typed plan by a model.
- Only deterministic code validates and executes a plan.
- Connectors are installed and configured by trusted operators, not selected
  or invented by retrieved content.
- A connector may discover or fetch, but cannot commit ontology changes.
- Search snippets, result rankings, redirects, and retrieved documents are
  untrusted data.
- Bot protections, access controls, paywalls, CAPTCHAs, and site terms are not
  bypassed.
- An inaccessible source remains an explicit coverage gap.
- Every network observation and acquired representation is time-versioned and
  attributable.
- Exact, graph, temporal, and lexical queries remain authoritative. Semantic
  retrieval may propose candidates but never establishes truth.

## Access strategy

Use the first permitted route that provides the required information:

1. Official API, bulk export, feed, sitemap, repository, or standards endpoint.
2. Domain index such as Crossref, OpenAlex, Europe PMC, or a regulator catalog.
3. General search API for candidate discovery.
4. Common Crawl index and WARC records for historical or otherwise unavailable
   public pages.
5. Lawfully accessible alternate representations, located through identifier
   resolution such as DOI plus Unpaywall.
6. A user-deposited file or browser export obtained under the user's access.
7. Metadata-only stub plus an `AccessConstraint` and knowledge-gap record.

The system must distinguish an intellectual work from its manifestations. A
publisher page, accepted manuscript, repository PDF, API record, and archived
snapshot can describe the same work while remaining separate source versions.

## New records

All records are immutable and receive content-derived identifiers.

### `QueryPlan`

- original trusted question;
- resolved ontology concepts;
- exact terms and controlled synonyms;
- source classes, languages, jurisdictions, and time bounds;
- required primary-source and independent-source coverage;
- controversy and dissent requirements;
- permitted connector capabilities;
- deterministic limits and stop conditions;
- plan compiler identity and version;
- human approval when scope or cost policy requires it.

### `DiscoveryRun`

- validated query-plan ID;
- connector and connector version;
- normalized executed query;
- start/end timestamps;
- index/API snapshot or version where available;
- pagination cursors and termination reason;
- result, duplicate, rejection, and error counts;
- rate-limit and truncation state.

### `DiscoveryHit`

- upstream identifier and canonical locator;
- title, authorship/publisher, dates, media type, and language;
- upstream rank and returned snippet;
- discovery-run ID;
- relationship to already known entities;
- acquisition eligibility;
- threat observations applying to the locator or connector.

### `AcquisitionAttempt`

- discovery-hit or explicit-locator ID;
- connector and resolved destination;
- redirect chain;
- HTTP or tool result;
- content length, media type, and hash when successful;
- robots, terms, authentication, licensing, and policy outcome;
- retry classification and next eligible attempt time.

### `AccessConstraint`

- target work, manifestation, or locator;
- reason such as paywall, authentication, robots policy, CAPTCHA, denial,
  unavailable API, licensing uncertainty, or missing archive;
- observed time and connector;
- possible lawful alternatives;
- whether human assistance could resolve it.

### `CoverageRun`

- topic branch and competency questions;
- discovery runs included;
- searched source classes and excluded source classes;
- temporal, geographic, and language scope;
- accessible, inaccessible, and metadata-only counts;
- known index limitations;
- unresolved gap IDs;
- freshness deadline.

## Connector contract

Connectors implement narrow capability interfaces instead of one unrestricted
search tool:

```python
class DiscoveryConnector(Protocol):
    manifest: ConnectorManifest

    def discover(self, request: DiscoveryRequest) -> Iterator[DiscoveryPage]: ...


class AcquisitionConnector(Protocol):
    manifest: ConnectorManifest

    def acquire(self, request: AcquisitionRequest) -> AcquisitionResult: ...
```

`ConnectorManifest` declares:

- stable connector ID and version;
- discovery, metadata, full-text, archive, or local-file capabilities;
- allowed schemes and destination hosts;
- credential environment-variable names, never credential values;
- query fields, filters, pagination, and deterministic limits;
- rate and concurrency limits;
- supported media types and maximum response size;
- license and terms notes;
- whether results can be redistributed or only referenced;
- parser and normalization versions;
- network trust zone.

The executor rejects fields not declared by the manifest. It also performs
redirect, DNS, scheme, host, address-range, response-size, decompression, and
media-type checks outside the model process.

## Initial connectors

Implement in this order:

1. **Local file** — completes the existing ingestion path and exercises the
   common interface without network risk.
2. **Git** — pinned repository, commit, tree, and blob acquisition.
3. **Crossref** — deterministic scholarly metadata and DOI discovery.
4. **OpenAlex** — broad scholarly discovery, citations, concepts, and alternate
   locations.
5. **Unpaywall** — lawful open-access location resolution from DOI.
6. **Europe PMC** — life-sciences metadata, references, and permitted full text.
7. **Common Crawl** — URL discovery followed by exact WARC-record acquisition.
8. **RSS/Atom and sitemap** — standing searches on explicitly configured sites.
9. **General search provider** — pluggable API used only for discovery.
10. **User deposit** — local PDF, HTML, email export, Zotero export, or browser
    save with user-supplied provenance.

Each connector ships with recorded response fixtures so its normalization,
pagination, and failure behavior can be tested without live network access.

## Query compilation and execution

The model-facing compiler receives the trusted question, ontology vocabulary,
and a list of available connector capabilities. It returns JSON conforming to
the `QueryPlan` schema. It receives no credentials and cannot execute the plan.

The validator:

- rejects unknown concepts, connectors, fields, and operators;
- clamps budgets to operator policy;
- normalizes time, locale, and identifier constraints;
- expands controlled synonyms from the ontology, not source text;
- calculates a stable plan hash;
- marks lossy clauses, including model-proposed concepts or semantic recall.

The deterministic planner then creates connector-specific requests. Every
translation is included in the audit record. Results are merged by exact
identifiers first, then by separately reviewable entity-resolution proposals.

## Local search

Add SQLite as the initial query store:

- ordinary tables for stable identifiers and exact filters;
- FTS5 indexes for labels, definitions, claims, and evidence;
- recursive common-table expressions for hierarchy traversal;
- explicit temporal and review-state predicates;
- saved query plans and snapshot IDs;
- provenance joins from claims to exact source fragments.

Natural-language questions compile to a typed local query plan. The compiled
SQL and parameters are inspectable. Semble may index the generated Markdown
projection for convenient discovery, but its results never replace canonical
graph or FTS queries.

## Acquisition and evidence states

Candidates advance through explicit states:

```text
discovered
  -> metadata_acquired
  -> content_acquired
  -> quarantined
  -> parsed
  -> evidence_addressable
  -> extraction_proposed
```

A discovery snippet remains `discovered`. Structured authoritative API data may
become addressable evidence if its schema, upstream identity, response bytes,
and license are archived. Claims based on document content require acquired
content and exact selectors.

## Prompt-injection and connector safeguards

- Discovery and acquisition execute in processes without ontology-write access.
- The model client receives no network, shell, filesystem, approval, or secret
  tools.
- Authenticated connectors keep cookies and API keys in the connector process.
- URL destinations come from validated requests and connector allowlists.
- Source text cannot change budgets, callbacks, destinations, policy, or
  connector configuration.
- Documents are parsed as inert bytes; scripts, macros, remote resources, and
  document actions are disabled.
- Extractor output is schema-validated and content-addressed.
- A deterministic policy decision is required before extraction and commit.
- Suspected source or connector poisoning creates an observation but cannot
  automatically establish a global source verdict.
- Independent index results remain distinct provenance observations so
  agreement is measurable rather than silently deduplicated.

## Milestones

### M1 — contracts and offline vertical slice

**Status: implemented.**

- Add the six records above and strict validation.
- Add connector manifests and discovery/acquisition protocols.
- Adapt local-file ingestion to the connector contract.
- Implement query-plan validation and audit serialization.
- Add recorded fixtures and invariant tests.

**Acceptance:** a fixture-backed question produces a reproducible query plan,
discovery run, acquired content hash, and coverage record without a model having
tools or write authority.

### M2 — scholarly and identifier discovery

- Implement Crossref and OpenAlex discovery.
- Add DOI, PMID, ORCID, ROR, ISSN, and URL normalization.
- Implement Unpaywall and Europe PMC resolution/acquisition.
- Represent works separately from manifestations.
- Add citation-following with depth and budget limits.

**Acceptance:** the same work found through several indexes resolves to one work
with separate attributed manifestations; inaccessible publisher content is
reported without blocking a lawful alternate copy.

### M3 — open-web and archived acquisition

- Add RSS/Atom, sitemap, and Common Crawl connectors.
- Add bounded HTTP acquisition for allowlisted sources.
- Enforce redirect, DNS, size, type, timeout, and decompression policies.
- Add access-constraint and retry scheduling.
- Add a pluggable general-search API interface.

**Acceptance:** a blocked live page can remain metadata-only or resolve to a
specific archived snapshot without bypassing access controls, and every route
attempt remains auditable.

### M4 — persistent deterministic query

- Add SQLite tables and migrations for sources, claims, evidence, concepts,
  controversy, gaps, and coverage.
- Add FTS5 lexical search and exact/faceted query.
- Add hierarchy traversal and temporal queries.
- Generate topic, controversy, provenance, and gap pages.
- Add optional Semble indexing over generated pages.

**Acceptance:** an independent agent answers the competency questions using only
typed read tools, exposes query mode and truncation, cites exact evidence, and
distinguishes unknown, inaccessible, contradicted, and false.

### M5 — gap-driven refresh

- Convert missing competency-question coverage into explicit gap records.
- Rank gaps by expected coverage gain, source diversity, age, and cost.
- Add standing query plans and freshness deadlines.
- Re-run only affected connectors and projections.
- Produce review queues for changed, disputed, or suspicious material.

**Acceptance:** the system can state which declared sources it searched, what
was inaccessible, which branches are stale, and which bounded search would most
improve coverage.

## Test strategy

- Unit tests for validators, URL policy, pagination, identifier normalization,
  and state transitions.
- Contract tests shared by all connectors.
- Recorded-response tests for deterministic offline replay.
- Property tests for hostile and malformed connector output.
- Golden tests for query compilation and source deduplication.
- Integration tests with ephemeral SQLite stores.
- Explicit tests that model text cannot select a destination, obtain a secret,
  approve a transition, or write canonical records.
- Live smoke tests kept separate from the deterministic suite.

## Operational outputs

Each research run should produce:

- a versioned query plan;
- discovery and acquisition audit events;
- archived source versions or explicit access constraints;
- a coverage statement;
- extraction and ontology patch proposals;
- threat observations;
- changed gap and freshness records;
- reproducible local indexes and topic projections.

Reports are optional projections of a selected snapshot. The reusable result is
the persistent, queryable knowledge state and its audit history.

## Decisions still requiring operator input

- Production graph backend after the SQLite vertical slice is measured.
