# Open-source research agents ontology

This directory is a maintained, executable example of the repository's durable
research product. It compares open-source research agents and adjacent
knowledge-curation systems without treating a generated report as canonical.

`bundle.yaml` and the two accepted `generated/*/bundle.yaml` seeds declared by
`build.yaml` form the reviewed ontology. `sources/` contains concise
project-authored source cards; the generated seeds contain license-recorded
extracts pinned to official repository commits. Each source is SHA-256 pinned,
parsed as inert data, structurally anchored, citation-indexed, and linked to
exact claim evidence. Documented public placeholder assignments remain exact
upstream bytes and pass a narrow non-secret scanner exception. The synthetic
poisoned source is deliberately inspected but is never used as claim evidence.

## Install the catalog sample

With Geas installed, add the repository as a named ontology subscription and
export its verified portable skill:

```bash
geas ontology-subscribe geas-samples https://github.com/Epiphytic/geas.git \
  --ref refs/heads/main
geas list
geas skill-export open-source-research-agents --link
```

The subscribe command states that it is adding and synchronizing the
subscription, verifies `geas.yaml` and its closed inventory, and asks before
trusting repository ontology bytes. Global `--yolo` may answer that trust gate
for one invocation only; it never persists trust and never bypasses catalog or
artifact integrity checks. See [user configuration](../../docs/USER_CONFIG.md)
for profiles and [portable ontology artifacts](../../docs/PORTABLE_ONTOLOGY_ARTIFACTS.md)
for the verified release asset used by skill export.

Remove the subscription checkout only through its exact managed identity:

```bash
geas ontology-unsubscribe geas-samples --remove-checkout
```

An exported agent skill remains an independently managed snapshot. Remove its
links and exact snapshot with `geas skill-remove /absolute/path/to/skill`; Geas
does not install itself when it is absent.

Run the complete build from a clean store:

```bash
demo_root=$(mktemp -d /tmp/geas-demo.XXXXXX)
./ontology/open-source-research-agents/demo.sh "$demo_root"
```

The script deterministically resolves and imports all three accepted seed
bundles through the verified catalog, audits them, captures canonical truth,
builds and checks the SQLite projection, runs representative natural-language queries,
preseeds and hydrates an offline content-addressed artifact, and exports the
same catalog-provenance Agent Skill twice. It refuses a root that already
contains canonical or projected state.

Maintenance is evidence-first:

1. Re-check official repositories, releases, and pinned commits in a bounded
   research pass.
2. Edit a source card with the new observed facts and observation date.
3. Update its SHA-256 in `bundle.yaml`.
4. Add or supersede claims, controversies, and gaps.
5. Run the demo and the full test suite.

The maintained `artifacts.yaml` describes a rebuildable current-schema
knowledge projection published as a content-addressed prerelease asset. It is a
query projection, not canonical truth, and it contains attributed excerpts
from project-authored source cards plus the accepted license-recorded
official-repository extracts. It contains no private source material.

Absence claims are deliberately narrow: “not verified in this source card” is
not equivalent to proving that a project lacks a capability.
