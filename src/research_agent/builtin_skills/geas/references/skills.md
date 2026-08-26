# Skill lifecycle

Use `geas skill-export`, `geas skill-update`, `geas skill-unlink`, and
`geas skill-remove` with `--help` to obtain the installed command syntax.
Exported ontology skills are portable projections with their own strict
manifest and evidence/provenance context; they are not canonical ontology
truth and do not bundle every acquired source document.

`config-init` installs or refreshes the packaged generic `geas` skill at
`<geas-config-root>/skills/geas`, then links it for detected agents. Codex and
OpenCode share `~/.agents/skills/geas`; Claude uses
`~/.claude/skills/geas`. The JSON `skills` receipt lists sorted installed,
updated, unchanged, linked, skipped, and conflicting paths. A manually managed
target is reported as a conflict and is not overwritten. Removing the managed
generic snapshot and running `config-init` installs it again.

Use `skill-unlink` to detach only exact managed agent links while retaining the
canonical snapshot. Use `skill-remove` only when the named managed snapshot is
also in scope. Do not remove a repository, worktree, or arbitrary skill
directory. Links and snapshot changes are validated and atomic; a missing Geas
installation is not permission to install it automatically.
