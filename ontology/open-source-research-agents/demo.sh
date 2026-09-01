#!/usr/bin/env bash
set -euo pipefail

workspace_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
ontology_path="$workspace_root/ontology/open-source-research-agents"
demo_root=${1:-$(mktemp -d /tmp/geas-open-source-agents.XXXXXX)}

if [[ -e "$demo_root/records" || -e "$demo_root/blobs" || -e "$demo_root/query.sqlite" ]]; then
  echo "demo root already contains canonical or projected state: $demo_root" >&2
  exit 2
fi
mkdir -p "$demo_root"
demo_root=$(cd "$demo_root" && pwd -P)

cd "$workspace_root"

uv run python - "$workspace_root" "$demo_root/seed-bundles.txt" <<'PY'
import sys
from pathlib import Path

from research_agent.ontology_build import OntologyBuildConfig
from research_agent.repository_catalog import verify_catalog

workspace = Path(sys.argv[1]).resolve()
destination = Path(sys.argv[2])
ontology = workspace / "ontology" / "open-source-research-agents"
config = OntologyBuildConfig.from_yaml(ontology / "build.yaml")
catalog = verify_catalog(workspace / "geas.yaml", names=(ontology.name,))[0]
catalog_paths = {
    (catalog.ontology_path / item.path).resolve()
    for item in catalog.files
}
seeds = {workspace / path for path in config.seed_bundles}
for pattern in config.seed_bundle_globs:
    matches = tuple(sorted(workspace.glob(pattern)))
    if not matches:
        raise SystemExit(f"seed bundle glob matched no files: {pattern}")
    seeds.update(matches)
resolved = tuple(sorted(path.resolve() for path in seeds))
for path in resolved:
    if not path.is_file() or path.is_symlink() or not path.is_relative_to(ontology):
        raise SystemExit(f"invalid maintained seed bundle path: {path}")
    if path not in catalog_paths:
        raise SystemExit(f"maintained seed bundle is absent from the verified catalog: {path}")
destination.write_text(
    "".join(f"{path.relative_to(workspace).as_posix()}\n" for path in resolved)
)
PY

seed_index=0
while IFS= read -r seed_bundle; do
  uv run geas bundle-import \
    "$seed_bundle" \
    --root "$demo_root" \
    --imported-by operator:demo \
    > "$demo_root/import-$seed_index.json"
  seed_index=$((seed_index + 1))
done < "$demo_root/seed-bundles.txt"
jq -s '.' "$demo_root"/import-*.json > "$demo_root/imports.json"

uv run geas knowledge-audit \
  --root "$demo_root" \
  --as-of 2026-08-29T17:00:00+00:00 \
  --fail-on-error \
  > "$demo_root/audit.json"

uv run geas --truth-policy "$workspace_root/config/truth-policy.yaml" truth-snapshot \
  --root "$demo_root" \
  --workspace "$workspace_root" \
  --created-by operator:demo \
  --created-at 2026-08-29T17:00:00+00:00 \
  > "$demo_root/snapshot-envelope.json"
jq '.snapshot' "$demo_root/snapshot-envelope.json" > "$demo_root/snapshot.json"

uv run geas --truth-policy "$workspace_root/config/truth-policy.yaml" projection-build \
  "$demo_root/snapshot.json" \
  "$demo_root/query.sqlite" \
  --root "$demo_root" \
  --workspace "$workspace_root" \
  > "$demo_root/projection.json"

uv run python - "$demo_root/snapshot.json" "$demo_root/query.sqlite" <<'PY'
import sys
from datetime import UTC, datetime
from pathlib import Path

from research_agent.projection import SQLiteKnowledgeProjection
from research_agent.truth import SQLiteProjectionGuard, TruthSnapshot

snapshot = TruthSnapshot.model_validate_json(Path(sys.argv[1]).read_text())
SQLiteProjectionGuard(
    clock=lambda: datetime(2026, 8, 29, 17, 0, tzinfo=UTC)
).stamp(
    Path(sys.argv[2]),
    snapshot,
    schema_version=SQLiteKnowledgeProjection.schema_version,
    builder_version=SQLiteKnowledgeProjection.builder_version,
)
PY

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
demo_ref=$(git symbolic-ref -q HEAD || printf '%s' "$demo_commit")
uv run python - "$demo_root" "$workspace_root" "$demo_commit" "$demo_ref" <<'PY'
import hashlib
import json
import shutil
import sys
from pathlib import Path

from research_agent.agent_skills import (
    OntologyIdentity,
    PortableArtifactIdentity,
    bind_catalog_skill_provenance,
    install_snapshot,
    validate_snapshot,
)
from research_agent.ontology_artifacts import (
    ArtifactRole,
    OntologyArtifact,
    OntologyArtifactManager,
)
from research_agent.projection import KnowledgeQueryEngine
from research_agent.render import render_ontology_skill
from research_agent.repository_catalog import verify_catalog


class LocalArtifactStore:
    """Offline content-addressed store used to exercise artifact hydration."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True)

    def _path(self, artifact: OntologyArtifact) -> Path:
        return self.directory / artifact.asset_name

    def ensure(self, artifact: OntologyArtifact, source: Path) -> bool:
        destination = self._path(artifact)
        if destination.is_file():
            return False
        shutil.copyfile(source, destination)
        return True

    def available(self, artifact: OntologyArtifact) -> bool:
        return self._path(artifact).is_file()

    def download(self, artifact: OntologyArtifact, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._path(artifact), destination)

root = Path(sys.argv[1])
workspace = Path(sys.argv[2])
ontology_commit = sys.argv[3]
active_ref = sys.argv[4]
catalog_ontology = verify_catalog(
    workspace / "geas.yaml", names=("open-source-research-agents",)
)[0]
portable_ontology = root / "portable-ontology" / "open-source-research-agents"
portable_ontology.mkdir(parents=True)
artifact_store = LocalArtifactStore(root / "artifact-store")
artifact_manager = OntologyArtifactManager(portable_ontology)
published = artifact_manager.publish(
    store=artifact_store,
    published_by="operator:demo",
    storage_rights_basis=(
        "Offline deterministic demo projection over accepted project-authored source "
        "cards and accepted, license-recorded official-repository source extracts."
    ),
    knowledge_projection=root / "query.sqlite",
)
first_hydration = artifact_manager.hydrate(
    store=artifact_store,
    roles=(ArtifactRole.KNOWLEDGE_PROJECTION,),
)
second_hydration = artifact_manager.hydrate(
    store=artifact_store,
    roles=(ArtifactRole.KNOWLEDGE_PROJECTION,),
)
first_artifact = first_hydration.hydrated[0]
second_artifact = second_hydration.hydrated[0]
topic = KnowledgeQueryEngine(Path(first_artifact.path)).topic(
    "concept:open-source-research-agents"
)
files = render_ontology_skill(
    topic,
    skill_name="open-source-research-agents",
    ontology_name="open-source-research-agents",
    repository_url="https://github.com/Epiphytic/geas.git",
    branch=active_ref.removeprefix("refs/heads/"),
    ontology_commit=ontology_commit,
    geas_version="0.1.0",
    geas_commit=ontology_commit,
)
files = bind_catalog_skill_provenance(
    files,
    ontology=OntologyIdentity(
        name="open-source-research-agents",
        repository_url="https://github.com/Epiphytic/geas.git",
        branch=active_ref.removeprefix("refs/heads/"),
        commit=ontology_commit,
        active_ref=active_ref,
        ontology_commit=ontology_commit,
        subscription_name="geas-samples",
        catalog_path="geas.yaml",
        ontology_path="ontology/open-source-research-agents",
        bundle_sha256=catalog_ontology.bundle_sha256,
    ),
    artifact=PortableArtifactIdentity(
        role=first_artifact.role.value,
        content_sha256=first_artifact.content_sha256,
        input_revision=first_artifact.input_revision,
    ),
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
write_json("artifact-publish.json", published.model_dump(mode="json"))
write_json("artifact-hydration-first.json", first_artifact.model_dump(mode="json"))
write_json("artifact-hydration-second.json", second_artifact.model_dump(mode="json"))
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
if first_artifact.downloaded is not True or second_artifact.downloaded is not False:
    raise SystemExit("demo artifact hydration must download then reuse the preseeded asset")
if first_artifact.content_sha256 != second_artifact.content_sha256:
    raise SystemExit("demo artifact hydration content addresses differ")
PY

uv run geas --truth-policy "$workspace_root/config/truth-policy.yaml" projection-check \
  "$demo_root/snapshot.json" \
  "$demo_root/query.sqlite" \
  --root "$demo_root" \
  --workspace "$workspace_root" \
  > "$demo_root/drift-check.json"

jq -n \
  --arg root "$demo_root" \
  --slurpfile imported "$demo_root/imports.json" \
  --rawfile seed_bundles "$demo_root/seed-bundles.txt" \
  --slurpfile projection "$demo_root/projection.json" \
  --slurpfile persistent "$demo_root/query-persistent-knowledge.json" \
  --slurpfile threats "$demo_root/query-threats.json" \
  --slurpfile storm "$demo_root/query-storm.json" \
  --slurpfile audit "$demo_root/audit.json" \
  --slurpfile drift "$demo_root/drift-check.json" \
  --slurpfile skill "$demo_root/skill-export-second.json" \
  '{
    demo_root: $root,
    topic: $imported[0][0].topic,
    seed_bundles: ($seed_bundles | split("\n") | map(select(length > 0) | sub("^ontology/open-source-research-agents/"; ""))),
    sources: ([$imported[0][].parse_receipts[]] | length),
    claims: ([$imported[0][].knowledge_receipt.claim_ids[]] | unique | length),
    controversies: ([$imported[0][].knowledge_receipt.controversy_ids[]] | unique | length),
    gaps: ([$imported[0][].knowledge_receipt.gap_ids[]] | unique | length),
    threat_observations: ([$imported[0][].knowledge_receipt.threat_observation_ids[]] | unique | length),
    references: ([$imported[0][].parse_receipts[].bibliographic_reference_ids[]] | unique | length),
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
