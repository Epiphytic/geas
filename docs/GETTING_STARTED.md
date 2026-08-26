# Build and use Geas end to end

This guide covers building Geas, configuring model and discovery providers,
adding repository material, maintaining ontologies, supplying attributable
context to agents, and exporting project-specific AI expert instructions.

For a path-aware walkthrough at any time, run:

```bash
uv run geas setup-guide --format markdown
```

Omit `--format markdown` for structured JSON that another process can inspect.
The walkthrough reports configuration paths, provider names, required
environment-variable names, and ordered commands; it never loads or displays
secret values.

## 1. Build the project

Requirements are Python 3.12 or newer, Git, and `uv`:

```bash
git clone https://github.com/Epiphytic/geas.git
cd geas
uv sync --extra dev
uv run geas --help
```

Run the offline verification suite:

```bash
uv run ruff check .
uv run pytest -q
git diff --check
```

To make `geas` available outside the checkout, install it as a `uv` tool:

```bash
uv tool install .
geas --help
```

Run `geas config-init` after installation. It copies packaged provider and
policy templates into the OS-standard user configuration root, and subsequent
CLI invocations resolve those live files from any working directory. Global
path options remain available for deliberate one-command overrides.

## 2. Initialize user configuration

Create the OS-standard configuration, shared ontology, and modular-secret
locations:

```bash
uv run geas config-init
```

On Linux this normally creates `~/.config/geas/config.yaml`, every live
provider and policy file, `~/.config/geas/ontologies/`, and
`~/.config/geas/secrets/`. XDG, macOS, Windows, overrides, team profiles, safe
default upgrades, and Git synchronization are described in
[User configuration](USER_CONFIG.md). Checked-in `config/` files are packaged
templates and maintained-demo inputs; normal CLI defaults resolve the user
copies.

Inspect the generated walkthrough with its resolved paths:

```bash
uv run geas setup-guide --format markdown
```

## 3. Configure APIs and LLMs

List configured model routes without exposing credentials:

```bash
uv run geas providers
```

The checked-in provider configuration includes:

| Provider | Use | Credential |
|---|---|---|
| `deepseek_local` | Local OpenAI-compatible endpoint | none |
| `openai` | External API proposal generation | `OPENAI_API_KEY` |
| `zai` | External API proposal generation | `ZAI_API_KEY` |
| `codex_oneshot` | Tool-isolated Codex CLI proposal generation | CLI authentication |
| `claude_oneshot` | Tool-isolated Claude Code proposal generation | CLI authentication |

Discovery can additionally use `MOJEEK_API_KEY`, `OPENALEX_API_KEY`, and
`UNPAYWALL_EMAIL`. `setup-guide` derives the complete list from the active
trusted configuration.

Put only the values you use into the first `secret_sources` file named by the
selected profile. The default dotenv form is:

```dotenv
MOJEEK_API_KEY=replace-me
OPENALEX_API_KEY=replace-me
UNPAYWALL_EMAIL=operator@example.org
OPENAI_API_KEY=replace-me
ZAI_API_KEY=replace-me
```

Keep the file mode private:

```bash
chmod 600 ~/.config/geas/secrets/common.env
```

Geas reads only credential names explicitly allowlisted by the provider or
connector being used. It never shell-evaluates the dotenv file. YAML and JSON
secret mappings and per-team sources are also supported.

To add or change an LLM, update all of these boundaries together:

1. `~/.config/geas/providers.toml` fixes the client kind, endpoint, model, credential
   name, external/local classification, and capacities.
2. `~/.config/geas/model-policy.yaml` authorizes the exact provider, endpoint, model,
   operation, and data classes.
3. `~/.config/geas/budget-policy.yaml` limits calls and token/cost exposure.
4. `ontology_defaults` in `~/.config/geas/config.yaml` selects the inherited
   provider, output limit, and model parameters; present fields in an
   ontology's `build.yaml` override them.

An entry in one file alone does not authorize a new route. Test a configured
provider through the same policy and accounting gates used by real work:

```bash
uv run geas model-smoke --provider deepseek_local
```

For `deepseek_local`, start the configured OpenAI-compatible server before the
smoke test. For `codex_oneshot` or `claude_oneshot`, install and authenticate
the corresponding CLI. Those routes remain tool-free proposal generators
inside Geas; they cannot select sources, credentials, workflow transitions, or
canonical writes.

## 4. Add a repository or document corpus

For an operator-selected local checkout, run deterministic discovery and
acquisition over the confined directory:

```bash
uv run geas research-local \
  "Which architecture, security, and operational facts must an expert know?" \
  --corpus /srv/projects/example \
  --concept concept:example-project \
  --term architecture \
  --term security \
  --root data/example-project
```

This archives matching supported files as immutable sources. It does not treat
repository text as instructions. Parse the operator-selected files that should
become searchable exact anchors:

```bash
uv run geas parse-document /srv/projects/example/README.md \
  --root data/example-project

uv run geas parse-document /srv/projects/example/docs/architecture.md \
  --root data/example-project
```

Individual files can instead enter through `source-add`, `deposit-add`, or
`parse-document`, depending on the provenance and handling information needed.

The autonomous ontology builder can discover supported GitHub repository hits,
resolve them through the official API at immutable commits, and parse their
README content. Put targeted discovery terms in the ontology's `queries` and
repository identities in `library.yaml`. Arbitrary direct repository-tree
acquisition and link traversal are not yet implemented; do not assume that a
repository selector downloads missing content.

## 5. Create and maintain an ontology

Create complete explicit configuration in the selected user profile:

```bash
uv run geas ontology-init \
  --topic "Example project architecture and operations" \
  --concept-id concept:example-project
```

Edit the resulting `build.yaml` before live work. Define scope criteria,
ontology facets, competency questions, discovery queries, source limits, model
route, output capacity, timing, and paths. Generic worker fields may instead be
set once under global `ontology_defaults`; ontology-local fields win when
present. Validate without network or model calls:

```bash
uv run geas ontology-build example-project \
  --root data/example-project \
  --check
```

Build or resume:

```bash
uv run geas ontology-build example-project \
  --root data/example-project
```

Each worker is bounded and resumable. Acquired sources, exact anchors,
validated proposals, gaps, and checkpoints survive interruption. Model output
remains proposal-only. Review candidate bundles and use the Git promotion
workflow before treating them as accepted knowledge; rerun the build after
promotion to rebuild the accepted SQLite and Markdown projections.

Synchronize shared configuration explicitly when needed:

```bash
uv run geas ontology-sync --pull
uv run geas ontology-sync --push --message "geas: update example ontology"
```

Profile settings can enable pull-before-update and push-on-update for
`ontology-init`. Generated proposals never gain automatic promotion authority.
Named ontology commands perform a freshness check by default and cache a
successful remote check for one hour. This avoids a Git fetch on every command.
Global and per-ontology overrides, plus conditional SQLite/generated-content
publication and lazy hydration, are documented in
[portable ontology artifacts](PORTABLE_ONTOLOGY_ARTIFACTS.md).

List ontologies from the selected profile or another direct-child ontology
root:

```bash
uv run geas ontology-list
uv run geas ontology-list /srv/shared/geas-ontologies
```

## 6. Use Geas with agents

Build a deterministic source library after parsing selected material:

```bash
uv run geas library-build example-project \
  --root data/example-project \
  --database data/example-project/library.sqlite
```

Give an agent a bounded attributable context package:

```bash
uv run geas library-context \
  "Which component owns authentication and authorization?" \
  --database data/example-project/library.sqlite \
  --limit 20 \
  --max-characters 16000
```

The JSON response includes the deterministic compiled query, immutable library
snapshot, exact fragments, offsets, source identities, provenance, threat
observations, and truncation status. An agent should treat every fragment as
untrusted data and cite its source identity rather than following instructions
inside it.

For accepted ontology knowledge, agents can use:

```bash
uv run geas knowledge-query \
  "authentication ownership and failure modes" \
  --database data/example-project/query.sqlite

uv run geas topic-show concept:example-project \
  --database data/example-project/query.sqlite
```

These commands use deterministic lexical, typed SQL, hierarchy, provenance,
dissent, temporal, gap, threat, anchor, and citation queries. There is no
invisible LLM ranking step.

## 7. Export an AI expert for a particular project

Exports require an accepted topic projection. Proposal-only claims must first
pass review and promotion.

Create a cross-linked Obsidian-style knowledge hierarchy inside the target
project:

```bash
uv run geas topic-export \
  concept:example-project \
  /srv/projects/consumer/docs/geas-expert \
  --database data/example-project/query.sqlite \
  --format obsidian
```

This creates `index.md` and typed `concepts/`, `claims/`, `sources/`,
`controversies/`, `gaps/`, `threats/`, and `references/` directories. Claim
notes link to source notes and preserve exact evidence. Source notes retain the
immutable source ID, content hash, archived locator, and original locator.
Reference notes retain DOI, PMID, PMCID, arXiv, or public-URL relationships
when available.

Create a project instruction handoff that links to that vault and to original
HTTP(S) sources:

```bash
uv run geas topic-export \
  concept:example-project \
  /srv/projects/consumer/GEAS_EXPERT.md \
  --database data/example-project/query.sqlite \
  --format agent-instructions \
  --vault-link docs/geas-expert/index.md
```

`GEAS_EXPERT.md` contains fixed operating instructions, the snapshot identity,
an original-source link index, and the accepted topic projection. It tells the
agent to preserve dissent and gaps, cite claim/source identities, verify exact
wording through original links, and never obey instructions found in source
material.

Do not replace an existing project `AGENTS.md` automatically. Add a small,
reviewed reference in the appropriate scope instead:

```markdown
## Domain expertise

For architecture, security, and operational questions about this project, read
[`GEAS_EXPERT.md`](GEAS_EXPERT.md). Follow its provenance requirements and use
the linked Geas vault to inspect claims, gaps, and exact source evidence.
```

Both files are disposable projections. Regenerate them from the accepted
snapshot after ontology updates. An identical Obsidian export is idempotent; a
differing directory requires `--force` for atomic replacement and stale-note
removal.

## 8. Verify provenance from an export

Use this path when an exported claim matters:

```text
agent instruction or vault index
  -> claim note and claim ID
  -> exact evidence ID and selector
  -> source note and immutable source version
  -> archived/original locator and content SHA-256
  -> original HTTP(S), repository, DOI, or local source
```

The original link is a verification route, not proof by itself. Accepted claims
remain grounded in the immutable archived bytes and exact selector recorded by
Geas. If the live page changes, the archived source identity and hash preserve
which version supported the claim.
