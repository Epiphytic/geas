from pathlib import Path

import pytest

from research_agent.connectors.crossref import (
    CrossrefDiscoveryConnector,
    CrossrefError,
)
from research_agent.discovery import DiscoveryRequest, TermMatch
from research_agent.identifiers import doi_locator, normalize_doi


class _FixtureTransport:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.requests: list[dict[str, str]] = []

    def request(self, parameters: dict[str, str]) -> bytes:
        self.requests.append(dict(parameters))
        return self.body


def _request() -> DiscoveryRequest:
    return DiscoveryRequest(
        query_plan_id="query-plan:fixture",
        exact_terms=("community water fluoridation", "dental caries"),
        match=TermMatch.ALL,
        result_limit=10,
        page_limit=1,
        languages=("en",),
    )


def test_crossref_fixture_preserves_doi_authorship_and_date() -> None:
    transport = _FixtureTransport(Path("tests/fixtures/crossref/search.json").read_bytes())
    connector = CrossrefDiscoveryConnector(transport)

    pages = tuple(connector.discover(_request()))

    assert len(pages) == 1
    assert pages[0].rejected_count == 1
    assert len(pages[0].candidates) == 1
    work = pages[0].candidates[0]
    assert work.upstream_id == "doi:10.1002/14651858.cd010856.pub3"
    assert work.canonical_locator == "https://doi.org/10.1002/14651858.cd010856.pub3"
    assert work.authors == ("Zipporah Iheozor-Ejiofor", "Tanya Walsh")
    assert work.publisher == "Wiley"
    assert work.published_at is not None
    assert transport.requests[0] == {
        "query.bibliographic": "community water fluoridation dental caries",
        "rows": "10",
        "cursor": "*",
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("doi:10.1234/ABC.1", "10.1234/abc.1"),
        ("https://doi.org/10.1002/example", "10.1002/example"),
        ("10.55555/example.", "10.55555/example"),
    ],
)
def test_doi_normalization(value: str, expected: str) -> None:
    assert normalize_doi(value) == expected
    assert doi_locator(value).startswith("https://doi.org/10.")


def test_crossref_error_does_not_repeat_upstream_content() -> None:
    connector = CrossrefDiscoveryConnector(_FixtureTransport(b'{"status":"secret error"}'))

    with pytest.raises(CrossrefError, match="invalid response") as caught:
        tuple(connector.discover(_request()))

    assert "secret" not in str(caught.value)
