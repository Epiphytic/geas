from pathlib import Path

import pytest

from research_agent.connectors.europe_pmc import (
    EuropePmcDiscoveryConnector,
    EuropePmcError,
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


def test_europe_pmc_lite_fixture_normalizes_bibliographic_metadata() -> None:
    transport = _FixtureTransport(
        Path("tests/fixtures/europe_pmc/search.json").read_bytes()
    )
    connector = EuropePmcDiscoveryConnector(transport)

    pages = tuple(connector.discover(_request()))

    assert len(pages) == 1
    assert pages[0].rejected_count == 0
    assert len(pages[0].candidates) == 2
    work = pages[0].candidates[0]
    assert work.canonical_locator == "https://doi.org/10.1002/14651858.cd010856.pub3"
    assert work.authors == ("Iheozor-Ejiofor Z", "Walsh T")
    assert work.known_entity_ids == (
        "europe-pmc:MED:25472792",
        "doi:10.1002/14651858.cd010856.pub3",
        "pmid:25472792",
        "pmcid:PMC123456",
        "issn:1469-493X",
    )
    assert work.metadata["is_open_access"] is True
    assert transport.requests == [
        {
            "query": '"community water fluoridation" OR "neurodevelopment"',
            "format": "json",
            "resultType": "lite",
            "pageSize": "10",
            "cursorMark": "*",
        }
    ]
    partial = pages[0].candidates[1]
    assert partial.known_entity_ids == ("europe-pmc:MED:valid-record",)
    assert partial.published_at is None


def test_europe_pmc_query_syntax_is_deterministic_and_inert() -> None:
    connector = EuropePmcDiscoveryConnector(_FixtureTransport(b"{}"))

    assert connector.normalize_query(
        _request(match=TermMatch.ALL, exact_terms=('safe" OR unsafe', "dental caries"))
    ) == (
        'query="safe\\" OR unsafe" AND "dental caries";'
        "cursorMark=*;resultType=lite"
    )


def test_europe_pmc_error_does_not_repeat_upstream_content() -> None:
    connector = EuropePmcDiscoveryConnector(
        _FixtureTransport(b'{"error":"secret source content"}')
    )

    with pytest.raises(EuropePmcError, match="invalid response") as caught:
        tuple(connector.discover(_request()))

    assert "secret" not in str(caught.value)
