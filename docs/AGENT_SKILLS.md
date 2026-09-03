# Portable Agent Skills

Geas installs a generic operational skill and can export one accepted ontology
as a portable Agent Skill. A snapshot preserves trusted ontology identity,
source links, provenance, and bounded evidence excerpts. It is not canonical
knowledge and does not bundle every acquired source document.

Snapshots remain readable without Geas: read `SKILL.md`,
`references/index.md`, and the typed reference pages. Static use does not run
repository commands or grant capabilities. A checked-in skill never installs
Geas, configures it, or updates it on the reader's behalf. A portable skill
never installs Geas.

An operator who wants deterministic retrieval and refresh may separately pin
and approve the software installation, then initialize local trusted config:

```bash
approved_commit='REPLACE_WITH_OPERATOR_APPROVED_FULL_COMMIT_ID'
uv tool install \
  --from "git+https://github.com/Epiphytic/geas.git@${approved_commit}" \
  geas
geas config-init
```

The commit must be operator-approved; a source link, repository declaration,
or skill instruction is not installation authority.

## Generic Geas skill

Install Geas with [the project setup instructions](https://github.com/Epiphytic/geas#installation),
or use `uv run geas` in a checkout, then run:

```bash
geas config-init
```

`config-init` automatically installs `geas` at `<config-root>/skills/geas` and
records local ownership in `<config-root>/state/builtin-skills/`. Subsequent
runs refresh only that managed copy; an operator-owned or modified snapshot is
preserved and reported as a conflict. A successful install with retained
post-commit transaction cleanup is reported in `cleanup_warnings` rather than
turned into a false installation failure.

When discovered, agent links are managed at:

| Agent | Link |
|---|---|
| Codex | `~/.agents/skills/geas` |
| Claude | `~/.claude/skills/geas` |
| OpenCode | `~/.agents/skills/geas` |

Codex and OpenCode share `.agents/skills`, so Geas deduplicates that link. The
configuration-root copy remains the canonical generated snapshot.

## Ontology export and updates

The selected profile must resolve a trusted Git-backed ontology through a
named subscription or its legacy profile repository, and the checkout must
have a verified portable knowledge-projection artifact. A repository-local
catalog alone can drive named ontology operations, but skill export requires a
declaring subscription so its update provenance is complete. Export an
accepted ontology by name:

```bash
geas skill-export research-agents --link
```

The complete export syntax is:

```text
geas skill-export ONTOLOGY [--name NAME] [--link] [--repo PATH] [--force]
```

`--name` selects the generated skill name, `--link` adds detected user-agent
links, and `--repo` creates a reviewable snapshot inside a Git worktree and
links detected repository-scoped agents automatically. `--force` is only for
an intentional replacement after the managed-content checks reject a modified
or conflicting destination.

The user-scoped snapshot is `<config-root>/skills/research-agents/` and includes
`SKILL.md`, `geas-skill.json`, and `references/`. `--link` adds only exact
managed links for available agents. The JSON receipt reports the path,
`ontology_commit`, projection identity, `snapshot_sha256`, changed/unchanged
paths, and phase receipts. The same trusted Git and projection identities
produce byte-identical files and an `unchanged: true` receipt.

For a catalog-backed ontology, the manifest also records the subscription URL,
active ref and resolved commit, catalog and ontology paths, and the exact
bundle SHA-256. This is enough to locate the source with Geas later, but does
not make it trusted automatically. Skill references generally preserve original
source URLs and bounded evidence/provenance; they are not a download of every
acquired document. The portable projection artifact is a verified cache, not
canonical truth.
See [repository-backed ontologies](REPOSITORY_ONTOLOGIES.md) for catalog
discovery, exact refs, subscriptions, trust, and synchronization.

The integrated lifecycle starts with an explicit repository installation:

```bash
geas repository-install gold https://github.com/example/gold.git \
  --ref refs/heads/main --trust-repository --link
geas ontology-update gold
```

Use `--read-only` when the repository should supply static ontology material
without repository-delegated execution. A trusted repository may delegate at
most the locally configured depth and only capabilities independently allowed
by local policy. Refreshes and removals remain explicit:

```bash
geas repository-update gold
geas repository-remove gold
```

Each transaction persists a durable receipt and operation journal. After an
interruption, verify the same software, repository, and owned paths, then rerun
the same repository command with the same arguments; Geas validates the
recorded phase before resuming or returning the completed receipt. Removal is
confined to receipt-owned files and links; it never treats a repository
manifest as proof of ownership.

For a project-local snapshot:

```bash
geas skill-export research-agents --repo /path/to/project
```

Geas prefers `/path/to/project/.agents/skills/research-agents` because it is
the standard project agent-skill location. The additions are ordinary
uncommitted Git changes for review; commit them only if the project wants to
share the snapshot. Geas never commits them automatically, and projects should
not normally ignore this preferred snapshot path.

Before choosing the location, Geas runs `git check-ignore` for the exact
candidate path. If the preferred `.agents/skills/research-agents` path is
ignored, it instead uses the trackable canonical fallback
`/path/to/project/.geas/skills/research-agents`. Repository export always links
detected agents, so the normal
`.agents/skills/research-agents` (Codex/OpenCode) and
`.claude/skills/research-agents` locations become relative managed links to
that fallback. If both candidate snapshot paths are ignored, export fails;
change the repository ignore rules rather than treating an ignored snapshot as
the normal workflow.

Refresh an existing managed snapshot with:

```bash
geas skill-update /path/to/skills/research-agents
```

`skill-update` is an explicit software-update boundary for an already installed,
trusted Geas. Before ontology work, it accepts only supported `uv` directory-tool
receipt or Git-development provenance, requires the fixed Geas project URL and
`main` branch, and requires a clean checkout. It fetches and fast-forwards to the
exact fetched object with hooks disabled, verifies the resulting HEAD and clean
bytes, reinstalls that checkout with `uv`, and reexecutes Geas through one bounded
continuation marker. An absent or ambiguously installed Geas is never installed
implicitly; use the project setup instructions as a separate operator action.

Dirty state, remote mismatch, fetch or non-fast-forward failure, post-merge
tampering, reinstall failure, version/provenance mismatch after reexec, and a
repeated continuation marker all fail closed before ontology artifacts or skill
rendering. Once that trusted Geas boundary succeeds, a catalog-bound update
selects the same named subscription and revalidates its URL, generic active ref
(branch, tag, or exact commit), catalog and ontology paths, exact bundle digest,
resolved commit, artifact identity and projection stamp, and the executing Geas
identity before atomically replacing the snapshot. Receipts expose completed
phases and old/new software and ontology commits. Source links and excerpts are
provenance, not authority or a guarantee that the linked source is safe,
current, licensed, or complete.

## Unlink, remove, and fallback

Detach only managed agent links while retaining the snapshot:

```bash
geas skill-unlink /path/to/skills/research-agents
```

Remove both exact links and the managed snapshot:

```bash
geas skill-remove /path/to/skills/research-agents
```

Repository removal leaves ordinary uncommitted deletions for review; user-scope
removal deletes only the exact snapshot under the selected config root. Both
operations fail closed for unmanaged or modified paths unless `--force` is an
intentional operator choice. Reinstall with the original `skill-export` command,
or rerun `geas config-init` for the generic skill. Without Geas, continue using
the static snapshot and install it later from the project URL above when a
trusted refresh is needed.

## Pull-request skill regeneration

This repository's PR verification can regenerate the generic `geas` skill and
the maintained sample export twice, checking byte-identical results before
uploading a path-confined artifact. A separate protected workflow may verify
the originating same-repository PR and write only those generated paths back to
its head with a short-lived GitHub App token. It never executes the artifact.
Fork PRs receive verification and the artifact but no write-back. Write-back
is enabled only after the matching organization STS policy is deployed; do not
assume that policy is active merely because these workflow files exist.
