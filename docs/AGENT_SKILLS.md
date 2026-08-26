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
preserved and reported as a conflict.

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
geas skill-export research-agents --repo /path/to/project --link
```

That writes `/path/to/project/.agents/skills/research-agents`. The additions are
ordinary uncommitted Git changes for review; commit them only if the project
wants to share the snapshot, or add the path to its ignore rules. Geas never
commits it automatically.

Refresh an existing managed snapshot with:

```bash
geas skill-update /path/to/skills/research-agents
```

Update validates the manifest, requires the active profile URL/branch to match,
fast-forwards only the configured remote, verifies the new artifact, then
atomically replaces the snapshot. Receipts expose completed phases and old/new
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
