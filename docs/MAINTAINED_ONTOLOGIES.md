# Maintained ontology bundles

A maintained ontology bundle packages inspectable source cards and reviewed
knowledge records into a reproducible import unit. The first maintained bundle
is [`ontology/open-source-research-agents/`](../ontology/open-source-research-agents/).
For the complete build, refresh, review, and retrieval paths, see
[common Geas use cases](USE_CASES.md).

## Bundle boundary

`bundle.yaml` declares:

- a fixed research-recording time;
- confined relative source paths and expected SHA-256 values;
- observation time, original locator, title, authors, publisher, license,
  usage conditions, rights basis, and provenance;
- concepts, exact evidence quotes, claims, controversies, and knowledge gaps.

The archived source URI uses a stable `bundle:` locator. `original_locator`
records the official upstream location. This distinguishes a project-authored
source card from a claim that the card bytes are an upstream document.

Unknown remains the default for absent authorship, license, usage, and rights
fields. Declared values are stored with explicit status fields. The bundle
importer uses the accepted deployment-boundary authorization model and does not
implement per-record access control.

## Import workflow

The deterministic importer:

1. rejects absolute, parent-traversing, escaping, or symlinked source paths;
2. verifies every declared source hash before canonical writes;
3. archives and parses source bytes at the declared observation time;
4. scans inert text for prompt-injection and exfiltration patterns;
5. derives structure and citations;
6. requires each evidence quote to occur exactly once;
7. refuses accepted evidence from a source with an active deterministic threat;
8. validates concept hierarchy and all proposal references;
9. commits immutable records and a receipt.

```bash
uv run geas bundle-import \
  ontology/open-source-research-agents/bundle.yaml \
  --root data \
  --imported-by operator:local
```

## Maintenance audit

`knowledge-audit` is model-free. Given an explicit `--as-of` time, it records
deterministic findings for:

- missing claim evidence;
- accepted claims depending on actively tainted sources;
- controversies without deterministically distinct positions;
- unresolved gaps past their freshness deadline;
- resolved gaps without a linked resolving claim; and
- explicit textual retraction signals requiring authoritative resolution.

```bash
uv run geas knowledge-audit \
  --root data \
  --as-of 2026-08-03T16:00:00+00:00 \
  --fail-on-error
```

Warnings remain visible maintenance work; errors make the report unclean.
`--fail-on-error` returns exit status 2 only for errors.

## Example evidence limits

The open-source-agents bundle uses concise, Apache-2.0 project-authored source
cards checked against official repositories and pinned commits. A source card
is a curated provenance layer, not a verbatim upstream snapshot. Claims about
missing functionality therefore use narrow language such as “not verified,”
and gaps preserve work requiring code, paper, security-advisory, or benchmark
inspection.

Run `demo.sh` to build, audit, snapshot, project, query, export, and drift-check
the ontology from a clean directory. The integration test repeats the core
vertical slice and pins expected counts, poisoned-source behavior, query
results, citation counts, and agent-readable rendering.
