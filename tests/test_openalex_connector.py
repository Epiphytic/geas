from pathlib import Path

import pytest

from research_agent.budget import BudgetPolicy, UsageLedger
from research_agent.connectors.openalex import (
    OpenAlexDiscoveryConnector,
    OpenAlexError,
)
from research_agent.discovery import DiscoveryRequest, TermMatch


class _FixtureTransport:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.requests: list[dict[str, str]] = []

    def request(self, parameters: dict[str, str]) -> bytes:
        self.requests.append(dict(parameters))
        return self.body


def _request(**updates: object) -> DiscoveryRequest:
    values: dict[str, object] = {
        "query_plan_id": "query-plan:fixture",
        "exact_terms": ("community water fluoridation", "neurodevelopment"),
        "match": TermMatch.ANY,
        "result_limit": 10,
        "page_limit": 1,
        "languages": ("en",),
    }
    values.update(updates)
    return DiscoveryRequest.model_validate(values)


def test_openalex_fixture_preserves_open_metadata_and_reported_cost() -> None:
    transport = _FixtureTransport(Path("tests/fixtures/openalex/search.json").read_bytes())
    connector = OpenAlexDiscoveryConnector(transport)

    pages = tuple(connector.discover(_request()))

    assert len(pages) == 1
    assert pages[0].rejected_count == 1
    assert pages[0].reported_cost_microusd == 1000
    assert len(pages[0].candidates) == 1
    work = pages[0].candidates[0]
    assert work.upstream_id == "openalex:W4401234567"
    assert work.canonical_locator == "https://doi.org/10.1002/14651858.cd010856.pub3"
    assert work.authors == ("Zipporah Iheozor-Ejiofor", "Tanya Walsh")
    assert work.publisher == "Cochrane Database of Systematic Reviews"
    assert work.known_entity_ids == (
        "openalex:W4401234567",
        "doi:10.1002/14651858.cd010856.pub3",
    )
    assert work.metadata == {
        "cited_by_count": 24,
        "doi": "10.1002/14651858.cd010856.pub3",
        "is_open_access": True,
        "is_retracted": False,
        "location_license": "cc-by",
        "open_access_status": "gold",
        "openalex_id": "W4401234567",
        "referenced_works_count": 157,
        "work_type": "review",
    }
    assert transport.requests[0] == {
        "search": '"community water fluoridation" OR "neurodevelopment"',
        "per_page": "10",
        "cursor": "*",
        "select": connector._select,
    }
    assert "api_key" not in transport.requests[0]


def test_openalex_all_match_translation_is_inspectable() -> None:
    connector = OpenAlexDiscoveryConnector(_FixtureTransport(b"{}"))

    assert (
        connector.normalize_query(_request(match=TermMatch.ALL))
        == 'search="community water fluoridation" AND "neurodevelopment";cursor=*'
    )


def test_openalex_query_language_characters_remain_inside_phrase() -> None:
    connector = OpenAlexDiscoveryConnector(_FixtureTransport(b"{}"))

    assert connector._query_text(_request(exact_terms=('safe" OR unsafe',))) == (
        '"safe\\" OR unsafe"'
    )


@pytest.mark.parametrize("cost", [-1, float("inf"), float("nan")])
def test_invalid_provider_cost_fails_closed(cost: float) -> None:
    with pytest.raises(OpenAlexError, match="invalid cost"):
        OpenAlexDiscoveryConnector._cost_microusd(cost)


def test_api_error_does_not_repeat_upstream_content() -> None:
    connector = OpenAlexDiscoveryConnector(
        _FixtureTransport(b'{"error":"secret-value","message":"bad key"}')
    )

    with pytest.raises(OpenAlexError, match="invalid response") as caught:
        tuple(connector.discover(_request()))

    assert "secret-value" not in str(caught.value)


def test_openalex_connector_settles_transactional_search_budget(
    tmp_path: Path,
) -> None:
    connector = OpenAlexDiscoveryConnector(
        _FixtureTransport(Path("tests/fixtures/openalex/search.json").read_bytes()),
        usage_ledger=UsageLedger(tmp_path / "usage.sqlite"),
        budget_policy=BudgetPolicy.from_yaml(Path("config/budget-policy.yaml")),
        run_id="run:fixture",
    )

    pages = tuple(connector.discover(_request()))

    assert pages[0].reported_cost_microusd == 1_000
