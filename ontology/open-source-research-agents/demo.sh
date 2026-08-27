#!/usr/bin/env bash
set -euo pipefail

workspace_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
bundle_path="$workspace_root/ontology/open-source-research-agents/bundle.yaml"
demo_root=${1:-$(mktemp -d /tmp/geas-open-source-agents.XXXXXX)}

if [[ -e "$demo_root/records" || -e "$demo_root/blobs" || -e "$demo_root/query.sqlite" ]]; then
  echo "demo root already contains canonical or projected state: $demo_root" >&2
  exit 2
fi
mkdir -p "$demo_root"
demo_root=$(cd "$demo_root" && pwd -P)

cd "$workspace_root"

uv run geas bundle-import \
  "$bundle_path" \
  --root "$demo_root" \
  --imported-by operator:demo \
  > "$demo_root/import.json"

uv run geas knowledge-audit \
  --root "$demo_root" \
  --as-of 2026-08-03T16:00:00+00:00 \
  --fail-on-error \
  > "$demo_root/audit.json"

uv run geas truth-snapshot \
  --root "$demo_root" \
  --workspace "$workspace_root" \
  --created-by operator:demo \
  > "$demo_root/snapshot-envelope.json"
jq '.snapshot' "$demo_root/snapshot-envelope.json" > "$demo_root/snapshot.json"

uv run geas projection-build \
  "$demo_root/snapshot.json" \
  "$demo_root/query.sqlite" \
  --root "$demo_root" \
  --workspace "$workspace_root" \
  > "$demo_root/projection.json"

uv run geas knowledge-query \
  "persistent ontology exact evidence and deterministic retrieval" \
  --database "$demo_root/query.sqlite" \
  --limit 20 \
  > "$demo_root/query-persistent-knowledge.json"

uv run geas knowledge-query \
  "prompt injection poisoned source threat" \
  --database "$demo_root/query.sqlite" \
  --kind threat \
  --limit 20 \
  > "$demo_root/query-threats.json"

uv run geas knowledge-query \
  "STORM hierarchical mind map references" \
  --database "$demo_root/query.sqlite" \
  --kind claim \
  --kind reference \
  --limit 20 \
  > "$demo_root/query-storm.json"

uv run geas topic-export \
  concept:open-source-research-agents \
  "$demo_root/topic.md" \
  --database "$demo_root/query.sqlite" \
  > "$demo_root/topic-export.json"

demo_commit=$(git rev-parse HEAD)
uv run python - "$demo_root" "$demo_commit" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

from research_agent.agent_skills import install_snapshot, validate_snapshot
from research_agent.projection import KnowledgeQueryEngine
from research_agent.render import render_ontology_skill

root = Path(sys.argv[1])
topic = KnowledgeQueryEngine(root / "query.sqlite").topic(
    "concept:open-source-research-agents"
)
files = render_ontology_skill(
    topic,
    skill_name="open-source-research-agents",
    ontology_name="open-source-research-agents",
    repository_url="https://github.com/Epiphytic/geas.git",
    branch="main",
    ontology_commit=sys.argv[2],
    geas_version="0.1.0",
    geas_commit=None,
)
target = root / "agent-skill" / "open-source-research-agents"
first = install_snapshot(files, target)
first_hashes = {
    path.relative_to(target).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted(target.rglob("*"))
    if path.is_file()
}
second = install_snapshot(files, target)
manifest = validate_snapshot(target)
second_hashes = {
    path.relative_to(target).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted(target.rglob("*"))
    if path.is_file()
}

def receipt(value):
    return {
        "ontology_commit": value.manifest.ontology.commit,
        "path": str(value.path),
        "projection_snapshot_id": value.manifest.projection.snapshot_id,
        "snapshot_sha256": value.manifest.snapshot_sha256,
        "unchanged": value.unchanged,
    }

def write_json(name, value):
    path = root / name
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return json.loads(path.read_text())

first_receipt = write_json("skill-export-first.json", receipt(first))
second_receipt = write_json("skill-export-second.json", receipt(second))
first_inventory = write_json("skill-export-first-files.json", first_hashes)
second_inventory = write_json("skill-export-second-files.json", second_hashes)
write_json("skill-export-files.json", second_hashes)

if first_receipt["unchanged"] is not False or second_receipt["unchanged"] is not True:
    raise SystemExit("demo skill exports must be changed then unchanged")
for field in ("snapshot_sha256", "projection_snapshot_id", "ontology_commit"):
    if first_receipt[field] != second_receipt[field]:
        raise SystemExit(f"demo skill receipts disagree on {field}")
if list(first_inventory) != sorted(first_inventory) or list(second_inventory) != sorted(second_inventory):
    raise SystemExit("demo skill file inventories must be sorted")
if first_inventory != second_inventory:
    raise SystemExit("demo skill file inventories differ")
if manifest.snapshot_sha256 != first_receipt["snapshot_sha256"]:
    raise SystemExit("demo skill manifest digest mismatch")
PY

uv run geas projection-check \
  "$demo_root/snapshot.json" \
  "$demo_root/query.sqlite" \
  --root "$demo_root" \
  --workspace "$workspace_root" \
  > "$demo_root/drift-check.json"

jq -n \
  --arg root "$demo_root" \
  --slurpfile imported "$demo_root/import.json" \
  --slurpfile projection "$demo_root/projection.json" \
  --slurpfile persistent "$demo_root/query-persistent-knowledge.json" \
  --slurpfile threats "$demo_root/query-threats.json" \
  --slurpfile storm "$demo_root/query-storm.json" \
  --slurpfile audit "$demo_root/audit.json" \
  --slurpfile drift "$demo_root/drift-check.json" \
  --slurpfile skill "$demo_root/skill-export-second.json" \
  '{
    demo_root: $root,
    topic: $imported[0].topic,
    sources: ($imported[0].parse_receipts | length),
    claims: ($imported[0].knowledge_receipt.claim_ids | length),
    controversies: ($imported[0].knowledge_receipt.controversy_ids | length),
    gaps: ($imported[0].knowledge_receipt.gap_ids | length),
    threat_observations: ($imported[0].knowledge_receipt.threat_observation_ids | length),
    references: ([$imported[0].parse_receipts[].bibliographic_reference_ids[]] | length),
    projection_schema: $projection[0].schema_version,
    persistent_query_hits: ($persistent[0].hits | length),
    threat_query_hits: ($threats[0].hits | length),
    storm_query_hits: ($storm[0].hits | length),
    audit_clean: $audit[0].report.clean,
    drift_clean: $drift[0].clean,
    agent_readable_topic: ($root + "/topic.md"),
    portable_skill: $skill[0].path,
    portable_skill_sha256: $skill[0].snapshot_sha256
  }'
