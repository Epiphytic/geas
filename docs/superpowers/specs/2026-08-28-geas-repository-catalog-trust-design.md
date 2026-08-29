# Git-connected ontology catalogs and trust design

Date: 2026-08-28

Status: approved in conversation; ready for implementation planning

## Summary

Geas will support ontology catalogs declared by strict `geas.yaml` files in
Git repositories. Repository-local catalogs augment the active user profile.
When Geas runs inside a worktree, it reads the deterministic ancestor chain
from the Git root through the current directory and merges those catalogs with
the innermost declaration taking precedence by ontology name.

Repository data remains untrusted until a separate user-configured trust rule
authorizes it. Trust can be scoped to a repository, an explicit Git ref or set
of refs, a directory, or an exact ontology bundle SHA-256. An invocation-only
global `--yolo` option authorizes all discovered repository ontologies for one
process without changing durable configuration. Integrity verification is
always mandatory and cannot be bypassed by trust or `--yolo`.

The maintained `ontology/open-source-research-agents` example will become the
first production catalog entry in the Geas repository. It will receive a new
bounded research pass, reviewed canonical updates, a compatible portable
projection artifact, README installation guidance, and deterministic CI
coverage of repository subscriptions and skill export.

Nostr signatures are intentionally outside this implementation. A future
signature envelope may sign the ontology bundle digest together with the
name, description, file inventory, author metadata, and timestamps.

## Goals

- Define one canonical repository catalog filename: `geas.yaml`.
- Allow a repository or repository subdirectory to declare one or more
  ontologies by explicit relative path and file inventory.
- Discover and cumulatively merge repository-local catalogs based on the
  current working directory.
- Augment, rather than replace, the active user profile.
- Support multiple named Git ontology subscriptions in one profile.
- Keep repository discovery separate from user authorization.
- Provide durable, deterministic trust scopes and an invocation-only
  `--yolo` override.
- Give every declared ontology a portable SHA-256 identity independent of its
  installation directory.
- Install an exact ontology snapshot without trusting future repository
  updates.
- Make the included research-agent ontology usable through the same public
  subscription and skill-export path as third-party ontologies.
- Test success, rejection, idempotence, update, removal, and interrupted-write
  behavior without depending on live network services.

## Non-goals

- Nostr or other author signatures.
- Treating SHA-256 alone as proof of authorship.
- Recursive searches for catalog files.
- Allowing repository catalogs to configure providers, credentials, policy,
  budgets, approval authority, or arbitrary commands.
- Letting trust bypass bundle verification, path confinement, threat policy,
  artifact compatibility, or Git promotion checks.
- Automatically promoting model proposals into accepted ontology knowledge.
- Running live research or external model calls in normal CI.

## Governing authority model

`geas.yaml`, ontology configuration, source cards, and accepted bundles are
Git-controlled data. They may become canonical ontology inputs only after both
of these independent gates pass:

1. **Integrity:** strict schema validation, confined path validation, exact
   file verification, and bundle-digest verification.
2. **Authorization:** a matching durable trust rule, an interactive decision,
   or the process-scoped `--yolo` override.

Trust is not integrity. A repository-wide allow does not accept a mismatched
digest. Integrity is not authorship. A digest proves that bytes match a
previously trusted value, not who created them.

The existing authority direction remains unchanged:

```text
trusted Git ontology and policy files
  -> validated immutable records and source blobs
  -> truth snapshot
  -> SQLite and Markdown projections
  -> exported skills and answers
```

Repository manifests never gain authority over user policies, secrets,
providers, endpoints, budgets, approvals, or workflow transitions.

## Repository catalog format

Every catalog is a strict, versioned YAML document named exactly
`geas.yaml`. JSON and alternate filenames are not supported.

Example:

```yaml
version: 1
ontologies:
  - name: open-source-research-agents
    description: Maintained comparison of open-source research agents.
    path: ontology/open-source-research-agents
    files:
      - path: build.yaml
        sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
        size_bytes: 1234
      - path: bundle.yaml
        sha256: 123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0
        size_bytes: 5678
      - path: library.yaml
        sha256: 23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01
        size_bytes: 901
    bundle_sha256: 3456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012
```

The schema rejects extra fields. Ontology names use the existing safe ontology
name grammar. `path` is relative to the directory containing `geas.yaml`.
Each file path is relative to the ontology directory.

Paths must be normalized, non-empty, relative, confined, unique, and free of
`..`. Catalogs, ontology directories, and listed files must not be symbolic
links. A listed file must be a regular file. Entries and file inventories must
be unique. File records must appear in ascending UTF-8 encoded path order.

Catalog entries use an explicit closed-world inventory. Geas does not add new
files recursively during verification or hash refresh. A repository-resident
file transitively read as an ontology input must be declared. This includes
build and library configuration, maintained bundles, source cards, threat
indexes, artifact manifests, and other canonical repository inputs. Runtime
stores, downloaded content, caches, model logs, projections, and generated
working files remain outside the configuration bundle unless explicitly
published through their existing artifact mechanism.

## Canonical ontology bundle digest

For each declared file, Geas verifies `size_bytes` and the SHA-256 of its exact
bytes. It then serializes this value with the existing canonical JSON helper:

```json
{
  "description": "Maintained comparison of open-source research agents.",
  "files": [
    {
      "path": "build.yaml",
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "size_bytes": 1234
    }
  ],
  "format": "geas-ontology-bundle/1",
  "name": "open-source-research-agents"
}
```

`bundle_sha256` is the lowercase hexadecimal SHA-256 of those canonical JSON
bytes. The catalog location and ontology directory prefix are excluded so an
exact bundle retains its identity when copied into a managed local snapshot.
The `bundle_sha256` field is excluded to avoid self-reference.

Changing the name, description, relative file path, file size, or file bytes
changes the bundle identity. Reordering identical file records does not create
a different identity because validation requires canonical ordering.

Geas will provide deterministic catalog verification and hash-refresh
commands. Refresh updates hashes and sizes only for files already declared in
the catalog. Adding a new authoritative file requires an explicit manifest
edit before refresh.

## Local repository discovery

When no explicit ontology directory or catalog source overrides discovery,
Geas determines whether the current working directory is inside a Git
worktree. If it is, Geas resolves the Git root and constructs the direct
ancestor chain from that root through the current directory.

For example, from `/repo/services/api`, Geas examines only:

```text
/repo/geas.yaml
/repo/services/geas.yaml
/repo/services/api/geas.yaml
```

It does not recurse into siblings or descendants and does not search above the
Git root. Every file found in the chain is parsed and validated. A malformed
catalog fails closed; an inner declaration cannot hide an invalid outer
catalog.

Catalogs merge from outermost to innermost by ontology name. An inner entry
replaces the complete same-named outer entry; fields are not partially merged.
Paths remain relative to the catalog that declared the winning entry.

The resolved repository catalog augments the selected profile catalog.
Repository results include their source manifest, Git repository identity,
active ref, commit, bundle digest, and trust status. A same-name collision
between the resolved repository catalog and the selected profile is reported
as an explicit ambiguity. Geas does not silently let a checkout shadow trusted
profile data.

`geas list` becomes the concise listing command. `geas ontology-list` remains
as a compatibility alias.

## Git repository identity and refs

For a repository with `origin`, durable trust uses the normalized origin URL.
Changing the URL invalidates that trust. For a repository without `origin`,
Geas may use the resolved local Git root as a machine-local identity and must
label it accordingly.

Trust rules and catalog receipts use generic Git refs rather than only branch
names. A ref set may contain full branch refs, full tag refs, or exact commit
object IDs. User-facing shorthand is normalized before persistence. A
subscription materializes one active ref at a time even when a trust rule
allows multiple refs.

Ref-scoped trust authorizes committed bytes represented by the resolved Git
object. A dirty declared file is not covered merely because the checkout is on
an allowed branch. It requires exact-digest trust, a broader path/repository
rule that explicitly allows changing content, or a new interactive decision.

## Trust configuration and evaluation

Trust rules live in the OS-standard trusted Geas user configuration, never in
the repository being evaluated. Each strict rule contains:

- an allow or deny decision;
- normalized repository identity;
- refs: `*` or an explicit non-empty set;
- paths: `*` or an explicit non-empty set of repository-relative ontology
  directories;
- bundle digests: `*` or an explicit non-empty set of SHA-256 values; and
- deterministic audit metadata describing how and when the user created the
  rule, without source excerpts or secrets.

Rules match only within the same repository identity. Specificity is ordered
deterministically by exact digest, exact path, then exact ref. This is
equivalent to the bit score `digest=4`, `path=2`, `ref=1`; wildcard dimensions
score zero. The highest-scoring matching rule wins. An allow and deny with the
same score resolve to deny. Configuration validation rejects duplicate rules
whose effective selectors and decision conflict ambiguously.

This single representation supports:

- whole-repository trust: wildcard refs, paths, and digests;
- ref or ref-set trust: exact refs, wildcard paths and digests;
- directory trust: exact paths with optionally exact refs;
- commit trust: an exact commit object ID in refs; and
- immutable ontology trust: an exact bundle digest, optionally further scoped
  by repository, path, and ref.

## Interactive trust flow

Geas may read an untrusted catalog sufficiently to display inert names, paths,
digests, and repository metadata. It does not parse or operate on the declared
ontology configuration before authorization.

When no rule resolves trust and an interactive terminal is available, Geas
states that repository ontologies were discovered and offers:

1. **Trust completely.** Persist a repository-wide wildcard allow.
2. **Trust selectively.** Ask about each ontology and persist the exact
   path/ref/digest scopes selected by the user.
3. **Install snapshots.** Let the user choose ontologies, copy each verified
   explicit inventory into the managed snapshot store, register the immutable
   digest in the profile, and persist a denial for the source repository/ref
   context.
4. **No.** Persist a denial for the repository/ref context.

Choices 3 and 4 therefore both record that the repository itself is not
trusted. Choice 3 separately trusts only the copied, verified versions.

Prompts and explanatory text use stderr or the controlling terminal so stdout
remains machine-readable JSON. Prompt I/O is injectable for deterministic
tests.

When no interactive terminal is available, unresolved trust fails closed.
`geas list` may return inert candidates with `trust_status: untrusted`, but
operational commands return a non-zero actionable error.

## Invocation-only `--yolo`

`--yolo` is a global CLI option. It supplies an in-memory repository-wide
allow for all repository ontologies encountered by that process. It never
writes configuration, never installs a snapshot, and never survives into a
child Geas invocation.

`--yolo` bypasses only the trust prompt. It does not bypass strict schema
validation, bundle hashes, path and symlink checks, Git ref verification,
artifact hashes and stamps, source policy, model policy, budgets, approvals,
or promotion rules.

## Installed ontology snapshots

Installed snapshots live under a dedicated managed user-config directory,
separate from mutable subscription checkouts and runtime data. The destination
includes the ontology name and bundle digest so different immutable versions
cannot overwrite one another.

Installation performs these steps transactionally:

1. validate the catalog and trust decision;
2. verify every declared file and the bundle digest;
3. copy only the declared files to a temporary confined directory;
4. re-verify the completed temporary snapshot;
5. atomically move it into the managed store; and
6. atomically register the snapshot in user configuration.

An existing identical snapshot is unchanged. A name collision with a different
digest is retained as a distinct version and requires explicit selection if
both are active. Removal targets one exact managed snapshot and never follows
symlinks or recursively deletes a broad user directory.

Snapshots do not update from the source repository. Installing a newer version
is a new explicit operation.

## Multiple subscriptions per profile

Profiles gain a map of named ontology subscriptions. Each subscription records:

- repository URL;
- one active ref;
- a confined checkout directory under the configuration root;
- a confined catalog path, default `geas.yaml`;
- remote name; and
- pull, push, and freshness behavior.

The checkout directory is the Git repository root. The catalog may be at the
root or a configured subdirectory. Synchronization always operates on the
checkout root, while discovery begins at the configured catalog.

Existing profiles using `ontology_directory`, `ontology_git`, and
`ontology_git.branch` remain readable. The loader normalizes that data into a
primary in-memory subscription whose active ref is the corresponding full
branch ref. `config-init` may write the explicit new representation only
through the existing atomic configuration update path; it does not move or
delete the existing checkout.

Subscription pushes are allowed only for writable branch refs. Tag and commit
subscriptions are read-only. Pull/fetch must resolve the configured ref to an
exact object ID, integrate only by the existing fail-closed rules, and verify
that checkout `HEAD` matches the fetched commit.

## CLI changes

Add these commands while retaining current compatibility aliases:

```text
geas list [DIRECTORY]
geas catalog-verify [CATALOG]
geas catalog-refresh [CATALOG] [ONTOLOGY [ONTOLOGY]]
geas ontology-subscribe NAME URL [--ref REF] [--catalog PATH]
geas ontology-unsubscribe NAME [--remove-checkout]
geas ontology-sync [NAME [NAME]] [--pull] [--push]
```

`ontology-subscribe` validates the name, URL, ref, checkout destination, and
catalog path before writes. It atomically records the subscription,
synchronizes the checkout, verifies the catalog, and invokes the normal trust
flow. A failure restores the previous configuration and removes only a newly
created, exact temporary checkout.

`ontology-unsubscribe` removes configuration but preserves its checkout by
default. `--remove-checkout` removes only the exact clean managed checkout
after verifying its recorded identity; dirty or mismatched checkouts are
preserved with an actionable error.

`ontology-sync` accepts zero or more subscription names. With no names, it
synchronizes every configured subscription in sorted-name order and returns a
separate receipt for each. Existing single-repository invocations retain their
behavior through the normalized primary subscription.

Named ontology operations resolve a catalog entry rather than assuming
`root / name`. Artifact hydration and publishing use the subscription that
declared the selected ontology. Skill export and update record the repository
URL, active ref, exact commit, catalog path, ontology path, bundle digest, and
portable artifact identity.

## Maintained sample conversion

Add a root `geas.yaml` to the Geas repository containing
`open-source-research-agents` at
`ontology/open-source-research-agents`. Its explicit inventory includes every
maintained configuration, accepted bundle, source card, threat index,
evaluation note, artifact manifest, and other repository input required by
the sample. Disposable runtime and cache files remain excluded.

Update sample paths and command resolution where needed so the ontology works
from:

- the Geas development checkout;
- a named subscription checkout;
- an installed immutable snapshot where supported; and
- exported-skill generation using verified portable artifacts.

The sample receives a new bounded research pass with a maximum normal worker
duration of 30 minutes. Discovery and acquisition use current official primary
sources and configured storage-rights policy. Model output remains
proposal-only. Accepted updates require review, exact evidence, maintained
source-card hash updates, and Git-mediated promotion where applicable.

If the current archiving behavior cannot reproduce the intended source
material, the implementation may inspect
`openclawl@192.168.128.149` read-only to compare the original runtime and
diagnose the archival process. It must not copy private runtime databases,
credentials, logs, or source material into Git.

After review, rebuild the truth snapshot and knowledge projection using the
current Geas projection schema. Publish compatible content-addressed portable
artifacts to the Geas repository's GitHub releases and commit the verified
artifact manifest. A skill export from a fresh subscription must succeed
without treating the SQLite projection as canonical truth.

## README workflow

Add a short sample-install section to the repository README:

```bash
geas ontology-subscribe geas-samples https://github.com/Epiphytic/geas.git \
  --ref refs/heads/main
geas list
geas skill-export open-source-research-agents --link
```

The prose explains that the first command states that it is adding and
synchronizing a subscription, then asks for trust by default. It explains
`--yolo` as invocation-only and links to the detailed trust and subscription
documentation. It also shows unsubscribe and installed-snapshot removal.

## Error handling and transaction boundaries

All normal result receipts remain JSON on stdout. Progress, prompts, and
diagnostics remain on stderr. Exceptions and logs must not expose source
excerpts, secrets, raw model responses, or credentials.

The following fail before ontology configuration is parsed or operational
work begins:

- invalid or ambiguous repository identity;
- invalid Git ref or unexpected resolved commit;
- malformed catalog;
- unsafe or symlinked paths;
- missing or changed declared files;
- bundle digest mismatch;
- unresolved trust in non-interactive mode; and
- conflicting ontology names without explicit selection.

No model, connector, artifact download, policy mutation, canonical write, or
skill export occurs after those failures.

Trust-config changes, subscription changes, snapshot installation, and catalog
hash refresh use atomic replacement. Interrupted operations preserve the prior
valid state. Git synchronization retains its existing lock, clean-worktree,
remote identity, branch/ref, and fast-forward checks.

## Deterministic tests

Unit tests cover:

- strict manifest success and extra-field rejection;
- exact canonical bundle digest vectors;
- ordering normalization and metadata sensitivity;
- absolute paths, traversal, duplicate paths, control characters, symlinks,
  missing files, size mismatch, content mismatch, and bundle mismatch;
- rejection when repository-resident transitive inputs are undeclared;
- Git-root-to-current-directory discovery without recursive scanning;
- cumulative merge and complete innermost replacement;
- repository/profile name ambiguity;
- remote and machine-local repository identity;
- generic branch, tag, commit, and explicit ref-set matching;
- repository, directory, ref, commit, and exact-digest trust;
- specificity ordering and deny-on-tie behavior;
- dirty declared files under ref-scoped trust;
- all four interactive choices through injected prompt I/O;
- non-interactive rejection;
- process-only `--yolo` and proof that configuration bytes do not change;
- proof that trust and `--yolo` cannot bypass integrity failure;
- snapshot install, idempotence, version coexistence, rollback, and exact
  removal;
- subscription add, sync, ref switching, update, unsubscribe, and safe
  checkout preservation;
- backward-compatible legacy profile normalization;
- multi-subscription ordering and partial failure receipts; and
- skill export/update provenance containing the bundle digest.

Tests use temporary roots, fake clocks, injected prompt I/O, fake Git
transports where network behavior is under test, and deterministic Git author
identity. Rejection tests assert that forbidden writes, network requests,
model calls, artifact hydration, and canonical operations did not occur.

## CI integration

Add an offline repository-subscription integration job to GitHub Actions. It:

1. creates a temporary Git checkout from the current workflow checkout;
2. assigns the expected public origin URL without fetching it;
3. creates an isolated Geas user configuration with network freshness checks
   disabled for the fixture;
4. registers the checkout as a named subscription;
5. exercises durable trust and process-only `--yolo` separately;
6. runs `geas list` from the root and nested directories;
7. verifies the root catalog and maintained sample bundle;
8. runs the maintained deterministic demo and rebuilds the projection;
9. exports and validates a sample skill using a preseeded verified artifact;
10. repeats verification, listing, sync-without-fetch, and export to prove
    idempotence; and
11. uninstalls the subscription and installed snapshot through exact managed
    paths.

The job uses the production catalog, trust, resolution, projection, and skill
paths. Initial remote clone/fetch behavior is covered with injected offline
Git fixtures rather than live GitHub. Normal tests remain network-free and do
not invoke external models.

## Security invariants

- Repository text and model text remain untrusted data, never instructions.
- A catalog can name ontology files but cannot authorize itself.
- SHA-256 values in an untrusted catalog gain authority only after comparison
  with trusted configuration or an explicit user decision.
- Trust never bypasses integrity or policy.
- Exact-digest snapshots never update implicitly.
- Ref trust does not silently cover dirty declared files.
- Symlinks and path traversal never cross repository, snapshot, config, or
  skill boundaries.
- New repository files never enter a bundle through implicit recursion.
- Model proposals retain zero commit authority.
- SQLite and generated Markdown remain rebuildable projections.
- Dissent, gaps, threat context, evidence selectors, and provenance remain
  queryable after sample refresh and skill export.

## Future Nostr signing compatibility

The bundle digest and explicit inventory provide the stable content identity
for a future Nostr signature feature. That feature may define a separately
versioned signed envelope over:

- bundle SHA-256;
- ontology name and description;
- exact file inventory;
- repository and ref metadata;
- author public key and claimed identity metadata; and
- issuance, validity, and revocation information.

No signature fields or implied author authenticity are added in this phase.
