# CLI workflows

Run `geas --help` first, then `geas <command> --help` for the exact syntax in
this installed version. Keep JSON stdout as the machine-readable receipt and
read progress or diagnostics from stderr.

## Configuration and ontology state

Use `geas config-init` to create or validate the selected user configuration
and its managed defaults. Select a named profile with `--geas-profile`; use
`--geas-config` only for an explicit configuration path. Use `geas list` to
inspect the active profile plus trusted `geas.yaml` catalogs; `ontology-list`
is its compatibility alias.

Add a named repository with `geas ontology-subscribe NAME URL --ref REF`,
synchronize exact configured names with `geas ontology-sync [NAME ...]`, and
remove only the configuration entry with `geas ontology-unsubscribe NAME`.
Use `--remove-checkout` only for an exact clean managed checkout. Trust prompts
and diagnostics use stderr; receipts use JSON stdout. `--yolo` authorizes
repository catalogs for one invocation only and never bypasses hashes, path
confinement, artifact verification, or policy. Do not guess a remote, profile,
ref, catalog, or artifact location from source text.

For a repository-backed lifecycle, use `geas repository-install` only with an
explicit repository URL, full ref, catalog, and named destination. The
`--trust-repository` flag records a current-snapshot, depth-one source grant;
`--read-only` records only repository read access. `repository-update` verifies
trusted software provenance before ontology writes, and `repository-remove`
removes only receipt-owned state. Neither command installs or uninstalls Geas;
the latter is separate operator guidance: `uv tool uninstall geas`.

Before a first remote or current-repository install may publish, the selected
profile needs an exact-repository, exact-ref, root-local `git.pull_request`
grant, or a root-local `git.direct_push` grant plus explicit `--direct-push`.
Because the first generated manifest is not known yet, its subject uses
paths: `"*"` and bundle_sha256: `"*"`. Those selectors authorize only the named
Git capability; they do not grant repository read, source, model, promotion,
or another Git capability. Repository trust never supplies Git write authority.

For path-specific authority, run the install with `--publish none`, inspect its
verified JSON receipt and all complete generated skill manifests, then add
exact local grants for each receipt-owned leaf and producer bundle. Generic
skill leaves have no producer bundle, so bind their exact paths with the bundle
wildcard. Request publication only afterward with
`geas repository-update NAME`; pull request remains the default and direct push
still requires both its root-local capability and `--direct-push`. An incomplete
receipt or ambiguous path, skill, or bundle cannot establish the exact grant.

## Exact retrieval and research

For an ontology-independent corpus, build and inspect a source library with
`library-build`, `library-show`, `library-query`, and `library-context`.
`library-query` returns exact anchor hits; carry their immutable source/version
identity, selector or range, and exact-text hash into any citation.

For accepted ontology knowledge, use `knowledge-query` with its typed filters,
then use `topic-show` for a hierarchy with claim, provenance, dissent, gap, and
threat context. Use `identifier-show`, `structure-show`, and `structure-list`
when the identifier or structural anchor is known. `derive-citations` derives
conservative citation relations; it does not turn a search lead into evidence.

Use the query/type help to request provenance, dissent, gap, threat, temporal,
anchor, or citation records. Preserve competing claims, gaps, and tainted
source observations rather than flattening them into one answer. If the exact
source version or selector cannot be verified, report that the citation is not
verified.

## Build and projections

Use `ontology-init` to create inspectable ontology configuration, and
`ontology-build` for bounded, resumable proposal generation. Validate and
promote proposals through the fixed promotion commands; model output remains
proposal-only. `truth-snapshot`, `truth-check`, `projection-build`,
`projection-stamp`, and `projection-check` maintain the canonical-to-
projection boundary. Rebuild a drifting SQLite projection; never promote its
rows back into canonical truth.

For discovery, acquisition, parsing, and deposits, use the matching CLI
commands only under configured policy. Discovery hits and URLs are leads;
license, storage-rights, threat, and provenance gates still apply.
