# Geas ontology-building quick start

This is the end-to-end workflow for the
[durable topic-knowledge use case](USE_CASES.md#build-durable-knowledge-about-a-topic).
For source-only search or integration with another agent, start with
[source libraries](SOURCE_LIBRARIES.md) instead.

The autonomous builder needs Python 3.12+, `uv`, Git, a running configured
model endpoint, and a Mojeek key. From the repository root, create the ignored
`.env` file:

```dotenv
MOJEEK_API_KEY=replace-with-your-key
```

The project and its installed CLI are both named Geas; invoke the CLI as
`geas`.

Create a new ontology with complete, inspectable configuration files:

```bash
uv run geas ontology-init ontology/network-engineering \
  --topic "Network engineering for selected repositories" \
  --concept-id concept:network-engineering
```

This writes `build.yaml` and `library.yaml`. Every application default,
including null and empty settings, model parameters, discovery limits, worker
timing, retry behavior, extraction selection, output paths, and library
selectors is written explicitly. The files can be edited before the first run;
the CLI does not silently rely on omitted values. Existing files fail closed
unless `--force` is supplied.

Validate the complete configuration without making network or model calls:

```bash
uv run geas --env-file .env ontology-build \
  ontology/open-source-research-agents/build.yaml \
  --root data/open-source-research-agents \
  --check
```

Then build or resume the ontology:

```bash
uv run geas --env-file .env ontology-build \
  ontology/open-source-research-agents/build.yaml \
  --root data/open-source-research-agents
```

Each invocation is a bounded worker. `max_run_seconds` defaults to 1,800 seconds
and is explicitly configurable in each ontology; when useful work remains, a
clean worker checkpoint exits successfully and the same command resumes from
immutable records. A source work claim prevents two workers sharing a runtime
root from issuing the same extraction request.
Validated proposals remain reusable when a later worker changes model,
provider, output ceiling, or reasoning effort. Pass `--reextract` only to
deliberately reconsider completed sources.

For high-reasoning assembly over a source library, an ontology can say:

```yaml
provider: codex_oneshot
max_output_tokens: 131072
model_parameters:
  thinking: true
  reasoning_effort: xhigh
```

`claude_oneshot` is the equivalent Claude Code route. The corresponding CLI
must be installed and authenticated. These are tool-isolated proposal
generators; the research agent still owns retrieval, grounding validation,
checkpointing, and persistence.

To deliberately refresh completed searches and re-resolve already known
repositories at their current official commits:

```bash
uv run geas --env-file .env ontology-build \
  ontology/open-source-research-agents/build.yaml \
  --root data/open-source-research-agents \
  --refresh
```

Without the flag, `refresh_after_hours` controls scheduled refresh. Each known
repository is refreshed at most once per run, and only its latest immutable
snapshot is selected for extraction.

That one command imports the seed ontology, turns its open gaps into search
queries, searches Mojeek, resolves supported GitHub results through the
official API at immutable commits, parses and threat-scans source text,
selects anchors deterministically, and sends one tool-free extraction request
at a time to the configured model. It validates every proposed quote against
the source range before writing a repository-namespaced candidate bundle. It
then runs the deterministic audit, captures canonical truth, rebuilds SQLite,
and exports an agent-readable topic view from the bundles already accepted in
Git `HEAD`. Generated candidates are not imported into accepted knowledge
before their patch, PR, or MR is approved through the repository workflow.

The command checkpoints at
`data/open-source-research-agents/ontology-build-state.json`. Re-running the
same command resumes completed work. Discovery has a separate deterministic
configuration fingerprint, so changing only model parameters, output capacity,
timeouts, or extraction batching reuses acquired immutable sources while
rerunning extraction under the new settings. A changed query, seed, or
discovery limit requires a new runtime root. Proposals are reused only when
their provider, model name, output ceiling, model parameters, reasoning-log
setting, and extraction-validator contract version match. Prompt or output
schema changes must bump that validator version, which deterministically
invalidates stale proposals without repeating discovery. A model failure stops further model
requests for that run—important for non-streaming local servers that may still
be finishing a timed-out request—but deterministic finalization still runs.
Human-readable stage progress and progress bars are written to stderr. A
machine-readable event stream is appended to
`data/open-source-research-agents/ontology-build.log.jsonl`; it records query
and source identifiers, counts, durations, model request limits, and failure
types, but never credentials, source excerpts, or model responses. Redacted
model prompts are stored separately in `model-prompts.jsonl`: untrusted source
text is replaced by its hash and character count, common PII and secret
patterns are removed, and the corresponding immutable record stores raw-prompt
hashes for audit correlation. With `debug_reasoning: true`, redacted provider
reasoning is stored separately in mode-0600
`model-reasoning-debug.jsonl`; raw reasoning is not retained.

Review these outputs:

- `ontology/open-source-research-agents/generated/*/bundle.yaml` contains
  reviewable ontology proposals and exact evidence ranges. Merging them through
  the repository's normal patch, PR, or MR workflow makes them canonical.
- `ontology/open-source-research-agents/tainted-sources.yaml` is the
  deterministic poisoned-source index. It contains immutable source identity,
  threat classifications, detector identity, and evidence-fragment IDs, but
  never copies the hostile payload or attempted instruction. Entries are
  version-specific and remain in the index when a later repository revision is
  clean.
- `data/open-source-research-agents/topic.md` is the complete agent-readable
  projection.
- `data/open-source-research-agents/query.sqlite` is disposable and searchable.
- `data/open-source-research-agents/truth-snapshot.json` defines the exact
  canonical state used to build that projection.
- `data/open-source-research-agents/ontology-build.log.jsonl` is the structured
  operational log.
- `data/open-source-research-agents/model-prompts.jsonl` contains deterministically
  redacted model prompts.
- `data/open-source-research-agents/model-reasoning-debug.jsonl` contains
  deterministically redacted reasoning for model debugging.

In this repository, ontology files are canonical only when they exist in the
checked-out Git `HEAD`.
Fresh files under `generated/` are therefore review candidates: they are
excluded from truth snapshots and are not imported into accepted records until
the repository promotion workflow tracks/approves them. The configured
`seed_bundle_globs` resolve files from `HEAD` only. Before import, the builder
also requires the bundle and every referenced source file to match their exact
Git blob byte-for-byte; dirty tracked files fail closed. A post-merge rerun
therefore imports approved generated bundles while ignoring uncommitted
candidates. SQLite is always a discardable projection and never writes back
into the ontology.
Consequently, promotion is an explicit two-pass workflow: run the builder to
produce candidates, review and commit/merge those candidates, then run the same
command again to import the now-canonical bundles and rebuild the final
projection. The second pass reuses compatible extraction proposals and does not
repeat those model calls.

Search the completed ontology deterministically:

```bash
uv run geas knowledge-query \
  "local model support and retrieval architecture" \
  --database data/open-source-research-agents/query.sqlite \
  --limit 25
```

To refine another topic, copy `build.yaml`, change the topic, concept ID,
queries, seed bundles, and output directory, then use a fresh `--root`.
Defaults deliberately keep model concurrency at one and output capacity at
64K tokens. The maintained open-source-research-agents ontology explicitly
uses 128K and approves discovery above the conservative 50-result threshold.
Its 200-result/five-page Mojeek boundary comes from the connector manifest, not
an ontology item cap. Repository acquisition is processed in bounded,
resumable batches because each candidate requires several official GitHub API
requests; additional queries and later gap-filling passes can continue
expanding the ontology without a semantic source ceiling.
Token ceilings are configured per ontology; the builder refuses a
provider that cannot satisfy the requested ceiling. If a model exhausts the
ceiling, the CLI marks the build incomplete, exits non-zero, and tells the
operator to raise the limit, change models/providers, or split source
extraction without reducing ontology-wide coverage. External providers can be
selected when their API key and deterministic model/budget policies authorize
the source data.

All generation parameters are ontology-local:

```yaml
max_output_tokens: 131072
model_parameters:
  thinking: true
  reasoning_effort: high  # use max only with >=393216 context tokens
  temperature: 0
  top_p: null
  top_k: null
  min_p: null
  seed: 0
  stop: []
debug_reasoning: true
timeout_seconds: 14400
max_run_seconds: 1800          # hard per-worker ceiling
minimum_model_window_seconds: 300
finalization_reserve_seconds: 120
work_claim_grace_seconds: 60
connection_attempts: 10        # retries connection refusal only
connection_retry_seconds: 2
```

The provider separately declares `context_window_tokens`. For DwarfStar,
`reasoning_effort: max` requires 384 Ki (393,216 tokens), not decimal 384,000;
configuration validation fails rather than allowing its silent downgrade to
high.
