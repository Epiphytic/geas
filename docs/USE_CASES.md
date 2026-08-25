# Common Geas use cases

Geas separates source discovery, source-library maintenance, ontology
construction, and retrieval. These layers can be used together or
independently. This guide identifies the supported entry point, durable output,
and important boundary for each common job.

All commands run from the repository root after:

```bash
uv sync --extra dev
```

The project and its CLI are both named Geas; invoke the CLI as `geas`.

## Build durable knowledge about a topic

Use this when the goal is a maintained understanding rather than a final
report.

```bash
uv run geas config-init
uv run geas ontology-init \
  --topic "Example topic" \
  --concept-id concept:example

uv run geas ontology-build example \
  --root data/example \
  --check

uv run geas ontology-build example \
  --root data/example
```

`ontology-init` writes every default explicitly. Edit discovery queries, scope,
facets, competency questions, model settings, budgets, and time limits before
the first live run. Each build invocation checkpoints its work and can resume
with another configured model or reasoning effort.

User-created ontologies default to the selected OS-standard Geas profile; see
[user configuration](USER_CONFIG.md) for team profiles, modular secrets, Git
sync, and Obsidian export. Explicit paths remain available for maintained
repository ontologies.

The durable outputs are immutable source records, a source library, validated
model proposals, reviewable ontology bundle candidates, and structured JSONL
logs. Candidate knowledge becomes canonical only through the repository's Git
review policy. SQLite and Markdown views are rebuildable projections.

Follow [the complete ontology quick start](QUICKSTART_ONTOLOGY.md) for
configuration and the two-pass promotion workflow.

## Search sources without building an ontology

Use a source library when exact source text is useful before, during, or
without semantic extraction:

```bash
uv run geas library-build \
  ontology/open-source-research-agents/library.yaml \
  --root data/open-source-research-agents \
  --database data/open-source-research-agents/library.sqlite

uv run geas library-query \
  "citation retrieval and persistent knowledge" \
  --database data/open-source-research-agents/library.sqlite \
  --limit 25
```

The manifest is inspectable YAML, immutable source and derivation records are
authoritative, and SQLite is disposable. Retrieval uses deterministic FTS5,
not model ranking. See [source libraries](SOURCE_LIBRARIES.md).

## Supply bounded, attributable context to another agent

`library-context` returns exact source fragments with offsets, provenance,
source identity, threat-observation IDs, a character budget, and an explicit
truncation flag:

```bash
uv run geas library-context \
  "citation retrieval and persistent knowledge" \
  --database data/open-source-research-agents/library.sqlite \
  --limit 25 \
  --max-characters 16000
```

For accepted semantic knowledge, use `knowledge-query` for lexical retrieval or
`topic-show` for a complete concept subtree. An integrating agent can therefore
request small source fragments for primary inspection, small claim sets for a
specific question, or a complete bounded topic view without consuming the
whole ontology.

## Maintain and refresh knowledge

Rerun the same `ontology-build` command to resume incomplete work. Add
`--refresh` to deliberately repeat completed searches and resolve known
repositories at their current official commits:

```bash
uv run geas --env-file .env ontology-build \
  ontology/example/build.yaml \
  --root data/example \
  --refresh
```

New source versions append to the immutable store. Compatible discovery,
acquisition, parsing, and validated extraction work is reused. Run the
model-free audit to expose stale gaps, missing evidence, weak dissent, tainted
evidence, and retraction signals:

```bash
uv run geas knowledge-audit \
  --root data/example \
  --as-of 2026-08-04T00:00:00+00:00 \
  --fail-on-error
```

Scheduling repeated workers is a deployment concern; the CLI provides the
bounded, resumable unit of work.

## Preserve dissent, uncertainty, and knowledge gaps

Geas stores controversies and gaps rather than flattening them into a single
answer. A controversy contains distinct positions linked to accepted claims;
claims link to exact evidence and provenance. Gaps carry status, priority, and
freshness information.

```bash
uv run geas topic-show \
  concept:example \
  --database data/example/query.sqlite
```

Use `--as-of` to inspect valid-time state. A topic result also includes
topic-scoped source threats, so an integrating agent can distinguish a disputed
claim from a potentially hostile input.

## Research local repositories and document collections

For local, operator-selected paths:

```bash
uv run geas research-local \
  "Which components own network configuration?" \
  --corpus ../router-repository \
  --corpus ../controller-repository \
  --concept concept:network-engineering \
  --root data/network-engineering
```

Individual files can be preserved with `source-add`, `deposit-add`, or
`parse-document`. Supported parsing preserves original bytes and creates
separate inert text derivations and stable structural anchors.

Repository selectors in a source-library manifest can select repositories
already acquired into the immutable store. Automatic arbitrary repository-tree
acquisition and bounded traversal of links contained in repositories are not
yet a complete first-class workflow. This distinction is documented in
[manifest selectors](SOURCE_LIBRARIES.md#manifest-selectors).

## Discover scholarly and open-web sources

Use connector-specific commands when source discovery is the job:

- `discover-crossref` for public DOI and bibliographic metadata;
- `discover-openalex` for authenticated CC0 scholarly metadata;
- `discover-europe-pmc` for credential-free lite life-sciences metadata;
- `discover-mojeek` for open-web discovery; and
- `resolve-unpaywall` plus `acquire-open-access` for license-aware open-access
  resolution and preservation.

Discovery hits and snippets are leads, not evidence. A claim can rely only on
acquired, parsed content with exact evidence selectors. Connector storage,
licensing, cost, and credential boundaries are described in the
[knowledge workflow](KNOWLEDGE_WORKFLOW.md).

## Use stronger reasoning only for ontology proposals

Set an ontology's provider to `codex_oneshot` or `claude_oneshot` when local
extraction is insufficient:

```yaml
provider: codex_oneshot
max_output_tokens: 131072
model_parameters:
  thinking: true
  reasoning_effort: xhigh
```

The coding-agent process is tool-isolated and receives selected source anchors,
not credentials or canonical write access. Its output must pass the same
deterministic schema, scope, hierarchy, and evidence validation as a local
model. See [model extraction](MODEL_EXTRACTION.md).

## Review and promote knowledge with Git

Model output is a proposal. Use the promotion workflow to bind a proposal to an
exact repository tree and render a forge-neutral manifest:

```bash
uv run geas promotion-stage \
  extraction-proposal:sha256:... \
  --topic "Example topic" \
  --topic-concept-id concept:example \
  --root data/example \
  --repository .
```

The manifest can travel as a Radicle patch, GitHub PR, or GitLab MR. Repository
rules decide whether it is accepted. After merge, `promotion-verify` and
`promotion-apply` require the exact canonical Git content before materializing
it. See [promotions](PROMOTIONS.md).

## Verify authority and detect drift

Canonical ontology files are the files accepted in Git `HEAD`; immutable
records and blobs are content-addressed; SQLite is a disposable query
projection.

```bash
uv run geas truth-snapshot \
  --root data/example \
  --created-by operator:local

uv run geas projection-check \
  data/example/records/truth-snapshot/aa/snapshot.json \
  data/example/query.sqlite \
  --root data/example
```

Use the actual snapshot path emitted by `truth-snapshot`. If projection content
drifts, discard and rebuild it. Geas never reconciles database changes back
into canonical knowledge. See [source of truth](SOURCE_OF_TRUTH.md).

## Track tainted sources without making global blocklists

Deterministic scanners treat retrieved text as inert data and create
version-specific threat observations. Maintained ontology builds also write a
reviewable `tainted-sources.yaml` index. Observations record provenance,
detector identity, classification, scope, and time; they do not turn a domain
into timeless global truth.

`policy-check` shows whether known observations allow a fixed workflow stage,
and `knowledge-audit` detects accepted claims that depend on actively tainted
evidence. See [security](../SECURITY.md) and
[tainted-source intelligence](THREAT_INTELLIGENCE_SOURCES.md).
