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

## 3. User deposits and private or licensed material

**Status:** accepted on 2026-08-02.

### Decision

- Authorization is enforced at the site and deployment boundary.
- Per-record, per-claim, and per-ontology-branch access control is out of scope
  for the initial version.
- Operators control deposit defaults, and individual users may override them.
- The default may make all deposited information ungated within the authorized
  deployment without making it publicly accessible.
- Provenance, acquisition method, rights basis, redistribution, retention, and
  handling intent remain recorded.
- Handling labels are advisory metadata, not ACLs.
- Use local files and user-created exports initially; do not give agents browser
  credentials or authenticated sessions.

### Operational consequence

Anyone admitted to a deployment may be able to access all indexed content in
that deployment. Hard isolation requires a separate deployment or store root
until nuanced access control is deliberately added.

The detailed boundary and deposit workflow are documented in
`docs/DEPOSITS.md`.

## 4. Rights, authorship, and signed ownership evidence

**Status:** accepted on 2026-08-02.

### Decision

- Unknown is the default for redistribution, license, authorship, usage
  conditions, rights basis, source provenance, and each usage permission.
- Fill known authors, provenance, license, rights basis, and usage conditions
  when they are available instead of treating missing metadata as negative
  evidence.
- Track archive, quotation, transformation, and redistribution-of-original
  permissions independently.
- Preserve cryptographically verified Nostr events as evidence for ownership,
  authorship, or publication claims when the event is bound to the exact
  deposited content.

### Evidence boundary

A valid Nostr signature proves that the event was signed by the corresponding
key. A matching NIP-94 `x` or `ox` tag binds that signed event to a file hash.
Neither fact alone proves the signer's legal identity or ownership. The system
therefore records the signature as evidence supporting a declared relation,
not as a conclusive ownership determination.

## 5. External model authorization

**Status:** accepted on 2026-08-02.

### Decision

- Use deterministic preauthorization with escalation for OpenAI, z.ai, and
  future external model providers.
- Local DeepSeek use is automatic.
- Bind external authorization to an allowlisted provider, exact endpoint,
  exact model, operation, data class, input kind, content route, input hash,
  output limit, and policy version.
- Models and retrieved source material cannot authorize a call, change its
  classification, choose a destination, or waive a limit.
- Unknown data is local-only. Source content requires an `external_allowed`
  route in addition to provider and operation authorization.
- Preserve successful authorizations as immutable audit records.

See `docs/MODEL_USE_POLICY.md` for the enforcement contract.

## 6. Automatic external-use budget and account treatment

**Status:** accepted on 2026-08-02.

### Decision

- Use the conservative automatic envelope: US$0.25 per call, US$2 and 10 calls
  per run, US$5 per UTC day, and US$25 per UTC month.
- Limit each call to 32,000 reserved input tokens and 8,192 output tokens.
- Reserve worst-case cost transactionally before network access and reconcile
  provider-reported usage afterward.
- Allow operators to classify an account as metered, subscription-included,
  enterprise-commit, no-marginal-cost, or another documented basis.
- Allow non-metered accounts to be excluded from dollar totals without
  excluding them from call counts, token limits, authorization, or auditing.
- Treat unknown accounting as ineligible for automatic use.

The checked-in OpenAI and Z.ai API accounts remain metered and counted. See
`docs/BUDGET_POLICY.md`.

## 7. Human approval mechanism

**Status:** accepted on 2026-08-02.

### Decision

- Use authenticated deployment approval.
- Treat the local OS account as the authenticated identity for the initial
  CLI-first application.
- Support `--override-external-budget`, but translate it into a five-minute,
  single-use approval receipt rather than a bare boolean authorization.
- Bind the receipt to the exact request, reserved cost, run, and policy
  versions and record the approving OS identity.
- Permit future site authentication middleware to issue the same receipt type.

An approval may override automatic dollar or call-count limits. It cannot
override token ceilings, classification, source routing, provider/model/
endpoint allowlists, unknown accounting, expiry, or replay protection.

See `docs/APPROVALS.md`.

## 8. Repository license

**Status:** accepted on 2026-08-02.

### Decision

- License the repository software and original project material under
  Apache License 2.0.
- Use `Epiphytic` as the copyright holder and 2026 as the initial year.
- Preserve more specific existing notices, including the ontology's CC0
  declaration.
- Do not imply that the repository license applies to user deposits, acquired
  sources, provider content, or referenced third-party datasets.

The complete boundary is documented in `docs/LICENSING.md`.

## 9. Initial workload target

**Status:** accepted on 2026-08-02.

### Decision

- Target a local, single-user CLI for initial production use.
- Use one serialized canonical writer with bounded parallel research workers
  and query readers.
- Benchmark reproducible 10,000-, 100,000-, and 1,000,000-claim fixtures.
- Prioritize inspectability, deterministic rebuilds, crash recovery, and
  portability before raw query latency.
- Defer a production graph-backend migration until measurements or a changed
  deployment target justify it.

See `docs/WORKLOAD_TARGET.md`.

## 10. Local query backend after measurement

**Status:** resolved by measurement on 2026-08-02.

### Decision

- Retain SQLite/FTS5 as the disposable query projection for the accepted local
  single-user CLI target.
- Keep ontology, policy, immutable JSON records, content-addressed record
  batches, and source blobs canonical and directly inspectable.
- Reconsider a graph backend only after a workload change or measured
  regression, not because the data is graph-shaped.

The configured 10,000-, 100,000-, and 1,000,000-claim tiers all completed.
At one million claims, canonical writes took 31.84 seconds, snapshot creation
6.27 seconds, projection rebuild 62.36 seconds, and a deliberately global
all-match FTS query 1.07 seconds median with 241 MiB peak RSS. See
`docs/BENCHMARKS.md`.

## 11. Authenticated OpenAlex scholarly discovery

**Status:** implemented on 2026-08-03 after the operator supplied an API key.

### Decision

- Use OpenAlex after Crossref in the domain-index priority order.
- Load `OPENALEX_API_KEY` only from the ignored environment file.
- Persist normalized OpenAlex metadata under CC0, but retain no raw response
  body and do not infer rights for linked documents.
- Treat search calls as metered and counted even when OpenAlex's daily free
  credit covers them.
- Reserve US$0.001 transactionally before each call, settle from
  `meta.cost_usd`, and reject responses reporting more than the reservation.
- Enforce 10 requests per run and a US$1 provider-specific UTC-day ceiling.
- Keep normalized index results as discovery metadata, never claim evidence.

The connector uses a fixed HTTPS endpoint, refuses redirects and non-JSON
responses, bounds response size, validates OpenAlex work IDs and DOIs, and
redacts upstream error content. Multiword terms are quoted, Boolean structure is
generated deterministically, and source text cannot alter the query, endpoint,
credentials, budget, or persistence policy.

References:

- [OpenAlex authentication](https://developers.openalex.org/api-reference/authentication)
- [OpenAlex search syntax and pricing](https://developers.openalex.org/guides/searching)
- [OpenAlex works list API](https://developers.openalex.org/api-reference/works/list-works)
