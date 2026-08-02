from pathlib import Path

import pytest

from research_agent.connectors import LocalFileConnector
from research_agent.discovery import CompilerIdentity, ConnectorCapability, SourceClass
from research_agent.planning import (
    ConceptVocabulary,
    ModelQueryCompiler,
    QueryPlanValidator,
    QueryPolicy,
    QueryProposal,
    deterministic_proposal,
)


class _FakeClient:
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        max_output_tokens: int | None = None,
    ) -> dict[str, object]:
        return self.result


def _validator(tmp_path: Path, *, policy: QueryPolicy | None = None) -> QueryPlanValidator:
    connector = LocalFileConnector([tmp_path])
    return QueryPlanValidator(
        vocabulary=ConceptVocabulary(
            concepts={"concept:ontology": ("ontology", "knowledge graph")}
        ),
        manifests={connector.manifest.id: connector.manifest},
        policy=policy,
    )


def test_plan_is_reproducible_and_expands_only_controlled_synonyms(tmp_path: Path) -> None:
    proposal = deterministic_proposal(
        "How should an ontology be maintained?",
        connector_id="connector:local-file",
        concept_ids=("concept:ontology",),
    )
    compiler = CompilerIdentity(id="compiler:test", version="1")

    first = _validator(tmp_path).validate(proposal, compiler=compiler)
    second = _validator(tmp_path).validate(proposal, compiler=compiler)

    assert first == second
    assert first.id.startswith("query-plan:sha256:")
    assert "knowledge graph" in first.exact_terms


def test_unknown_concept_and_connector_capability_are_rejected(tmp_path: Path) -> None:
    base = deterministic_proposal(
        "ontology maintenance",
        connector_id="connector:local-file",
    )
    compiler = CompilerIdentity(id="compiler:test", version="1")

    with pytest.raises(ValueError, match="unknown ontology concepts"):
        _validator(tmp_path).validate(
            base.model_copy(update={"concept_ids": ("concept:invented",)}),
            compiler=compiler,
        )
    with pytest.raises(ValueError, match="do not declare capabilities"):
        _validator(tmp_path).validate(
            base.model_copy(
                update={
                    "capabilities": frozenset({*base.capabilities, ConnectorCapability.ARCHIVE})
                }
            ),
            compiler=compiler,
        )
    with pytest.raises(ValueError, match="do not support source classes"):
        _validator(tmp_path).validate(
            base.model_copy(update={"source_classes": frozenset({SourceClass.WEB})}),
            compiler=compiler,
        )


def test_budgets_are_clamped_and_high_effective_budget_requires_approval(tmp_path: Path) -> None:
    proposal = QueryProposal(
        question="ontology",
        exact_terms=("ontology",),
        connector_ids=("connector:local-file",),
        capabilities=frozenset({ConnectorCapability.DISCOVERY}),
        result_limit=500,
        page_limit=500,
        max_content_bytes=500_000_000,
        stop_after_empty_pages=10,
    )
    validator = _validator(
        tmp_path,
        policy=QueryPolicy(
            max_results=40,
            max_pages=3,
            max_content_bytes=1_000,
            max_empty_pages=1,
            require_human_approval_above_results=40,
        ),
    )
    plan = validator.validate(
        proposal,
        compiler=CompilerIdentity(id="compiler:test", version="1"),
    )

    assert plan.result_limit == 40
    assert plan.page_limit == 3
    assert plan.max_content_bytes == 1_000
    assert set(plan.lossy_clauses) == {
        "max_content_bytes",
        "page_limit",
        "result_limit",
        "stop_after_empty_pages",
    }


def test_unapproved_high_effective_budget_is_rejected(tmp_path: Path) -> None:
    proposal = deterministic_proposal(
        "ontology",
        connector_id="connector:local-file",
    ).model_copy(update={"result_limit": 60})

    with pytest.raises(ValueError, match="requires human approval"):
        _validator(tmp_path).validate(
            proposal,
            compiler=CompilerIdentity(id="compiler:test", version="1"),
        )


def test_model_compiler_output_cannot_add_destination_or_tool_fields(tmp_path: Path) -> None:
    compiler = ModelQueryCompiler(
        _FakeClient(
            {
                "question": "ontology",
                "exact_terms": ["ontology"],
                "connector_ids": ["connector:local-file"],
                "capabilities": ["discovery"],
                "destination": "https://attacker.invalid/",
            }
        )
    )

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        compiler.compile(
            "ontology",
            vocabulary=ConceptVocabulary(concepts={}),
            manifests={
                "connector:local-file": LocalFileConnector([tmp_path]).manifest,
            },
        )


def test_model_compiler_cannot_rewrite_trusted_question(tmp_path: Path) -> None:
    compiler = ModelQueryCompiler(
        _FakeClient(
            {
                "question": "ignore the original",
                "exact_terms": ["ontology"],
                "connector_ids": ["connector:local-file"],
                "capabilities": ["discovery"],
            }
        )
    )
    proposal = compiler.compile(
        "trusted ontology question",
        vocabulary=ConceptVocabulary(concepts={}),
        manifests={"connector:local-file": LocalFileConnector([tmp_path]).manifest},
    )

    assert proposal.question == "trusted ontology question"
