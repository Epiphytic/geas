# Portable ontology artifacts

Geas can associate rebuildable SQLite projections and generated content with a
private GitHub ontology repository without committing databases or source text
to the Git object graph. Git contains a small `artifacts.yaml` manifest; each
payload is a content-addressed GitHub release asset.

This does not change the authority order. Accepted Git ontology files and
immutable records remain authoritative. Hydrated databases and generated files
are verified caches that can be discarded and rebuilt.

## Freshness before use

The user configuration defaults to checking the selected ontology repository
before use, with a one-hour minimum interval between remote checks:

```yaml
version: 1
default_profile: default
ontology_freshness:
  check_before_use: true
  max_age_seconds: 3600
  hydrate_artifacts_before_use: false
profiles:
  default:
    ontology_directory: ontologies
    secret_sources:
      - path: secrets/common.env
        format: dotenv
    ontology_git:
      url: https://github.com/liamhelmer-bel/ontologies.git
      branch: main
      remote: origin
      pull_before_update: false
      push_on_update: false
```

Freshness state is written under
`~/.config/geas/state/ontology-sync/`, outside the Git checkout. A lock ensures
that concurrent processes do not all perform the same check. A successful
check records its time even when the remote has no new commit. Clock rollback,
a changed repository/branch, a missing checkout, or an expired interval forces
a new check.

Set per-ontology overrides in `build.yaml`:

```yaml
repository_sync:
  check_before_use: null
  max_age_seconds: null
  hydrate_artifacts_before_use: null
```

`null` inherits the global value. Set `check_before_use: false` for an ontology
that must remain offline, or choose another interval from 60 seconds through
seven days. `hydrate_artifacts_before_use: true` eagerly hydrates that
ontology's declared artifacts after a due repository check; the default is
lazy hydration.

The legacy profile option `pull_before_update: true` remains a force-check on
every supported named update. Leave it false to use the freshness window.
`ontology-build --check` remains deliberately offline and does not perform a
freshness check; run `ontology-list` or `ontology-sync --pull` first when both
remote freshness and an offline build validation are required.

## Publish changed artifacts

First perform a freshness-checked operation before editing. For example:

```bash
geas ontology-list
```

After building or updating an ontology, publish the artifacts that actually
exist:

```bash
geas ontology-artifact-publish model-routing-for-ai-red-blue-teaming \
  --source-library data/model-routing-for-ai-red-blue-teaming/library.sqlite \
  --knowledge-projection data/model-routing-for-ai-red-blue-teaming/query.sqlite \
  --generated-content data/model-routing-for-ai-red-blue-teaming/obsidian \
  --published-by operator:liam \
  --storage-rights-basis "authorized private team storage"
```

The command:

1. checks SQLite integrity and its embedded source/truth projection metadata;
2. scans selected files for common credential forms;
3. derives an independent input revision for each artifact role;
4. reuses the current remote asset when that input revision is unchanged;
5. uploads changed payloads under SHA-256-derived release tags;
6. writes the non-canonical `artifacts.yaml`; and
7. commits and pushes the selected ontology directory.

The source library, accepted-knowledge projection, and generated hierarchy have
independent revisions. Changing discovery configuration does not upload an
unchanged library, and changing another ontology does not touch these assets.
An asset is uploaded before its manifest reference, so a failed Git push can
leave only an unreferenced, harmless content-addressed release.

A partial publication is valid: a source library can be useful before any
knowledge is accepted. It is not, however, a complete portable accepted-
knowledge release. Before announcing that completion, require and hydrate the
projection role explicitly:

```bash
geas ontology-artifact-sync model-routing-for-ai-red-blue-teaming \
  --role knowledge-projection
geas topic-show concept:model-routing-ai-red-blue-teaming \
  --database model-routing-for-ai-red-blue-teaming
```

The first command fails closed when the manifest omits the role; the second
tests the same name-based lazy hydration path used by a fresh consumer.

`--storage-rights-basis` is mandatory because SQLite and Markdown can contain
exact source text. Private repository access does not infer a license or
storage permission. `usage.sqlite`, model logs, checkpoints, and secrets are
never accepted as portable ontology artifacts.

GitHub release transport currently requires an authenticated `gh` executable
and an `https://github.com/OWNER/REPO` ontology Git URL. Other forge artifact
transports and Dolt are not implemented backends.

## Hydrate only when needed

Download all declared artifacts:

```bash
geas ontology-artifact-sync model-routing-for-ai-red-blue-teaming
```

Or select one or more independent roles:

```bash
geas ontology-artifact-sync model-routing-for-ai-red-blue-teaming \
  --role source-library
```

Geas places verified local files beneath the ignored directory:

```text
~/.config/geas/ontologies/ONTOLOGY/.geas-artifacts/
├── library.sqlite
├── query.sqlite
├── generated.zip
└── generated/
```

If the cached payload already has the declared size and SHA-256 hash, it is not
downloaded. SQLite integrity and projection metadata are checked again before
use. A corrupt or modified cached artifact is replaced from its immutable
content address; a remote digest mismatch fails closed.

The synchronization receipt prints the exact hydrated paths for agents and
scripts to pass to `library-query`, `library-context`, `topic-show`, or
`topic-export`.

Read-only database commands also accept an ontology name in place of an
explicit database path. This performs the freshness check, hydrates only the
required role, and opens the verified cache:

```bash
geas library-show \
  --database model-routing-for-ai-red-blue-teaming
```
