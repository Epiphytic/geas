from pathlib import Path

import pytest
from pydantic import ValidationError

from research_agent.operator_policy import ResearchPolicy


def test_checked_in_policy_prefers_mojeek_and_open_acquisition() -> None:
    policy = ResearchPolicy.from_yaml(Path("config/research-policy.yaml"))

    mojeek = policy.provider("connector:mojeek")
    brave = policy.provider("connector:brave-search")
    assert mojeek.enabled
    assert mojeek.priority == 1
    assert not mojeek.persist_normalized_results
    assert not brave.enabled
    assert policy.open_source_acquisition_order[:3] == (
        "official_api",
        "domain_index",
        "open_repository",
    )
    assert not policy.general_search_results_are_evidence


def test_persistence_requires_confirmed_storage_rights() -> None:
    with pytest.raises(ValidationError, match="confirmed storage rights"):
        ResearchPolicy.model_validate(
            {
                "version": 1,
                "open_source_acquisition_order": ["official_api"],
                "general_search_results_are_evidence": False,
                "general_search_providers": [
                    {
                        "connector_id": "connector:mojeek",
                        "enabled": True,
                        "priority": 1,
                        "credential_env": "MOJEEK_API_KEY",
                        "storage_rights": "unconfirmed",
                        "persist_normalized_results": True,
                        "raw_response_retention_days": 0,
                        "max_requests_per_run": 50,
                        "max_requests_per_month": 5000,
                        "monthly_cost_ceiling_usd": 25,
                    }
                ],
            }
        )
