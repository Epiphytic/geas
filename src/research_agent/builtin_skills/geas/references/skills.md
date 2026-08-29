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
generic snapshot and running `config-init` installs it again. Non-fatal
post-commit transaction cleanup is reported separately in `cleanup_warnings`.

User-scoped ontology export links detected agents only when `--link` is passed.
Repository-scoped export with `--repo` always creates the applicable relative
managed links; it does not depend on `--link`.

`skill-update` is the only automatic Geas software-update boundary. It updates
an existing trusted installation; it never installs Geas when Geas is absent.
Supported provenance is a directory-backed `uv` tool receipt or an explicit Git
development invocation. The checkout must use the fixed Geas project URL and
`main` branch and must be clean. Update fetches and fast-forwards to the exact
fetched commit with hooks disabled, rechecks HEAD and clean bytes, reinstalls the
exact directory with `uv`, and reexecutes once with a bounded continuation
marker before ontology synchronization or rendering continues.

Remote mismatch, dirty or divergent history, fetch failure, post-merge changes,
installer failure, post-reexec version or provenance mismatch, unsupported
installer state, and a repeated continuation marker fail closed. Installation
instructions remain a separate explicit operator action in the Geas project
documentation.

Use `skill-unlink` to detach only exact managed agent links while retaining the
canonical snapshot. Use `skill-remove` only when the named managed snapshot is
also in scope. Do not remove a repository, worktree, or arbitrary skill
directory. Links and snapshot changes are validated and atomic; a missing Geas
installation is not permission to install it automatically.
