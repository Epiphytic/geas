#!/usr/bin/env bash
set -euo pipefail

workspace_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
bundle_path="$workspace_root/ontology/open-source-research-agents/bundle.yaml"
demo_root=${1:-$(mktemp -d /tmp/research-agent-open-source-agents.XXXXXX)}

if [[ -e "$demo_root/records" || -e "$demo_root/blobs" || -e "$demo_root/query.sqlite" ]]; then
  echo "demo root already contains canonical or projected state: $demo_root" >&2
  exit 2
fi
mkdir -p "$demo_root"

cd "$workspace_root"

uv run research-agent bundle-import \
  "$bundle_path" \
  --root "$demo_root" \
  --imported-by operator:demo \
  > "$demo_root/import.json"

uv run research-agent knowledge-audit \
  --root "$demo_root" \
  --as-of 2026-08-03T16:00:00+00:00 \
  --fail-on-error \
  > "$demo_root/audit.json"

uv run research-agent truth-snapshot \
  --root "$demo_root" \
  --workspace "$workspace_root" \
  --created-by operator:demo \
  > "$demo_root/snapshot-envelope.json"
jq '.snapshot' "$demo_root/snapshot-envelope.json" > "$demo_root/snapshot.json"

uv run research-agent projection-build \
  "$demo_root/snapshot.json" \
  "$demo_root/query.sqlite" \
  --root "$demo_root" \
  --workspace "$workspace_root" \
  > "$demo_root/projection.json"

uv run research-agent knowledge-query \
  "persistent ontology exact evidence and deterministic retrieval" \
  --database "$demo_root/query.sqlite" \
  --limit 20 \
  > "$demo_root/query-persistent-knowledge.json"

uv run research-agent knowledge-query \
  "prompt injection poisoned source threat" \
  --database "$demo_root/query.sqlite" \
  --kind threat \
  --limit 20 \
  > "$demo_root/query-threats.json"

uv run research-agent knowledge-query \
  "STORM hierarchical mind map references" \
  --database "$demo_root/query.sqlite" \
  --kind claim \
  --kind reference \
  --limit 20 \
  > "$demo_root/query-storm.json"

uv run research-agent topic-export \
  concept:open-source-research-agents \
  "$demo_root/topic.md" \
  --database "$demo_root/query.sqlite" \
  > "$demo_root/topic-export.json"

uv run research-agent projection-check \
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
    agent_readable_topic: ($root + "/topic.md")
  }'
