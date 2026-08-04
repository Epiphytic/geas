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

## High-reasoning one-shot providers

`codex_oneshot` and `claude_oneshot` let an ontology select a bounded coding
agent for assembly while discovery, source selection, validation, and
persistence stay deterministic. Configure the provider and reasoning effort in
the ontology's `build.yaml`.

The subprocess starts in an empty temporary directory and receives only the
trusted extraction instruction plus already-selected untrusted anchors on
stdin. Codex runs ephemeral with user configuration and repository rules
ignored, read-only sandboxing, web search disabled, a deny-all `PreToolUse`
hook, strict output schema, and typed JSONL event auditing. Claude Code runs in
safe mode with no session persistence, an empty strict MCP configuration, and
an empty tool list. A typed command, file-change, MCP, tool-call, or web-search
Codex event fails closed.

Coding-agent output has proposal authority only. The normal envelope,
allowed-concept, exact-excerpt, anchor-containment, evidence-hash, and semantic
validators still run before a proposal enters the immutable store. The
subprocess cannot promote or write ontology truth.

Document, page, and section containers are rejected. At most 200 leaf anchors
and 200,000 source characters enter one call. Source text is JSON-labelled as
`untrusted_source_anchors`; no tools are offered to the model.

## Deterministic validation

The prompt includes the generated JSON Schema, explicit required top-level
keys, literal-enum instructions, and an output contract scaled to the configured
token ceiling. It tells the model to prioritize supported items and close the
object before the ceiling rather than starting an item it cannot finish.
Every supported generation control is configurable by ontology: thinking,
reasoning effort, temperature, top-p, top-k, min-p, seed, stop strings, output
ceiling, and timeout. The general ontology default is 64K output tokens; each
ontology can request a different ceiling when its provider/model supports it.
The maintained open-source research-agent ontology uses thinking mode with a
128K output ceiling. Output capacity and context capacity are represented
separately. DwarfStar max-thinking requires a context of at least 384 Ki
(393,216 tokens); preflight rejects max when the provider does not declare that
capacity, preventing the server's otherwise silent fallback to high. These
ceilings are operational, not caps on concept, claim, controversy, gap, source,
or query counts.

Model output must be one complete strict JSON object containing proposed
concepts, claims, controversies, and gaps. The provider adapter checks
`finish_reason` before parsing: `length` becomes an explicit truncation failure.
It never salvages a nested object from a truncated outer envelope. Code outside
the model:

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

Token exhaustion makes the autonomous build explicitly incomplete and produces
an actionable non-zero CLI result. The receipt identifies the source,
requested ceiling, provider capacity, observed output tokens, and recommends
raising the ceiling, changing provider/model, or splitting grounded extraction
into smaller batches.

No model command converts this record into an accepted `Claim`. The separate
Git-native promotion path renders a lossless review manifest and accepts it only
after deterministic verification from its declared canonical branch. Forge and
repository approval rules do not run inside the ontology engine. See
[`PROMOTIONS.md`](PROMOTIONS.md).

Model-call and output-validation failures retain the sanitized request and a
failure record containing stage, exception class, safe schema locations,
finish reason, and provider-reported output-token count. Invalid raw model
output and exception text are not retained.

Every request also creates a JSONL and immutable prompt-audit record. The system
prompt and trusted structure are retained, but untrusted source excerpts are
replaced by SHA-256/character-count markers. Deterministic patterns redact
emails, IP addresses, phone-like strings, bearer tokens, secret assignments,
Nostr secret keys, private keys, and secret-bearing environment values. This
does not claim perfect de-identification; deployments handling private or
sensitive prompts should keep these logs inside the same authorization
boundary and apply retention policy.

When `debug_reasoning` is enabled (the default), provider reasoning is written
separately to `model-reasoning-debug.jsonl` and an immutable
`model-reasoning-debug` record. Both prompt and reasoning JSONL files are mode
0600. Exact selected source excerpts are replaced by hash/length markers before
the same deterministic PII and secret redactor runs. Raw reasoning is not
retained, although its SHA-256 digest is kept for correlation. This is a debug
facility, not a promise that free-form reasoning has been perfectly
de-identified; keep it within the source data's authorization boundary or
disable it with `debug_reasoning: false` / `--no-debug-reasoning`.

Local or external model authorization remains governed by the existing
deterministic model gate.

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
  --reasoning-effort high \
  --temperature 0 \
  --seed 0 \
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

When tuning reasoning effort, run high and max against the same derivation,
anchor set, question, sampling parameters, and output ceiling. Then compare
the two proposal IDs without another model call:

```bash
uv run research-agent compare-extractions \
  extraction-proposal:sha256:HIGH... \
  extraction-proposal:sha256:MAX... \
  --root data
```

The comparison reports separate grounded-claim, predicate, concept,
hierarchy, controversy, gap, evidence, and uniqueness measurements. It
recommends the candidate only when at least two independent dimensions improve
without material redundancy regression; it does not turn verbosity into one
opaque quality score.

For a reproducible local DeepSeek high/max trial, the repository also includes
`scripts/evaluate_reasoning_modes.sh`. It selects leaf anchors through the CLI,
runs the two tool-free requests serially, keeps their full receipts out of
terminal logs, and writes the deterministic comparison to a caller-selected
JSON path. It can be placed under a service supervisor for long runs without
changing ontology state or granting either proposal commit authority.

Inspect one small concept subtree without loading a whole proposal:

```bash
uv run research-agent proposal-slice \
  extraction-proposal:sha256:... \
  concept:model-provider-support \
  --root data
```

The deterministic slice follows `broader` edges, then returns only concepts in
that subtree, claims whose subject is in the subtree, and controversies/gaps
linked to those claims.

External providers require the accepted data classification, content route,
budget reservation, and approval rules. Unknown data remains local-only.
