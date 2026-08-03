# Deterministic citation and identifier graph

Every successfully parsed document produces a citation derivation after its
text and structural derivations. Source bytes remain canonical; citation
records are immutable derivations tied to an exact text SHA-256 and Unicode
code-point ranges.

## Records

- `ResearchIdentifier` is a normalized DOI, PMID, PMCID, arXiv identifier, or
  public HTTP(S) URL with a canonical locator.
- `BibliographicReference` is one exact identifier occurrence linked to its
  smallest structural anchor, source version, selector range, and selector hash.
- `CitationDerivation` binds one structural derivation to the complete ordered
  identifier and reference indexes produced by a versioned extractor.

The extractor rejects credential-bearing URLs, local hostnames, and non-global
literal IP addresses. It does no network access and interprets no source
instruction.

## Relation semantics

Relations are conservative textual classifications:

- `mentions` is the default;
- `cites` requires citation syntax or a reference-section ancestor;
- `updates`, `corrects`, `retracts`, `reviews`, and `replies_to` require an
  explicit nearby lexical signal.

These records describe what source text says. In particular, a `retracts` edge
is not authoritative proof that the target work is retracted. The deterministic
knowledge audit raises it for resolution against an authoritative source.

## Exact resolution

The SQLite projection joins identifiers to discovery and open-access records
only through an exact normalized entity ID, exact canonical locator, or exact
normalized DOI. No fuzzy title or author matching silently creates graph edges.
Future fuzzy entity resolution must remain a separate, reviewable proposal.

Use natural-language lexical search for discovery:

```bash
uv run research-agent knowledge-query \
  "retraction DOI example" \
  --kind reference \
  --database data/query.sqlite
```

Use exact traversal for complete inbound references and deterministic metadata
matches:

```bash
uv run research-agent identifier-show \
  doi 10.18653/v1/2024.naacl-long.347 \
  --database data/query.sqlite
```

Re-derive an already stored structural document idempotently:

```bash
uv run research-agent derive-citations \
  structural-derivation:sha256:... \
  --root data
```

Projection schema 7 introduced identifier, reference, and exact-resolution
tables; schema 8 retains them and adds extraction-proposal review search.
SQLite remains disposable; none of these joins can reconcile changes back into
canonical state.
