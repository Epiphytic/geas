# Open-source research agents ontology

This directory is a maintained, executable example of the repository's durable
research product. It compares open-source research agents and adjacent
knowledge-curation systems without treating a generated report as canonical.

`bundle.yaml` is the reviewed ontology bundle. `sources/` contains concise
project-authored source cards pinned to official repository commits or local
project documentation. Each card is SHA-256 pinned in the bundle, parsed as
inert data, structurally anchored, citation-indexed, and linked to exact claim
evidence. The synthetic poisoned source is deliberately inspected but is never
used as claim evidence.

Run the complete build from a clean store:

```bash
demo_root=$(mktemp -d /tmp/geas-demo.XXXXXX)
./ontology/open-source-research-agents/demo.sh "$demo_root"
```

The script imports and audits the bundle, captures canonical truth, builds and
checks the SQLite projection, runs representative natural-language queries, and
writes an agent-readable `topic.md`. It refuses a root that already contains
canonical or projected state.

Maintenance is evidence-first:

1. Re-check official repositories and pinned commits.
2. Edit a source card with the new observed facts and observation date.
3. Update its SHA-256 in `bundle.yaml`.
4. Add or supersede claims, controversies, and gaps.
5. Run the demo and the full test suite.

Absence claims are deliberately narrow: “not verified in this source card” is
not equivalent to proving that a project lacks a capability.
