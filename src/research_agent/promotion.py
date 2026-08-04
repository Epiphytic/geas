from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from research_agent.extraction import ValidatedExtractionProposal
from research_agent.knowledge import (
    ClaimProposal,
    Concept,
    ControversyProposal,
    EvidenceProposal,
    GapProposal,
    KnowledgeImporter,
    KnowledgePack,
)
from research_agent.models import StrictModel, canonical_json, content_id
from research_agent.store import ImmutableStore
from research_agent.structure import StructuralAnchor, StructuralDerivation


class PromotionError(ValueError):
    pass


class PromotionManifest(StrictModel):
    version: Literal[1]
    id: str
    proposal_id: str
    proposal_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal: ValidatedExtractionProposal
    source_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    structural_derivation_id: str
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    target_ref: str
    accepted_by: str
    pack_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pack: KnowledgePack
    renderer_version: str
    authority: Literal["canonical_git_ref"] = "canonical_git_ref"
    repository_policy: Literal["out_of_scope"] = "out_of_scope"

    @field_validator("target_ref")
    @classmethod
    def target_is_full_ref(cls, value: str) -> str:
        if not value.startswith("refs/heads/") or any(
            character.isspace() or ord(character) < 32 for character in value
        ):
            raise ValueError("target_ref must be a full local branch ref")
        if (
            value.endswith((".", "/"))
            or ".." in value
            or "@{" in value
            or any(character in value for character in "~^:?*[\\")
        ):
            raise ValueError("target_ref is not a valid canonical branch ref")
        return value


class PromotionReceipt(StrictModel):
    id: str
    manifest_id: str
    manifest_path: str
    canonical_ref: str
    canonical_commit: str
    knowledge_import_receipt_id: str
    record_hashes: dict[str, tuple[str, ...]]
    verifier_version: str


class PromotionStageReceipt(StrictModel):
    manifest: PromotionManifest
    path: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    branch: str
    transport_commands: dict[str, tuple[str, ...]]


class GitPromotionManager:
    """Render and apply proposal promotions without trusting a forge API."""

    version = "git-promotion/1"

    def __init__(self, *, store: ImmutableStore, repository: Path) -> None:
        self.store = store
        self.repository = repository.resolve()

    def stage(
        self,
        proposal_id: str,
        *,
        topic: str,
        topic_concept_id: str,
        output: Path,
        target_ref: str = "refs/heads/main",
    ) -> PromotionStageReceipt:
        self.store.initialize()
        proposal, proposal_digest = self._stored_proposal(proposal_id)
        base_commit = self._git("rev-parse", "--verify", f"{target_ref}^{{commit}}")
        branch = self._git("branch", "--show-current")
        if not branch:
            raise PromotionError("promotion staging requires a named Git branch")
        accepted_by = f"vcs:{target_ref}"
        pack = self._build_pack(
            proposal,
            topic=topic,
            topic_concept_id=topic_concept_id,
            accepted_by=accepted_by,
        )
        pack_sha256 = hashlib.sha256(canonical_json(pack)).hexdigest()
        identity = {
            "proposal_id": proposal.id,
            "proposal_record_sha256": proposal_digest,
            "source_content_sha256": proposal.source_content_sha256,
            "structural_derivation_id": proposal.structural_derivation_id,
            "base_commit": base_commit,
            "target_ref": target_ref,
            "accepted_by": accepted_by,
            "pack_sha256": pack_sha256,
            "renderer_version": self.version,
        }
        manifest = PromotionManifest(
            version=1,
            id=content_id("promotion-manifest", identity),
            proposal_id=proposal.id,
            proposal_record_sha256=proposal_digest,
            proposal=proposal,
            source_content_sha256=proposal.source_content_sha256,
            structural_derivation_id=proposal.structural_derivation_id,
            base_commit=base_commit,
            target_ref=target_ref,
            accepted_by=accepted_by,
            pack_sha256=pack_sha256,
            pack=pack,
            renderer_version=self.version,
        )
        unresolved = output if output.is_absolute() else self.repository / output
        if unresolved.is_symlink():
            raise PromotionError("promotion manifest cannot replace a symlink")
        destination = unresolved.resolve()
        if not destination.is_relative_to(self.repository):
            raise PromotionError("promotion manifest must remain inside the repository")
        destination.parent.mkdir(parents=True, exist_ok=True)
        rendered = self._render(manifest)
        destination.write_bytes(rendered)
        relative = destination.relative_to(self.repository).as_posix()
        title = f"Promote extraction proposal {proposal.id}"
        return PromotionStageReceipt(
            manifest=manifest,
            path=relative,
            manifest_sha256=hashlib.sha256(rendered).hexdigest(),
            branch=branch,
            transport_commands={
                "github": (
                    "gh",
                    "pr",
                    "create",
                    "--base",
                    target_ref.removeprefix("refs/heads/"),
                    "--head",
                    branch,
                    "--title",
                    title,
                    "--body",
                    f"Reviews `{relative}`. Repository approval policy is out of scope.",
                ),
                "gitlab": (
                    "glab",
                    "mr",
                    "create",
                    "--source-branch",
                    branch,
                    "--target-branch",
                    target_ref.removeprefix("refs/heads/"),
                    "--title",
                    title,
                    "--description",
                    f"Reviews `{relative}`. Repository approval policy is out of scope.",
                ),
                "radicle": (
                    "git",
                    "push",
                    "rad",
                    "HEAD:refs/patches",
                    "-o",
                    f"patch.message={title}",
                ),
            },
        )

    def verify_from_ref(
        self,
        manifest_path: Path,
        *,
        canonical_ref: str = "refs/heads/main",
    ) -> tuple[PromotionManifest, str, str]:
        relative = self._relative_manifest_path(manifest_path)
        PromotionManifest.target_is_full_ref(canonical_ref)
        ref = canonical_ref
        commit = self._git("rev-parse", "--verify", f"{ref}^{{commit}}")
        raw = self._git_bytes("show", f"{commit}:{relative}")
        manifest = PromotionManifest.model_validate_json(raw)
        if manifest.target_ref != ref:
            raise PromotionError("manifest target ref does not match canonical ref")
        if self._git_status("merge-base", "--is-ancestor", manifest.base_commit, commit) != 0:
            raise PromotionError("manifest base commit is not an ancestor of canonical ref")
        self._verify_manifest(manifest)
        return manifest, relative, commit

    def apply(
        self,
        manifest_path: Path,
        *,
        canonical_ref: str = "refs/heads/main",
    ) -> PromotionReceipt:
        manifest, relative, commit = self.verify_from_ref(
            manifest_path,
            canonical_ref=canonical_ref,
        )
        importer = KnowledgeImporter(
            store=self.store,
            clock=lambda: manifest.proposal.proposed_at,
        )
        imported = importer.import_pack(manifest.pack, imported_by=manifest.accepted_by)
        fields = {
            "manifest_id": manifest.id,
            "manifest_path": relative,
            "canonical_ref": manifest.target_ref,
            "canonical_commit": commit,
            "knowledge_import_receipt_id": imported.id,
            "record_hashes": imported.record_hashes,
            "verifier_version": self.version,
        }
        receipt = PromotionReceipt(
            id=content_id("promotion-receipt", fields),
            **fields,
        )
        self.store.put_record("promotion-receipt", receipt)
        return receipt

    def _verify_manifest(self, manifest: PromotionManifest) -> None:
        if manifest.renderer_version != self.version:
            raise PromotionError("promotion renderer version is unsupported")
        if manifest.accepted_by != f"vcs:{manifest.target_ref}":
            raise PromotionError("promotion authority attribution is invalid")
        proposal, digest = self._stored_proposal(manifest.proposal_id)
        if proposal != manifest.proposal or digest != manifest.proposal_record_sha256:
            raise PromotionError("embedded proposal does not match immutable proposal record")
        if manifest.source_content_sha256 != proposal.source_content_sha256:
            raise PromotionError("manifest source hash does not match proposal")
        if manifest.structural_derivation_id != proposal.structural_derivation_id:
            raise PromotionError("manifest structural derivation does not match proposal")
        expected_pack = self._build_pack(
            proposal,
            topic=manifest.pack.topic,
            topic_concept_id=manifest.pack.topic_concept_id,
            accepted_by=manifest.accepted_by,
        )
        digest = hashlib.sha256(canonical_json(manifest.pack)).hexdigest()
        if digest != manifest.pack_sha256 or manifest.pack != expected_pack:
            raise PromotionError("promotion pack is not a lossless rendering of proposal")
        identity = {
            "proposal_id": proposal.id,
            "proposal_record_sha256": manifest.proposal_record_sha256,
            "source_content_sha256": manifest.source_content_sha256,
            "structural_derivation_id": manifest.structural_derivation_id,
            "base_commit": manifest.base_commit,
            "target_ref": manifest.target_ref,
            "accepted_by": manifest.accepted_by,
            "pack_sha256": manifest.pack_sha256,
            "renderer_version": manifest.renderer_version,
        }
        if manifest.id != content_id("promotion-manifest", identity):
            raise PromotionError("promotion manifest ID does not match its bound fields")
        self._verify_evidence(proposal)

    def _verify_evidence(self, proposal: ValidatedExtractionProposal) -> None:
        derivations = [
            StructuralDerivation.model_validate(value)
            for value in self.store.iter_records("structural-derivation")
            if value.get("id") == proposal.structural_derivation_id
        ]
        if len(derivations) != 1:
            raise PromotionError("proposal structural derivation is missing or ambiguous")
        derivation = derivations[0]
        if (
            derivation.source_version_id != proposal.source_version_id
            or derivation.source_content_sha256 != proposal.source_content_sha256
        ):
            raise PromotionError("proposal no longer matches structural derivation")
        anchors = {
            value["id"]: StructuralAnchor.model_validate(value)
            for value in self.store.iter_records("structural-anchor")
            if value.get("structural_derivation_id") == derivation.id
        }
        text = self.store.read_blob(proposal.source_content_sha256).decode("utf-8")
        for claim in proposal.claims:
            for evidence in claim.evidence:
                anchor = anchors.get(evidence.anchor_id)
                within_anchor = anchor is not None and (
                    anchor.start <= evidence.start < evidence.end <= anchor.end
                )
                if not within_anchor:
                    raise PromotionError("proposal evidence is outside its structural anchor")
                exact = text[evidence.start : evidence.end]
                if exact != evidence.exact:
                    raise PromotionError("proposal evidence range no longer matches source")
                if hashlib.sha256(exact.encode()).hexdigest() != evidence.exact_sha256:
                    raise PromotionError("proposal evidence hash no longer matches source")

    def _build_pack(
        self,
        proposal: ValidatedExtractionProposal,
        *,
        topic: str,
        topic_concept_id: str,
        accepted_by: str,
    ) -> KnowledgePack:
        proposed = {
            item.id: Concept(
                id=item.id,
                label=item.label,
                description=item.description,
                broader=item.broader,
                synonyms=item.synonyms,
                recorded_at=proposal.proposed_at,
                recorded_by=f"extraction-proposal:{proposal.id}",
            )
            for item in proposal.concepts
        }
        existing = {
            value["id"]: Concept.model_validate(value)
            for value in self.store.iter_records("concept")
        }
        required = {
            topic_concept_id,
            *proposed,
            *(claim.subject for claim in proposal.claims),
            *(parent for concept in proposal.concepts for parent in concept.broader),
        }
        while True:
            missing = sorted(required - set(proposed) - set(existing))
            if missing:
                raise PromotionError(
                    "promotion references concepts absent from proposal and accepted store: "
                    + ", ".join(missing)
                )
            parents: set[str] = set()
            for concept_id in required:
                concept = proposed.get(concept_id) or existing[concept_id]
                parents.update(concept.broader)
            expanded = required | parents
            if expanded == required:
                break
            required = expanded
        concepts = {item: proposed.get(item) or existing[item] for item in required}
        evidence: list[EvidenceProposal] = []
        claims: list[ClaimProposal] = []
        for claim in proposal.claims:
            evidence_keys = []
            for index, item in enumerate(claim.evidence):
                key = f"{claim.key}-evidence-{index + 1}"
                evidence_keys.append(key)
                evidence.append(
                    EvidenceProposal(
                        key=key,
                        source_content_sha256=proposal.source_content_sha256,
                        exact=item.exact,
                        start=item.start,
                        end=item.end,
                    )
                )
            claims.append(
                ClaimProposal(
                    key=claim.key,
                    subject=claim.subject,
                    predicate=claim.predicate,
                    object=claim.object,
                    qualifiers=claim.qualifiers,
                    stance=claim.stance,
                    epistemic_status=claim.epistemic_status,
                    asserted_by=claim.asserted_by,
                    evidence_keys=tuple(evidence_keys),
                )
            )
        controversies = tuple(
            ControversyProposal(
                topic_concept_id=topic_concept_id,
                question=item.question,
                description=item.description,
                claim_keys=item.claim_keys,
                status=item.status,
            )
            for item in proposal.controversies
        )
        gaps = tuple(
            GapProposal(
                topic_concept_id=topic_concept_id,
                question=item.question,
                kind=item.kind,
                rationale=item.rationale,
                related_claim_keys=item.related_claim_keys,
                priority=item.priority,
            )
            for item in proposal.gaps
        )
        return KnowledgePack(
            version=1,
            topic=topic,
            topic_concept_id=topic_concept_id,
            concepts=tuple(sorted(concepts.values(), key=lambda item: item.id)),
            evidence=tuple(evidence),
            claims=tuple(claims),
            controversies=controversies,
            gaps=gaps,
            inspect_source_sha256s=(proposal.source_content_sha256,),
        )

    def _stored_proposal(
        self,
        proposal_id: str,
    ) -> tuple[ValidatedExtractionProposal, str]:
        values = [
            value
            for value in self.store.iter_records("extraction-proposal")
            if value.get("id") == proposal_id
        ]
        if len(values) != 1:
            raise PromotionError("extraction proposal does not exist or is ambiguous")
        proposal = ValidatedExtractionProposal.model_validate(values[0])
        return proposal, hashlib.sha256(canonical_json(proposal)).hexdigest()

    def _relative_manifest_path(self, path: Path) -> str:
        unresolved = path if path.is_absolute() else self.repository / path
        if unresolved.is_symlink():
            raise PromotionError("manifest path must be a non-symlink inside repository")
        absolute = unresolved.resolve()
        if not absolute.is_relative_to(self.repository):
            raise PromotionError("manifest path must be a non-symlink inside repository")
        return absolute.relative_to(self.repository).as_posix()

    @staticmethod
    def _render(manifest: PromotionManifest) -> bytes:
        value = json.loads(canonical_json(manifest))
        return (
            json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True).encode()
            + b"\n"
        )

    def _git(self, *arguments: str) -> str:
        return self._git_bytes(*arguments).decode().strip()

    def _git_bytes(self, *arguments: str) -> bytes:
        result = subprocess.run(
            ("git", "-C", str(self.repository), *arguments),
            check=False,
            capture_output=True,
        )
        if result.returncode:
            message = result.stderr.decode(errors="replace").strip()
            raise PromotionError(f"Git command failed: {message}")
        return result.stdout

    def _git_status(self, *arguments: str) -> int:
        return subprocess.run(
            ("git", "-C", str(self.repository), *arguments),
            check=False,
            capture_output=True,
        ).returncode
