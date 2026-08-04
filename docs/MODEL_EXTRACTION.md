# Anchor-grounded model extraction proposals

Models may propose ontology additions, but they never receive commit authority.
`propose-extraction` is the initial proposal-only path for local DeepSeek and
deterministically authorized external providers.

## Trusted inputs

The operator supplies:

- one stored structural-derivation ID;
- one or more exact leaf-anchor IDs from that derivation;
- a trusted research question;
- the existing concept IDs that claims may use;
- provider, data-class, route, token, approval, and budget inputs.

Document, page, and section containers are rejected. At most 200 leaf anchors
and 200,000 source characters enter one call. Source text is JSON-labelled as
`untrusted_source_anchors`; no tools are offered to the model.

## Deterministic validation

Model output must be one strict JSON object containing proposed concepts,
claims, controversies, and gaps. Code outside the model:

- rejects extra fields such as tool names or destinations;
- limits string, list, anchor, and token sizes;
- restricts claim subjects to operator-allowed or internally proposed concepts;
- rejects unknown broader concepts and cyclic proposed hierarchies;
- validates predicates and unique proposal keys;
- requires every evidence anchor to be in the trusted selection;
- requires every exact quote to occur exactly once inside that anchor;
- calculates global Unicode ranges and SHA-256 selector hashes;
- validates controversy and gap references;
- sets `asserted_by` from the configured provider and model;
- forces `review_state: proposed` and
  `commit_authority: none_proposal_only`.

No model command converts this record into an accepted `Claim`. The separate
Git-native promotion path renders a lossless review manifest and accepts it only
after deterministic verification from its declared canonical branch. Forge and
repository approval rules do not run inside the ontology engine. See
[`PROMOTIONS.md`](PROMOTIONS.md).

Model-call and output-validation failures retain the sanitized request and a
failure record containing only stage and exception class. Invalid raw model
output and exception text are not retained. Local or external model
authorization remains governed by the existing deterministic model gate.

## CLI

First search or inspect structural anchors, then select exact leaf IDs:

```bash
uv run research-agent propose-extraction \
  structural-derivation:sha256:... \
  --anchor structural-anchor:sha256:... \
  --anchor structural-anchor:sha256:... \
  --question "Which claims and knowledge gaps are explicitly supported?" \
  --concept concept:open-source-research-agents \
  --provider deepseek_local \
  --root data
```

Validated proposals are searchable but remain visibly quarantined from
accepted knowledge:

```bash
uv run research-agent knowledge-query \
  "persistent ontology proposal" \
  --kind proposal \
  --database data/query.sqlite
```

External providers require the accepted data classification, content route,
budget reservation, and approval rules. Unknown data remains local-only.
