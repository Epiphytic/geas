# Operator decisions

This log records choices that affect cost, licensing, privacy, and system
boundaries. Each decision remains versioned with the code that enforces it.

## 1. General web discovery

**Status:** accepted on 2026-08-02.

### Decision

- Use Mojeek as the first general-web discovery provider.
- Keep Brave configured but disabled until a later operator decision.
- Prefer official APIs, domain indexes, open repositories, publisher-provided
  open access, feeds, and archives when acquiring evidence.
- Treat general-search results as candidate metadata, never evidence.
- Limit Mojeek to 50 requests per research run, 5,000 requests per month, and a
  US$25-equivalent monthly ceiling.
- Do not automatically exceed those limits.

The connector and plan validator enforce the per-run limit. Monthly request and
cost ceilings are currently declarative; scheduled searches must remain disabled
until a persistent usage ledger enforces them.

### Storage qualification

Mojeek permits persistent result storage on its Business plan. Other plans
permit only one-hour caching. The current account tier has not been confirmed,
so `config/research-policy.yaml` conservatively disables normalized-result and
raw-response persistence. Query plans, aggregate run metadata, response hashes,
and counts can still be retained without storing result content.

Once Business storage rights are verified, the operator may change
`storage_rights` to `confirmed`, enable `persist_normalized_results`, and set an
approved raw-response retention period.

### Implementation

- Credential: `MOJEEK_API_KEY`, loaded from the ignored `.env` file without
  shell evaluation.
- API destination: fixed to `https://api.mojeek.com/search`.
- API keys are never included in connector manifests, query plans, audit
  records, or raised transport errors.
- Result URLs are normalized and private, local, non-HTTP, or credential-bearing
  locators are rejected before becoming discovery hits.
- Mojeek discovery is available through `research-agent discover-mojeek`.

References:

- [Mojeek Search API](https://www.mojeek.com/services/search/web-search-api/)
- [Mojeek request parameters](https://www.mojeek.com/support/api/search/request_parameters.html)
- [Mojeek JSON response](https://www.mojeek.com/support/api/search/json_response.html)

## 2. Persistent query backend

**Status:** accepted on 2026-08-02.

### Decision

- Use SQLite as the deterministic query projection.
- Keep ontology definitions and controlled vocabulary in version-controlled,
  inspectable text.
- Keep knowledge history and evidence in immutable, content-addressed records
  and blobs.
- Bind canonical state with versioned `TruthSnapshot` records.
- Treat SQLite as disposable: it may be rebuilt from canonical state but may
  never update canonical state through reconciliation.
- Defer PostgreSQL, Apache AGE, Neo4j, or RDF-store deployment until measured
  concurrency, scale, availability, or traversal requirements justify it.

### Drift response

- Canonical changes require review, a successor truth snapshot, and projection
  rebuild.
- A stale or mutated SQLite projection is discarded and rebuilt.
- Direct SQLite edits never become ontology patches or accepted claims.
- Every projection records the truth snapshot, schema version, builder version,
  and logical database digest from which it was produced.

The complete contract and operational commands are documented in
`docs/SOURCE_OF_TRUTH.md`.
