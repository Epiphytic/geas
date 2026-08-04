from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

from research_agent.bundles import BundleEvidence, BundleSource, KnowledgeBundle
from research_agent.discovery_acquisition import RepositorySnapshot
from research_agent.extraction import ValidatedExtractionProposal
from research_agent.knowledge import KnowledgePack
from research_agent.models import canonical_json
from research_agent.promotion import GitPromotionManager, PromotionError
from research_agent.store import ImmutableStore


class CandidateBundleError(ValueError):
    pass


class CandidateLicenseError(CandidateBundleError):
    pass


class CandidateBundleWriter:
    """Render validated proposals and immutable source bytes for Git review."""

    version = "candidate-bundle-writer/2"
    permissive_licenses = frozenset(
        {
            "0BSD",
            "Apache-2.0",
            "BSD-2-Clause",
            "BSD-3-Clause",
            "CC-BY-4.0",
            "CC0-1.0",
            "ISC",
            "MIT",
            "MPL-2.0",
            "Unlicense",
        }
    )

    def __init__(self, *, store: ImmutableStore, workspace: Path) -> None:
        self.store = store
        self.workspace = workspace.resolve()

    def write(
        self,
        proposal: ValidatedExtractionProposal,
        snapshot: RepositorySnapshot,
        *,
        topic: str,
        topic_concept_id: str,
        output_root: Path,
    ) -> Path:
        if proposal.source_version_id != snapshot.source_version_id:
            raise CandidateBundleError("proposal and repository snapshot source differ")
        if snapshot.license not in self.permissive_licenses:
            raise CandidateLicenseError(
                "source license is not a known redistributable license"
            )
        manager = GitPromotionManager(store=self.store, repository=self.workspace)
        try:
            pack = manager.build_pack(
                proposal,
                topic=topic,
                topic_concept_id=topic_concept_id,
                accepted_by="vcs:refs/heads/main",
            )
        except PromotionError as error:
            raise CandidateBundleError(str(error)) from error
        pack = self._namespace_proposed_concepts(pack, proposal, snapshot)
        slug = re.sub(r"[^a-z0-9]+", "-", snapshot.repository.casefold()).strip("-")
        relative_root = output_root / slug
        self._confined(relative_root)
        source_name = f"{slug}-{snapshot.source_content_sha256[:12]}.md"
        source_path = self._confined(relative_root / "sources" / source_name)
        bundle_path = self._confined(relative_root / "bundle.yaml")
        source_path.parent.mkdir(parents=True, exist_ok=True)
        content = self.store.read_blob(snapshot.source_content_sha256)
        source_path.write_bytes(content)
        source_key = f"{slug}-{snapshot.source_content_sha256[:12]}"
        bundle = KnowledgeBundle(
            version=1,
            topic=topic,
            topic_concept_id=topic_concept_id,
            recorded_at=proposal.proposed_at,
            sources=(
                BundleSource(
                    key=source_key,
                    path=f"sources/{source_name}",
                    expected_sha256=hashlib.sha256(content).hexdigest(),
                    acquired_at=snapshot.observed_at,
                    original_locator=(
                        f"{snapshot.canonical_locator}/tree/{snapshot.commit_sha}/"
                        f"{snapshot.readme_path}"
                    ),
                    title=f"{snapshot.repository} README at {snapshot.commit_sha[:12]}",
                    publisher="GitHub",
                    media_type="text/markdown",
                    license=snapshot.license,
                    usage_conditions=(
                        f"Redistributed under SPDX license {snapshot.license}.",
                    ),
                    rights_basis=f"SPDX license reported by the official repository API: "
                    f"{snapshot.license}",
                    provenance_note=(
                        "Resolved through the official GitHub API, pinned to immutable "
                        f"commit {snapshot.commit_sha}, and verified against Git blob "
                        f"{snapshot.readme_blob_sha}."
                    ),
                ),
            ),
            concepts=pack.concepts,
            evidence=tuple(
                BundleEvidence(
                    key=item.key,
                    source_key=source_key,
                    exact=item.exact,
                    prefix=item.prefix,
                    suffix=item.suffix,
                    start=item.start,
                    end=item.end,
                )
                for item in pack.evidence
            ),
            claims=pack.claims,
            controversies=pack.controversies,
            gaps=pack.gaps,
        )
        rendered = yaml.safe_dump(
            bundle.model_dump(mode="json", exclude_none=True),
            allow_unicode=True,
            sort_keys=False,
        ).encode()
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        bundle_path.write_bytes(rendered)
        # Reparse before returning so malformed output can never be staged.
        if canonical_json(KnowledgeBundle.from_yaml(bundle_path)) != canonical_json(bundle):
            raise CandidateBundleError("rendered candidate bundle failed round-trip validation")
        return bundle_path.relative_to(self.workspace)

    @staticmethod
    def _namespace_proposed_concepts(
        pack: KnowledgePack,
        proposal: ValidatedExtractionProposal,
        snapshot: RepositorySnapshot,
    ) -> KnowledgePack:
        """Make model-created IDs deterministic and collision-free across repositories."""

        repository_namespace = snapshot.repository.casefold().replace("/", ":")
        proposed_ids = {item.id for item in proposal.concepts}
        remap = {
            concept_id: f"concept:{repository_namespace}:{concept_id.removeprefix('concept:')}"
            for concept_id in proposed_ids
        }

        def mapped(value: str) -> str:
            return remap.get(value, value)

        concepts = tuple(
            item.model_copy(
                update={
                    "id": mapped(item.id),
                    "broader": tuple(mapped(parent) for parent in item.broader),
                }
            )
            for item in pack.concepts
        )
        claims = tuple(
            item.model_copy(
                update={
                    "subject": mapped(item.subject),
                    "object": mapped(item.object)
                    if isinstance(item.object, str)
                    else item.object,
                    "qualifiers": {
                        key: mapped(value) if isinstance(value, str) else value
                        for key, value in item.qualifiers.items()
                    },
                }
            )
            for item in pack.claims
        )
        controversies = tuple(
            item.model_copy(update={"topic_concept_id": mapped(item.topic_concept_id)})
            for item in pack.controversies
        )
        gaps = tuple(
            item.model_copy(update={"topic_concept_id": mapped(item.topic_concept_id)})
            for item in pack.gaps
        )
        return pack.model_copy(
            update={
                "concepts": concepts,
                "claims": claims,
                "controversies": controversies,
                "gaps": gaps,
            }
        )

    def _confined(self, relative: Path) -> Path:
        if relative.is_absolute() or ".." in relative.parts:
            raise CandidateBundleError("candidate bundle path must remain in the workspace")
        unresolved = self.workspace / relative
        if unresolved.is_symlink():
            raise CandidateBundleError("candidate bundle path cannot replace a symlink")
        resolved = unresolved.resolve()
        if not resolved.is_relative_to(self.workspace):
            raise CandidateBundleError("candidate bundle path escapes the workspace")
        return resolved
