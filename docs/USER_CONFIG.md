# User configuration, shared ontologies, and Git sync

Geas stores ordinary user-created ontology configuration outside the source
checkout by default. This lets every Geas process for the same OS user resolve
an ontology by name. The maintained `ontology/open-source-research-agents/`
example remains an explicit repository-local exception.

Initialize the user configuration:

```bash
uv run geas config-init
```

The default locations are:

| OS | Configuration root |
|---|---|
| Linux and other Unix systems | `$XDG_CONFIG_HOME/geas`, or `~/.config/geas` |
| macOS | `~/Library/Application Support/geas` |
| Windows | `%APPDATA%\geas` |

`GEAS_CONFIG_HOME` overrides the root on every OS. `--geas-config` selects a
specific `config.yaml`, useful for a completely separate deployment.

## Managed provider and policy configuration

`config-init` installs every live provider, policy, workload, truth, deposit,
and query-vocabulary file beside `config.yaml`:

```text
config.yaml
providers.toml
source-policy.yaml
research-policy.yaml
truth-policy.yaml
deposit-policy.yaml
model-policy.yaml
budget-policy.yaml
workload-policy.yaml
query-vocabulary.yaml
defaults-state.json
```

The CLI resolves these files from the directory containing the selected
`config.yaml`. Global path options remain available for one-command overrides.
The source repository's `config/` directory and packaged copies are
installation templates, not the live defaults after initialization.

Geas records template and installed hashes in `defaults-state.json`. To adopt
new packaged defaults after upgrading:

```bash
geas config-init --update-defaults
```

Files still matching the previously installed bytes are updated atomically.
Operator-modified files are never overwritten; Geas preserves them and writes
a sibling `.new` candidate for review. After manually adopting or merging a
candidate, rerun `config-init --update-defaults` to reconcile the state.

## Ontology location and named profiles

`config.yaml` is explicit, versioned configuration. Its default profile points
at the private shared repository `liamhelmer-bel/ontologies`:

```yaml
version: 1
default_profile: default
ontology_freshness:
  check_before_use: true
  max_age_seconds: 3600
  hydrate_artifacts_before_use: false
ontology_defaults:
  provider: deepseek_local
  max_output_tokens: 65536
  model_parameters:
    thinking: true
    reasoning_effort: high
    temperature: 0.0
    top_p: null
    top_k: null
    min_p: null
    seed: null
    stop: []
  discovery_enabled: true
  include_gap_queries: true
  refresh_after_hours: 168
  max_queries: null
  result_limit: 30
  approve_large_queries: false
  repository_limit_per_query: 20
  timeout_seconds: 3600.0
  max_run_seconds: 1800.0
  minimum_model_window_seconds: 300.0
  finalization_reserve_seconds: 120.0
  work_claim_grace_seconds: 60.0
  connection_attempts: 10
  connection_retry_seconds: 2.0
  anchors_per_batch: 200
  max_batches_per_source: null
  max_sources: null
  model_parallelism: 1
profiles:
  default:
    ontology_directory: ontologies
    secret_sources:
      - path: secrets/common.env
        format: dotenv
    ontology_git:
      url: https://github.com/liamhelmer-bel/ontologies.git
      branch: main
      remote: origin
      pull_before_update: false
      push_on_update: false
```

The generated file also includes the complete generic `ontology_facets` list
and `debug_reasoning`. The abbreviated example above highlights the operational
settings operators most often change.

`ontology_defaults` applies to every ontology loaded with this Geas user
configuration. The eligible fields are generic facets, discovery and refresh
limits, provider/model parameters, worker timing and reserves, connection
retries, anchor batching, source/batch caps, and serial model parallelism.
Topic identity, description, scope and competency questions, queries, seed
bundles, source-library snapshot, repository freshness overrides, output path,
and tainted-source index stay ontology-specific.

An ontology `build.yaml` inherits an eligible field only when that field is
absent. A present value wins even when it is `null`; `model_parameters` is
merged field-by-field. The resulting complete configuration is strictly
validated before any discovery or model call. `ontology-init` still writes all
effective fields so a new ontology remains inspectable. Running `config-init`
after an upgrade materializes newly introduced global defaults without
overwriting values already present in `config.yaml`.

Create an ontology in the selected profile and later address it by name:

```bash
uv run geas ontology-init \
  --topic "Model routing for AI red and blue teaming" \
  --concept-id concept:model-routing-for-ai-red-blue-teaming

uv run geas ontology-build model-routing-for-ai-red-blue-teaming \
  --root data/model-routing-for-ai-red-blue-teaming \
  --check
```

Passing a directory to `ontology-init` preserves the explicit workspace-local
workflow. `ontology-build` and `library-build` accept either an explicit path
or an ontology name from the selected profile.

List available ontologies with `geas ontology-list`. Pass a directory to
inventory another direct-child ontology root, for example
`geas ontology-list /srv/team/ontologies`.

Add profiles for different teams or security domains. Each can select its own
ontology directory, secret sources, and Git repository. Select one with the
global `--geas-profile NAME` option. Profile paths are confined beneath the
directory containing `config.yaml`.

## Modular secret sources

Secret values live under the common configuration root, outside the ontology
repository. A profile can load an ordered list of dotenv, YAML, or JSON
mappings:

```yaml
profiles:
  red-team:
    ontology_directory: teams/red/ontologies
    secret_sources:
      - path: secrets/common.env
        format: dotenv
      - path: secrets/red-team.yaml
        format: yaml
      - path: secrets/vendor.json
        format: json
    ontology_git: null
```

Only credential names explicitly authorized by trusted provider or connector
configuration are read. Sources are processed in order and never overwrite an
already populated environment variable. Values in YAML and JSON must be
strings. The legacy global `--env-file PATH` option remains an explicit
one-command override.

`config-init` creates `secrets/.gitignore` containing a deny-by-default rule;
it never creates a secret value.

## Shared Git repository

Pull the selected profile repository, cloning it on first use:

```bash
uv run geas ontology-sync --pull
```

Commit and push ontology-directory changes:

```bash
uv run geas ontology-sync --push --message "geas: update routing ontology"
```

Use both flags to fast-forward before pushing. Pull refuses a dirty checkout,
uses a fixed configured remote and branch, and permits only fast-forward
integration. Push stages only the selected scope, rejects unrelated previously
staged paths, scans file names and contents for common credential forms, then
commits and pushes without placing credentials in the repository URL.

The checkout receives a conservative `.gitignore` that excludes `.env` files,
private-key formats, credential- and secret-named paths, SQLite databases,
runtime `data/`, hydrated `.geas-artifacts/`, model logs, reasoning logs, and
checkpoints. These controls
reduce accidental disclosure; they are not a substitute for repository access
controls, forge secret scanning, or credential rotation after an exposure.

Set `pull_before_update` or `push_on_update` per profile to make
profile-backed `ontology-init` pull before and/or push its configuration update.
When a configured checkout does not exist yet, `ontology-init` performs the
initial pull before writing unless `--no-pull` is explicit. Named ontology and
library builds also honor `pull_before_update` before reading configuration.
The command-line `--pull`/`--no-pull` and `--push`/`--no-push` flags override
those settings for one invocation. Model proposals never acquire automatic
promotion authority: use the normal review and promotion workflow before
treating generated knowledge as canonical.

By default, named ontology use also performs a freshness-throttled Git check.
The successful check time is cached outside Git, so repeated uses within one
hour do not contact the remote. Configure `ontology_freshness` globally or the
nullable `repository_sync` fields in an ontology's `build.yaml`. See
[portable ontology artifacts](PORTABLE_ONTOLOGY_ARTIFACTS.md) for conditional
database publication and lazy hydration.

## Obsidian-style Markdown export

Export an accepted topic projection as a deterministic cross-linked Markdown
vault:

```bash
uv run geas topic-export \
  concept:model-routing-for-ai-red-blue-teaming \
  data/model-routing-for-ai-red-blue-teaming/obsidian \
  --database data/model-routing-for-ai-red-blue-teaming/query.sqlite \
  --format obsidian
```

The export contains `index.md` plus typed `concepts/`, `claims/`, `sources/`,
`controversies/`, `gaps/`, `threats/`, and `references/` directories. Notes use
stable hashed filenames, Obsidian wikilinks, and frontmatter that marks them as
non-canonical projections. Exact evidence remains visibly marked as untrusted
source text.

An identical export is idempotent. A differing existing directory fails closed;
pass `--force` to atomically replace it and remove stale generated notes.

Use `--format agent-instructions --vault-link PATH` for a project expert
handoff that links to the vault and original HTTP(S) sources. The complete
workflow, including how to reference the handoff from a project's existing
agent instructions, is in [Build and use Geas end to end](GETTING_STARTED.md).
