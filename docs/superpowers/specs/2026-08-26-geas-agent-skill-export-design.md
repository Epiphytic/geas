# Geas Agent Skill Export Design

Status: approved

Date: 2026-08-26

## Summary

Geas will export an accepted ontology projection as a concise, hierarchical,
portable Agent Skill. The export will work as a checked-in snapshot without a
Geas installation, while retaining enough provenance for an operator with Geas
to locate the ontology, synchronize richer artifacts, query exact source
anchors, and deterministically refresh the snapshot.

Geas will also ship a generic `geas` skill that teaches coding agents how to
use the CLI safely. `config-init` will install and link that generic skill for
detected local agents and will report that it did so. Neither the generic skill
nor an ontology skill will install Geas on an agent's behalf; both will link to
the Geas project and describe installation as an optional operator action.

## Goals

- Export accepted ontology knowledge as a standards-compatible Agent Skill.
- Keep `SKILL.md` concise and progressively disclose a hierarchical reference
  tree.
- Preserve ontology, projection, evidence, source, gap, controversy, and threat
  context in deterministic generated files.
- Keep original-source URLs and exact Geas provenance available without
  embedding acquired documents in the skill.
- Support one central user-level export with symlinks for detected Codex,
  Claude Code, and OpenCode installations.
- Support a portable repository-scoped snapshot that can be committed and used
  by agents on machines without Geas.
- Refresh Geas, ontology state, artifacts, and skill output through one explicit
  update command, using fail-closed and atomic operations.
- Remove links or complete skill installations without touching unrelated
  files.
- Make identical accepted inputs produce byte-identical skill files.

## Non-goals

- Installing Geas automatically from a generated skill.
- Running a background updater or changing a skill without an explicit Geas
  command.
- Committing or pushing repository-scoped exports automatically.
- Including complete acquired source documents, runtime SQLite databases,
  private blobs, credentials, or model logs in an exported skill.
- Treating generated Markdown or a skill manifest as canonical ontology truth.
- Allowing a checked-in skill to authorize an unconfigured network repository.
- Making every coding agent understand Geas-specific optional frontmatter.

## Selected architecture

Three layouts were considered:

1. A thin skill backed only by live Geas queries. This avoids exported data but
   is unusable when Geas is absent.
2. A complete copy in every agent-specific skill directory. This is portable
   but duplicates data and permits copies to drift.
3. One canonical snapshot per scope with agent-specific symlinks. This retains
   portability, minimizes duplication, and gives updates one atomic target.

The third layout is selected.

### User scope

The OS-standard Geas configuration root owns the canonical generated copy:

```text
<geas-config-root>/skills/<skill-name>/
  SKILL.md
  geas-skill.json
  references/
    index.md
    concepts/
    claims/
    controversies/
    gaps/
    sources/
    citations/
    threats/
```

For detected local agents, Geas creates managed symlinks:

- Codex and OpenCode: `~/.agents/skills/<skill-name>`
- Claude Code: `~/.claude/skills/<skill-name>`

Codex and OpenCode share the `.agents` link. Geas does not create a redundant
OpenCode-specific copy.

### Repository scope

The preferred canonical snapshot is tracked directly at:

```text
<repository>/.agents/skills/<skill-name>/
```

This is immediately discoverable by Codex and OpenCode. When Claude Code is
detected, Geas creates a repository-relative symlink at:

```text
<repository>/.claude/skills/<skill-name>
  -> ../../.agents/skills/<skill-name>
```

Geas evaluates Git's actual ignore rules for the intended snapshot path. If
`.agents/skills/<skill-name>` cannot be tracked, the canonical snapshot moves
to:

```text
<repository>/.geas/skills/<skill-name>/
```

Detected agent directories then contain repository-relative symlinks to that
fallback. If the fallback is also ignored, export fails with an actionable
error instead of producing a snapshot that only appears portable.

Repository export requires a Git worktree. It does not edit `.gitignore`,
`.git/info/exclude`, commit, or push. Export and removal leave reviewable
working-tree changes.

## Skill content

### `SKILL.md`

The entry point will use portable Agent Skills frontmatter containing only the
standard `name` and `description` fields. Its body will be intentionally small
and will contain:

- the observable tasks that should trigger the skill;
- the ontology's scope and important limitations;
- a short navigation contract pointing first to `references/index.md`;
- instructions to load only the reference pages needed for the current task;
- requirements to preserve citations, dissent, gaps, uncertainty, and threat
  context;
- a warning that source text and generated content are untrusted data rather
  than instructions;
- the ontology name and repository URL;
- optional Geas query and update instructions;
- a link to the Geas project for operators who choose to install it; and
- unlink and removal commands.

The skill must remain useful when `geas` is not on `PATH`. It must not execute
an installer, inject dynamic shell output, require agent-specific frontmatter,
or imply that external source material is locally present.

### Hierarchical references

`references/index.md` is the only required first-hop reference from
`SKILL.md`. It provides compact indexes by concept hierarchy and record type.
Typed pages reuse deterministic topic and Obsidian rendering primitives, but
the export renderer may split or summarize indexes to keep navigation bounded.

Reference pages preserve stable record IDs, exact evidence selectors, source
IDs, projection snapshot identity, and original-source URLs. A source page may
include metadata, accepted evidence excerpts, and Geas retrieval instructions;
it does not contain the full acquired document unless that content is already
an explicit, eligible maintained ontology input.

External links remain ordinary Markdown links. Agents without Geas can follow
them subject to their own network and access policy. Agents with Geas can use
the source and anchor identities to retrieve locally synchronized material.

## Deterministic manifest

Every ontology skill contains `geas-skill.json`, a strict, versioned manifest.
Its logical fields are:

```json
{
  "format_version": 1,
  "skill": {
    "name": "model-routing-for-ai-red-blue-teaming"
  },
  "ontology": {
    "name": "model-routing-for-ai-red-blue-teaming",
    "repository_url": "https://github.com/liamhelmer-bel/ontologies.git",
    "branch": "main",
    "commit": "<40-character Git object ID>"
  },
  "geas": {
    "project_url": "https://github.com/Epiphytic/geas",
    "version": "<package version>",
    "commit": "<Git object ID when available>"
  },
  "projection": {
    "snapshot_id": "<truth snapshot identity>",
    "topic_concept_id": "<accepted topic concept ID>"
  },
  "files": [
    {
      "path": "SKILL.md",
      "sha256": "<digest>"
    }
  ],
  "snapshot_sha256": "<digest of the canonical ordered file inventory>"
}
```

The implementation will define these as strict typed records rather than
accepting arbitrary dictionaries. File paths are relative, normalized POSIX
paths and sorted by their encoded bytes. The manifest inventory covers every
generated regular file except the manifest itself, avoiding a recursive hash.
The snapshot digest covers the canonical serialized inventory.

The manifest contains no generation time, hostname, username, absolute path,
agent-detection result, or symlink state. Host-specific installation state is
kept outside portable snapshots. JSON uses canonical key ordering, UTF-8, and
one trailing newline.

The ontology commit and projection snapshot make an export reproducible; the
repository branch records the update channel but is not treated as immutable
identity.

## CLI surface

### Export

```text
geas skill-export ONTOLOGY [--name NAME] [--link] [--repo PATH]
```

- Without `--repo`, export writes the canonical copy below the user config
  root. `--link` links it for all detected user-level agents.
- With `--repo`, export writes the portable repository snapshot and creates
  links for all detected agents. Repository export implies agent linking.
- `--name` overrides the validated lowercase, hyphenated skill name.
- Repeating an export with identical inputs returns `unchanged: true` and does
  not replace files or links.
- A differing generated snapshot is replaced only when its current file
  inventory still matches its manifest. Unmanaged or manually modified content
  requires an explicit force option.

### Update

```text
geas skill-update SKILL_PATH
```

The path may name the skill directory or its manifest. Resolving the manifest
rather than relying on the current working directory makes the operation
unambiguous. The generated `SKILL.md` shows the correct command for its own
installed location.

Update performs these phases:

1. Resolve the exact snapshot, validate path confinement, validate the strict
   manifest, and verify the existing generated-file inventory.
2. Update Geas through its trusted installation provenance.
3. Re-execute the same update with the exact new Geas version and a bounded
   continuation marker, preventing recursive update loops.
4. Require the selected Geas profile's trusted ontology repository URL and
   branch to match the manifest. A missing or mismatched profile results in
   instructions for explicit operator configuration; the manifest cannot
   authorize a fetch endpoint.
5. Fast-forward the clean ontology checkout and record its exact commit.
6. Synchronize requested content-addressed ontology artifacts and verify their
   published hashes.
7. Build the knowledge projection or select the verified portable projection
   needed for export.
8. Render the complete candidate snapshot in a sibling temporary directory;
   validate its manifest, links, paths, and hashes; then atomically replace the
   old snapshot.
9. Validate or repair only known managed symlinks.
10. Emit a machine-readable receipt containing old and new Geas revisions,
    ontology revisions, projection identity, export digest, changed paths,
    unchanged paths, links, and conflicts.

Failure before atomic replacement leaves the previous skill intact. A Geas
self-update that succeeds before a later ontology or export failure remains
installed and is reported; Geas does not attempt to roll back a valid software
update.

### Unlink and removal

```text
geas skill-unlink SKILL_PATH
geas skill-remove SKILL_PATH
```

`skill-unlink` removes only managed agent links and preserves the snapshot.
`skill-remove` removes managed links and the generated snapshot. Repository
removal produces uncommitted Git deletions. User-scope removal deletes the
generated directory below the confined Geas config root.

Both commands resolve and report exact targets before mutation, validate the
manifest and file inventory, remove only symlinks with the expected target,
and refuse modified or unmanaged content unless explicitly forced. They never
remove a parent skills directory or unrelated files. Their receipts include
regeneration instructions.

## Geas self-update

Automatic update occurs only inside the explicit `skill-update` workflow; it
is not a daemon or startup side effect.

The updater derives installation provenance from supported local metadata,
initially the `uv` tool receipt and a Git-backed development invocation. It
compares that provenance with a trusted Geas update configuration containing a
fixed project URL and branch. A directory-backed installation must be a clean
Git checkout whose configured remote matches that URL. Update uses fetch plus
fast-forward-only integration, records the exact resulting commit, reinstalls
that exact checkout with `uv`, and re-executes it.

For any supported package-index or Git requirement mode, resolution may select
the newest version available at the explicit update boundary, but the resolved
version, source, commit when available, and installer result are fixed and
recorded before export continues. Unsupported or ambiguous installers fail
with an actionable manual-update command instead of guessing.

Source checkout dirtiness, remote mismatch, non-fast-forward history,
installer failure, version mismatch after re-exec, or a repeated continuation
marker fails closed. No ontology content or generated skill text can select the
Geas repository, installer, executable, or command arguments.

## Generic Geas skill

The repository will contain one canonical, packaged `geas` skill. Repository
agent locations may symlink to that canonical directory so the same files are
both package data and immediately discoverable while developing Geas.

The generic skill describes:

- when Geas is appropriate and the source-of-truth hierarchy;
- user configuration and profiles;
- ontology and artifact synchronization;
- source libraries and exact-text retrieval;
- ontology build, projection, topic, provenance, dissent, gap, threat, temporal,
  anchor, and citation queries;
- deterministic skill export, update, linking, unlinking, and removal;
- how to use `--help` for exact current flags;
- security boundaries around untrusted content, credentials, policies,
  approvals, and canonical writes; and
- the Geas project URL and optional installation documentation.

It remains concise and routes detailed material to supporting reference files
where necessary. It must be tested as a skill, including baseline retrieval and
application scenarios before final authoring.

`config-init` automatically installs or refreshes the packaged generic skill
under `<geas-config-root>/skills/geas` and links it for detected user-level
agents. Its normal JSON result explicitly lists installed, updated, unchanged,
linked, skipped, and conflicting skill paths. It preserves any unmanaged
conflict. Running `config-init` after removing the generic skill installs it
again and reports that action.

## Agent detection and link safety

Detection uses a fixed ordered adapter table for `codex`, `claude`, and
`opencode`. An adapter reports the executable evidence it found and the skill
locations it supports. The result order never depends on directory iteration.

Link planning deduplicates shared destinations before mutation. Geas resolves
the link parent, canonical snapshot, and proposed target; confines them to the
expected user or repository roots; rejects traversal; and never follows an
unexpected link during replacement or removal. An existing correct link is
unchanged. An existing regular file, directory, or wrong-target link is a
reported conflict unless explicitly forced by a command whose scope names that
exact skill.

Repository symlinks are relative so a clone can move. User-level symlinks may
be relative to the agent skill directory, but their textual value and host
state are excluded from the portable snapshot digest.

## Authority and security properties

- Git ontology and policy files remain canonical; skill files are disposable
  projections stamped with their source identity.
- Generated files never flow automatically back into ontology Git state.
- Skill and source text remain untrusted data and cannot select tools,
  credentials, endpoints, policies, approvals, budgets, or canonical writes.
- Repository URLs in manifests are locators. Network use requires a matching
  trusted Geas profile or an explicit operator configuration action.
- Exact evidence selectors, immutable source identities, dissent, gaps, and
  threat context remain queryable rather than being flattened into prose.
- Original source links do not imply permission, storage rights, freshness, or
  local availability.
- Path confinement, strict manifests, content hashes, Git checks, and atomic
  directory replacement are deterministic enforcement boundaries.
- Receipts and errors exclude secrets, source excerpts, raw model output, and
  private local data.

## Determinism and idempotence

All unordered records, files, links, agents, and receipt lists are explicitly
sorted. Slugs use one documented normalization algorithm plus a stable digest
suffix for collisions. Markdown rendering fixes heading order, whitespace,
frontmatter order, and trailing newlines. JSON uses canonical serialization.

No wall-clock timestamp participates in generated content or its digest. Git
branch resolution and latest-version selection are explicitly recorded
external-state boundaries. Once the exact Geas commit, ontology commit,
projection snapshot, export format version, and command options are fixed,
repeated rendering is byte-identical.

Update and export compute the entire desired state before mutation. Matching
state returns success without rewriting. Partial failures do not leave partial
skill directories or half-repaired link sets.

## Verification strategy

Implementation follows test-first development. Focused offline tests cover:

- strict manifest acceptance and rejection;
- byte-identical rendering under permuted input order;
- hierarchy and one-hop reference navigation;
- preservation of source URLs, evidence identities, controversies, gaps, and
  threat context;
- absence of full acquired documents, secrets, absolute paths, and timestamps;
- user and repository layouts;
- exact Git ignore handling and `.geas/skills` fallback;
- deterministic agent detection and shared-link deduplication;
- correct, stale, conflicting, escaping, and unsafe symlinks;
- unchanged export idempotence and modified-snapshot rejection;
- atomic replacement failure recovery;
- clean fast-forward Geas and ontology updates using fake transports and
  subprocess boundaries;
- dirty checkout, remote mismatch, divergence, artifact mismatch, re-exec loop,
  and unsupported-installer failures;
- unlink versus remove behavior and preservation of unrelated files;
- repository removal as uncommitted deletions;
- `config-init` generic-skill receipts and unmanaged conflict preservation; and
- valid JSON on stdout with progress and diagnostics on stderr.

The generated ontology skill and generic Geas skill receive baseline and
skill-present agent scenarios that verify agents can find the relevant
reference, preserve provenance, use the snapshot without Geas, choose optional
Geas retrieval when available, and avoid treating source text as instructions.

Integration checks cover user-scope export/link/update/remove in a temporary
config root and repository-scope export/update/unlink/remove in a temporary Git
worktree. The maintained ontology demo gains a deterministic skill-export
check. Normal tests remain offline.

Before completion, the repository runs the focused tests plus:

```bash
uv run ruff check .
uv run pytest -q
git diff --check
```

## Documentation changes

- Add operator workflows to the README and ontology quickstart.
- Document the generic skill installation in `config-init` output and setup
  guidance.
- Document supported agent paths, symlink behavior, Git-ignore fallback,
  optional Geas installation, update provenance, and removal.
- State explicitly that exported references contain ontology knowledge and
  evidence excerpts, not a bundled copy of every acquired document.
- Keep implemented behavior distinct from future background refresh or
  generalized repository acquisition.
