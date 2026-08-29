# Portable Agent Skills

Geas installs a generic operational skill and can export one accepted ontology
as a portable Agent Skill. A snapshot preserves trusted ontology identity,
source links, provenance, and bounded evidence excerpts. It is not canonical
knowledge and does not bundle every acquired source document.

Snapshots remain readable without the `geas` executable: read `SKILL.md`,
`references/index.md`, and the typed reference pages. Installing Geas later
enables deterministic retrieval and refresh; a snapshot never installs,
configures, or updates Geas itself.

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

An active profile must configure a trusted ontology Git URL/branch and the
ontology checkout must have a verified portable knowledge-projection artifact.
Export an accepted ontology by name:

```bash
geas skill-export research-agents --link
```

The user-scoped snapshot is `<config-root>/skills/research-agents/` and includes
`SKILL.md`, `geas-skill.json`, and `references/`. `--link` adds only exact
managed links for available agents. The JSON receipt reports the path,
`ontology_commit`, projection identity, `snapshot_sha256`, changed/unchanged
paths, and phase receipts. The same trusted Git and projection identities
produce byte-identical files and an `unchanged: true` receipt.

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
rendering. Once that trusted Geas boundary succeeds, update validates the skill
manifest, requires the active ontology profile URL/branch to match, fast-forwards
only the configured ontology remote, verifies the new artifact, then atomically
replaces the snapshot. Receipts expose completed phases and old/new software and
ontology commits. Source links and excerpts are provenance, not authority or a
guarantee that the linked source is safe, current, licensed, or complete.

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
