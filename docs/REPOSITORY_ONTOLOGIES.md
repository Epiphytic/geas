# Repository-backed ontologies

Geas can use ontology configuration and accepted knowledge stored in Git. A
strict `geas.yaml` catalog identifies the ontology directories and pins every
repository file Geas may read. Catalog integrity and repository trust are
separate: matching hashes prove which bytes were selected, not who authored
them or whether they should be trusted.

Use one of three modes:

| Mode | Best for | Update behavior |
|---|---|---|
| Repository-local catalog | A project that carries its own ontologies | Geas reads the checked-out files after verifying and authorizing them |
| Named subscription | Using an ontology repository from any directory | Geas maintains a checkout under its user configuration root and synchronizes its exact configured Git ref |
| Immutable snapshot | Keeping one reviewed version without trusting future repository changes | Geas copies only the verified inventory; snapshots never update in place |

Repository catalogs augment the active user profile. They do not replace it,
and a same-name repository/profile collision fails as an ambiguity.

## Use a catalog from the current repository

From anywhere inside a Git worktree containing an applicable `geas.yaml`, run:

```bash
geas list
```

Geas discovers catalogs only on the direct path from the Git worktree root to
the current directory. For `/repo/services/api`, it considers, in order:

```text
/repo/geas.yaml
/repo/services/geas.yaml
/repo/services/api/geas.yaml
```

It does not search parents above the Git root, siblings, or descendants. The
catalogs merge from outermost to innermost. An inner same-name declaration
replaces the complete outer declaration; individual fields are not merged, and
paths stay relative to the `geas.yaml` that declared them. Pass a directory to
inspect the catalog chain that applies there:

```bash
geas list services/api
```

The compatibility alias `geas ontology-list` remains available. Prefer
`geas list` in new scripts.

## Subscribe to a catalog repository

Use a named subscription when the ontology is not in the current worktree:

```bash
geas ontology-subscribe geas-samples https://github.com/Epiphytic/geas.git \
  --ref refs/heads/main
geas list
```

`ontology-subscribe` announces the operation on stderr, checks out the exact
ref, verifies the catalog and its closed inventory, asks for trust when needed,
and records the subscription only if every step succeeds. The checkout lives
under the OS-standard Geas configuration root; use the JSON receipt for its
exact path rather than assuming a Linux location.

`--ref` accepts a full branch ref, a full tag ref, or an exact 40- or
64-character lowercase hexadecimal commit object ID. The default is
`refs/heads/main`. Use `--catalog` when the repository's applicable catalog is
below its root:

```bash
geas ontology-subscribe service-catalog git@github.com:example/ontologies.git \
  --ref refs/tags/v1.2.0 \
  --catalog services/geas.yaml
```

URLs must be credential-free HTTPS or Git SSH remotes. Tag and commit
subscriptions are read-only.

## Author `geas.yaml`

The filename is exactly `geas.yaml`; alternate YAML, JSON, and hidden filenames
are not discovered. Each catalog uses this strict shape:

```yaml
version: 1
ontologies:
  - name: example
    description: Maintained knowledge about the example domain.
    path: ontology/example
    files:
      - path: build.yaml
        sha256: 0000000000000000000000000000000000000000000000000000000000000000
        size_bytes: 0
      - path: bundle.yaml
        sha256: 0000000000000000000000000000000000000000000000000000000000000000
        size_bytes: 0
      - path: library.yaml
        sha256: 0000000000000000000000000000000000000000000000000000000000000000
        size_bytes: 0
    bundle_sha256: 0000000000000000000000000000000000000000000000000000000000000000
```

The ontology `path` is relative to the directory containing the catalog. Each
file path is relative to that ontology directory. Declare every transitive
repository input Geas may read, including build and library configuration,
accepted bundles, source cards, threat indexes, artifact manifests, and other
canonical inputs. Keep the file records in ascending UTF-8 path order.
Runtime stores, downloaded documents, SQLite projections, caches, checkpoints,
and model logs do not belong in this inventory.

The zero values above are an authoring scaffold, not a valid verified catalog.
After declaring the complete file list, calculate each size and digest plus the
portable bundle digest deterministically:

```bash
geas catalog-refresh geas.yaml example
geas catalog-verify geas.yaml
```

With no ontology names, `catalog-refresh` refreshes every declaration. It
updates hashes and sizes only for files already listed; it never recursively
adds files. Add a new authoritative file to the inventory explicitly before
refreshing. `catalog-verify` is read-only and fails on missing, extra-field,
out-of-order, unsafe, symlinked, transitively undeclared, or hash-mismatched
inputs.

Commit the ontology files and refreshed catalog together. A dirty declared file
still has a verified content digest, but branch/ref-only trust does not cover
uncommitted bytes.

## Choose repository trust

An untrusted catalog is inert until a trusted user decision authorizes it.
`geas list` is read-only: it reports the candidate as inert without prompting.
When a subscription or operational ontology command needs an unresolved
catalog in an interactive terminal, Geas offers:

1. **Trust completely** — persist a wildcard allow for the repository.
2. **Trust selectively** — ask about each ontology and persist its exact
   repository path, ref, and bundle digest decision.
3. **Install immutable snapshots** — copy selected verified inventories and
   deny the mutable source repository for the current ref context.
4. **No** — deny the source repository for the current ref context.

Choices 3 and 4 both record a repository denial. Choice 3 separately registers
the copied version by ontology name and exact bundle SHA-256. In a
non-interactive process, unresolved trust fails closed; `geas list` can still
report inert candidates.

The global option `--yolo` answers only the repository trust gate for one Geas
invocation:

```bash
geas --yolo list
```

It never persists trust, installs snapshots, or weakens catalog hashes, path
confinement, Git identity checks, artifact verification, source/model policy,
budgets, approvals, or promotion rules.

Durable trust rules live only in the selected profile's trusted `config.yaml`.
Interactive choices write them automatically. Operators may also add strict
manual rules with `decision`, normalized `repository`, `refs`, `paths`,
`bundle_sha256`, timezone-aware `created_at`, and `created_via: manual`.
Selectors are either `"*"` or a sorted non-empty list. Exact digest is more
specific than exact path, which is more specific than exact ref; deny wins an
equal-specificity conflict.

For example, this manually trusts one ontology directory in one branch context
while still requiring every catalog digest to verify:

```yaml
profiles:
  default:
    trust_rules:
      - decision: allow
        repository: https://github.com/Epiphytic/geas
        refs:
          - refs/heads/main
        paths:
          - ontology/open-source-research-agents
        bundle_sha256: "*"
        created_at: 2026-09-01T00:00:00Z
        created_via: manual
```

Use an exact 64-character lowercase digest list instead of `"*"` to pin one
immutable ontology bundle. Preserve the other profile fields already present;
the excerpt above is not a complete `config.yaml`.

## Use and update a subscribed ontology

After authorization, `geas list` reports each candidate's source, catalog,
repository identity, ref, commit, ontology path, bundle digest, and trust
status. Commands that accept an ontology name resolve that verified selection.
A repository-local catalog can drive those ontology operations directly.

Portable skill export additionally requires a declaring named subscription (or
the legacy profile Git repository) so the update chain has a trusted remote and
exact ref. After subscribing to the maintained sample, export it as a portable
agent skill:

```bash
geas skill-export open-source-research-agents --link
```

Skill export requires the ontology's verified knowledge-projection artifact.
The skill is a deterministic snapshot containing bounded accepted evidence and
source links, not every acquired source document. Its manifest records the
repository URL, ref, exact commit, catalog and ontology paths, bundle digest,
and artifact identity so `geas skill-update` can revalidate the chain.

Synchronize selected subscriptions:

```bash
geas ontology-sync geas-samples --pull
```

With no names, `ontology-sync` processes every selected-profile subscription
in sorted order. A sync with neither `--pull` nor `--push` pulls by default.
Push is available only for writable branch refs and uses the existing confined
staging, secret scan, and fast-forward protections. The current parser exposes
`--message`, but the subscription service does not yet forward it; pushed
commits therefore use the default `geas: update ontologies` message. Do not rely
on a custom message until that implementation gap is closed. Named operations
also use the configured freshness window, which defaults to a remote check at
most once per hour.

## Remove subscriptions, snapshots, and skills

Remove only the subscription declaration while preserving its checkout:

```bash
geas ontology-unsubscribe geas-samples
```

Request checkout removal only when Geas can prove the exact managed checkout
is clean and still has the recorded identity:

```bash
geas ontology-unsubscribe geas-samples --remove-checkout
```

Remove an installed immutable ontology snapshot using the exact name and digest
from its installation receipt:

```bash
geas ontology-snapshot-remove open-source-research-agents BUNDLE_SHA256
```

This is separate from `geas skill-unlink PATH`, which keeps an exported skill
snapshot, and `geas skill-remove PATH`, which removes its exact managed links
and snapshot. Removing a subscription never implicitly removes either kind of
snapshot.

## Command reference

These concrete examples are checked against the current CLI parser in the test
suite. Global options such as `--geas-profile` and `--yolo` precede the command.

<!-- CLI_REFERENCE_START -->
```console
$ geas list
$ geas list services/api
$ geas catalog-refresh geas.yaml
$ geas catalog-refresh geas.yaml open-source-research-agents
$ geas catalog-verify geas.yaml
$ geas ontology-subscribe geas-samples https://github.com/Epiphytic/geas.git --ref refs/heads/main
$ geas ontology-subscribe service-catalog git@github.com:example/ontologies.git --ref refs/tags/v1.2.0 --catalog services/geas.yaml
$ geas ontology-sync
$ geas ontology-sync geas-samples --pull
$ geas ontology-sync geas-samples --push
$ geas --yolo list
$ geas skill-export open-source-research-agents --link
$ geas skill-export open-source-research-agents --name research-agents --repo /srv/project --force
$ geas ontology-unsubscribe geas-samples
$ geas ontology-unsubscribe geas-samples --remove-checkout
$ geas ontology-snapshot-remove open-source-research-agents 0000000000000000000000000000000000000000000000000000000000000000
$ geas topic-export concept:open-source-research-agents generated/research-agents.ttl --database open-source-research-agents --format turtle
```
<!-- CLI_REFERENCE_END -->

Use `geas --help` and `geas COMMAND --help` as the authoritative option list for
the installed version. JSON receipts go to stdout; progress, trust prompts, and
diagnostics go to stderr.

## Security and authority boundary

A repository catalog may identify ontology inputs only. It cannot configure
providers, endpoints, credentials, secrets, policies, budgets, approvals,
commands, workflow transitions, or canonical writes. Retrieved source text and
metadata remain untrusted data. Catalogs also do not recursively acquire
repository trees or download every externally linked reference document.

Git ontology and policy files remain canonical; immutable records and source
blobs derive from them, followed by truth snapshots, SQLite/Markdown/RDF
projections, exported skills, and answers. Never write a later projection back
into canonical ontology state automatically.
