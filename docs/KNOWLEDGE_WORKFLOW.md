# Persistent knowledge workflow

The implemented vertical slice separates four durable layers:

1. acquired source bytes and immutable audit records;
2. operator-reviewed knowledge packs containing concepts, evidence selectors,
   claims, controversies, and gaps;
3. a `TruthSnapshot` binding ontology, policy, schema, records, and blobs;
4. a disposable SQLite/FTS5 projection and generated Markdown views.

SQLite is never reconciled back into canonical records. A changed ontology or
new immutable record requires a successor snapshot and rebuild. A changed
SQLite row requires the database to be discarded and rebuilt.

## Research and import

Run deterministic local acquisition:

```bash
uv run research-agent research-local \
  "community water fluoridation caries cognition" \
  --corpus tests/fixtures/fluoridation_corpus \
  --term fluoridation \
  --root data
```

Run open scholarly discovery, which retains normalized DOI metadata but does
not treat it as evidence. The initial connector uses Crossref's anonymous
public pool; an operator-confirmed contact identity can enable the polite pool
later:

```bash
uv run research-agent discover-crossref \
  "community water fluoridation neurodevelopment" \
  --term "community water fluoridation" \
  --term neurodevelopment \
  --root data
```

OpenAlex adds authenticated, CC0 scholarly metadata including stable OpenAlex
and DOI identifiers, authorship, retraction status, citation/reference counts,
and open-access state. API usage is reserved and settled transactionally:

```bash
uv run research-agent --env-file .env discover-openalex \
  "community water fluoridation neurodevelopment" \
  --concept concept:community-water-fluoridation \
  --term "community water fluoridation" \
  --term "fluoride neurodevelopment IQ" \
  --root data
```

The API response body is hashed for audit but not retained. Normalized metadata
is discovery—not evidence—and linked full text retains its own license.

Mojeek remains a discovery-only fallback. Its transient hits are not persisted
until the operator confirms the account's storage terms:

```bash
uv run research-agent --env-file .env discover-mojeek \
  "community water fluoridation neurodevelopment" \
  --root data
```

A reviewed YAML knowledge pack binds exact selectors to already archived source
hashes. The importer rejects missing sources, selectors that are absent or
ambiguous, cyclic concept hierarchies, missing claim/evidence references, and
claim evidence from a source with a suspected deterministic threat:

```bash
uv run research-agent knowledge-import reviewed-topic.yaml \
  --root data \
  --imported-by operator:alice
```

Source text is scanned as inert bytes by fixed rules. Matches create suspected
threat observations and topic/source associations. No model sees the text in
order to make the security decision, and a suspected source cannot support a
claim through this import path.

## Snapshot and projection

```bash
uv run research-agent truth-snapshot \
  --root data \
  --created-by operator:alice

uv run research-agent projection-build \
  data/records/truth-snapshot/aa/<digest>.json \
  data/query.sqlite \
  --root data

uv run research-agent projection-check \
  data/records/truth-snapshot/aa/<digest>.json \
  data/query.sqlite \
  --root data
```

The builder verifies truth before and after its work, builds into a temporary
database, checks foreign keys, stamps the logical schema and row digest, and
atomically replaces the previous projection.

Large accepted record sets may be stored in content-addressed JSON batches.
Each batch remains directly inspectable and carries a sorted SHA-256 index of
its individual records. The reader verifies both the batch hash and every item
hash. This avoids an `fsync` per claim without shifting authority to SQLite.

## Deterministic retrieval

Natural-language lexical retrieval is compiled without a model into a
parameterized FTS5 expression. Tokens, selected record classes, SQL, parameters,
limit, truncation, and snapshot ID are returned with every result:

```bash
uv run research-agent knowledge-query \
  "lower IQ uncertainty" \
  --kind claim \
  --kind gap \
  --database data/query.sqlite
```

An exact topic view traverses descendants with a recursive CTE and joins claims
to exact evidence and source versions. It also returns topic-scoped sources,
controversies, ranked gaps, and tainted-source observations:

```bash
uv run research-agent topic-show \
  concept:community-water-fluoridation \
  --database data/query.sqlite
```

Use `--as-of 2025-01-01T00:00:00+00:00` for valid-time filtering. Generate an
agent-readable, deterministic Markdown page with:

```bash
uv run research-agent topic-export \
  concept:community-water-fluoridation \
  generated/fluoridation.md \
  --database data/query.sqlite
```

The generated page is a projection, not a report of record and not canonical.
It explicitly marks source/evidence content as untrusted data.
