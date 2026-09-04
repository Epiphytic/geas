import json
import subprocess
from datetime import UTC, datetime

import pytest

from research_agent.capabilities import Capability
from research_agent.extraction import AnchorGroundedExtractionManager
from research_agent.parsing import ParsedDocumentManager
from research_agent.promotion import GitPromotionManager, PromotionError
from research_agent.publishing import PathRole, PublishMode, required_capabilities
from research_agent.store import ImmutableStore

INSTANT = datetime(2026, 8, 3, tzinfo=UTC)
TEXT = """# Finding

The reviewed source supports a persistent graph.
"""


class _Client:
    def __init__(self, anchor_id):
        self.anchor_id = anchor_id

    def complete_json(self, **_kwargs):
        return {
            "version": 1,
            "concepts": [
                {
                    "key": "research",
                    "id": "concept:research",
                    "label": "Research",
                    "description": "Persistent research knowledge.",
                }
            ],
            "claims": [
                {
                    "key": "persistent",
                    "subject": "concept:research",
                    "predicate": "ep:stores",
                    "object": "persistent graph",
                    "stance": "asserts",
                    "epistemic_status": "observed",
                    "evidence": [
                        {
                            "anchor_id": self.anchor_id,
                            "exact": "The reviewed source supports a persistent graph.",
                        }
                    ],
                }
            ],
        }


def _git(repository, *args):
    return subprocess.run(
        ("git", "-C", str(repository), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fixture(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Test Operator")
    _git(repository, "config", "user.email", "operator@example.invalid")
    (repository / "README.md").write_text("test\n")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "initial")
    _git(repository, "switch", "-c", "proposal")

    store = ImmutableStore(tmp_path / "data")
    parsed = ParsedDocumentManager(store=store, clock=lambda: INSTANT).ingest(
        TEXT.encode(),
        source_uri="file:///finding.md",
        media_type="text/markdown",
        connector_id="connector:test",
        license="unknown",
    )
    paragraph = next(
        value
        for value in store.iter_records("structural-anchor")
        if value["kind"] == "paragraph"
    )
    proposal = AnchorGroundedExtractionManager(
        store=store,
        client=_Client(paragraph["id"]),
        provider="deepseek_local",
        model="fixture",
        clock=lambda: INSTANT,
    ).propose(
        question="What does the source support?",
        structural_derivation_id=parsed.structural_derivation_id,
        anchor_ids=[paragraph["id"]],
    )
    return repository, store, proposal.proposal.id


def test_git_promotion_requires_exact_manifest_on_canonical_ref(tmp_path) -> None:
    repository, store, proposal_id = _fixture(tmp_path)
    manager = GitPromotionManager(store=store, repository=repository)
    output = repository / "ontology" / "promotions" / "proposal.json"
    staged = manager.stage(
        proposal_id,
        topic="Research",
        topic_concept_id="concept:research",
        output=output,
    )
    first_bytes = output.read_bytes()
    second = manager.stage(
        proposal_id,
        topic="Research",
        topic_concept_id="concept:research",
        output=output,
    )

    assert output.read_bytes() == first_bytes
    assert second.manifest == staged.manifest
    assert staged.manifest.repository_policy == "out_of_scope"
    assert staged.transport_commands["radicle"][:4] == (
        "git",
        "push",
        "rad",
        "HEAD:refs/patches",
    )
    assert tuple(store.iter_records("claim")) == ()
    assert required_capabilities(
        PathRole.CANONICAL_KNOWLEDGE,
        PublishMode.DIRECT_PUSH,
        canonical_target=True,
    ) == frozenset(
        {Capability.GIT_DIRECT_PUSH, Capability.KNOWLEDGE_AUTO_PROMOTE}
    )
    with pytest.raises(PromotionError, match="Git command failed"):
        manager.verify_from_ref(output)

    _git(repository, "add", staged.path)
    _git(repository, "commit", "-m", "Propose ontology promotion")
    _git(repository, "switch", "main")
    _git(repository, "merge", "--ff-only", "proposal")
    verified, relative, commit = manager.verify_from_ref(output)
    receipt = manager.apply(output)
    repeated = manager.apply(output)

    assert verified.id == staged.manifest.id
    assert relative == staged.path
    assert commit == _git(repository, "rev-parse", "main")
    assert receipt == repeated
    claims = tuple(store.iter_records("claim"))
    assert len(claims) == 1
    assert claims[0]["review_state"] == "accepted"
    assert claims[0]["recorded_at"] == INSTANT.isoformat().replace("+00:00", "Z")


def test_git_promotion_rejects_semantic_edits_even_after_merge(tmp_path) -> None:
    repository, store, proposal_id = _fixture(tmp_path)
    manager = GitPromotionManager(store=store, repository=repository)
    output = repository / "ontology" / "promotions" / "proposal.json"
    manager.stage(
        proposal_id,
        topic="Research",
        topic_concept_id="concept:research",
        output=output,
    )
    value = json.loads(output.read_text())
    value["pack"]["claims"][0]["object"] = "silently edited conclusion"
    output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    _git(repository, "add", "ontology/promotions/proposal.json")
    _git(repository, "commit", "-m", "Tamper with proposed fact")
    _git(repository, "switch", "main")
    _git(repository, "merge", "--ff-only", "proposal")

    with pytest.raises(PromotionError, match="lossless rendering"):
        manager.verify_from_ref(output)


def test_ranged_evidence_allows_repeated_text(tmp_path) -> None:
    store = ImmutableStore(tmp_path / "data")
    source = store.ingest_bytes(
        b"Repeated fact.\nRepeated fact.\n",
        source_uri="file:///repeated.txt",
        media_type="text/plain",
        connector_id="connector:test",
        acquired_at=INSTANT,
    )
    from research_agent.knowledge import (
        ClaimProposal,
        Concept,
        EvidenceProposal,
        KnowledgeImporter,
        KnowledgePack,
    )

    pack = KnowledgePack(
        version=1,
        topic="Repeated",
        topic_concept_id="concept:repeated",
        concepts=(
            Concept(
                id="concept:repeated",
                label="Repeated",
                description="Repeated evidence.",
                recorded_at=INSTANT,
                recorded_by="operator:test",
            ),
        ),
        evidence=(
            EvidenceProposal(
                key="second",
                source_content_sha256=source.content_sha256,
                exact="Repeated fact.",
                start=15,
                end=29,
            ),
        ),
        claims=(
            ClaimProposal(
                key="claim",
                subject="concept:repeated",
                predicate="ep:states",
                object="fact",
                stance="asserts",
                epistemic_status="observed",
                asserted_by="source:test",
                evidence_keys=("second",),
            ),
        ),
    )
    receipt = KnowledgeImporter(store=store, clock=lambda: INSTANT).import_pack(
        pack,
        imported_by="operator:test",
    )
    evidence = tuple(store.iter_records("evidence-fragment"))

    assert receipt.claim_ids
    assert evidence[0]["selector"]["start"] == 15
