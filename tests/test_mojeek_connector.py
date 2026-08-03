from pathlib import Path

import pytest

from research_agent.connectors.mojeek import MojeekDiscoveryConnector, MojeekError
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
        "exact_terms": ("knowledge graph", "ontology"),
        "match": TermMatch.ALL,
        "result_limit": 3,
        "page_limit": 1,
        "languages": ("en",),
    }
    values.update(updates)
    return DiscoveryRequest.model_validate(values)


def test_mojeek_fixture_is_normalized_without_credentials() -> None:
    body = Path("tests/fixtures/mojeek/search.json").read_bytes()
    transport = _FixtureTransport(body)
    connector = MojeekDiscoveryConnector(transport)

    pages = tuple(connector.discover(_request()))

    assert len(pages) == 1
    assert pages[0].rejected_count == 1
    assert len(pages[0].candidates) == 2
    assert pages[0].candidates[0].canonical_locator == ("https://example.org/research/ontology")
    assert pages[0].candidates[0].title == "Ontology & knowledge graphs"
    assert pages[0].response_sha256 is not None
    assert transport.requests == [
        {
            "q": '"knowledge graph" ontology',
            "fmt": "json",
            "s": "1",
            "t": "3",
            "lb": "EN",
            "lbb": "100",
            "date": "1",
            "cdate": "1",
            "dlen": "511",
        }
    ]
    assert "api_key" not in transport.requests[0]


def test_any_match_has_inspectable_deterministic_translation() -> None:
    connector = MojeekDiscoveryConnector(_FixtureTransport(b"{}"))

    normalized = connector.normalize_query(_request(match=TermMatch.ANY))

    assert normalized == 'q="knowledge graph" OR ontology;lb=EN;lbb=100'


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/",
        "http://10.0.0.1/",
        "https://user:password@example.org/",
        "javascript:alert(1)",
    ],
)
def test_non_public_or_non_http_result_locators_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        MojeekDiscoveryConnector._canonical_locator(url)


def test_api_error_does_not_echo_upstream_message() -> None:
    body = (
        b'{"response":{"status":"ERROR: key secret-value invalid","head":{"query":"x","start":1}}}'
    )
    connector = MojeekDiscoveryConnector(_FixtureTransport(body))

    with pytest.raises(MojeekError, match="API error") as caught:
        tuple(connector.discover(_request()))

    assert "secret-value" not in str(caught.value)
