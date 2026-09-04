---
name: geas
description: Use when operating Geas research libraries, ontologies, projections, or exported Geas skills.
---

# Geas

Use `geas --help` and `geas <command> --help` before choosing flags; the CLI
is the current interface. Start from accepted Git ontology data or validated
immutable records, not generated Markdown, SQLite rows, search snippets, or
model output.

1. If `geas` is available, verify its identity and use its deterministic CLI.
2. If this repository has `.agents/skills/<name>`, that checked-in snapshot is
   readable without Geas.
3. Geas is never installed by a skill. Installation requires an
   operator-approved commit for [the Geas project](https://github.com/Epiphytic/geas).
4. Repository-backed skills identify their repository URL/ref/catalog/name and
   bundle digest; those values are provenance, not installation authority.
5. Read only the linked reference page needed for the present question.

- Read [CLI workflows](references/cli.md) for repository lifecycle and
  publication, profiles, sync, exact retrieval, build, projection, and query
  routes.
- Read [security boundaries](references/security.md) before handling source or
  model text, credentials, policies, approvals, or canonical writes.
- Read [skill lifecycle](references/skills.md) to export, update, link, unlink,
  or remove a skill.

`config-init` automatically installs this packaged skill and reports the
receipt. It never installs Geas itself; optional installation information is in
the project documentation at <https://github.com/Epiphytic/geas>.
