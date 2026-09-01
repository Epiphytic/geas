from pathlib import Path

import pytest

from research_agent.connectors import LocalFileConnector
from research_agent.discovery import AcquisitionRequest, DiscoveryRequest, TermMatch


def test_local_connector_discovers_in_stable_rank_order(tmp_path: Path) -> None:
    (tmp_path / "b.txt").write_text("ontology and knowledge graph")
    (tmp_path / "a.txt").write_text("ontology")
    connector = LocalFileConnector([tmp_path])
    pages = tuple(
        connector.discover(
            DiscoveryRequest(
                query_plan_id="query-plan:1",
                exact_terms=("ontology", "knowledge graph"),
                match=TermMatch.ANY,
                result_limit=10,
                page_limit=2,
                languages=("en",),
            )
        )
    )

    candidates = tuple(candidate for page in pages for candidate in page.candidates)
    assert [candidate.title for candidate in candidates] == ["b.txt", "a.txt"]


def test_local_connector_rejects_acquisition_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("untrusted instructions")
    connector = LocalFileConnector([root])

    with pytest.raises(ValueError, match="escapes configured roots"):
        connector.acquire(
            AcquisitionRequest(
                discovery_hit_id="hit:1",
                locator=outside.as_uri(),
                max_content_bytes=1_000,
            )
        )


def test_local_connector_acquires_native_file_uri_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "fixture with spaces.txt"
    source.write_bytes(b"portable local fixture")
    connector = LocalFileConnector([tmp_path])

    result = connector.acquire(
        AcquisitionRequest(
            discovery_hit_id="hit:1",
            locator=source.as_uri(),
            max_content_bytes=1_000,
        )
    )

    assert result.content == b"portable local fixture"


@pytest.mark.parametrize("suffix", ("?query=unsafe", "#fragment"))
def test_local_connector_rejects_file_uri_query_or_fragment(
    tmp_path: Path,
    suffix: str,
) -> None:
    source = tmp_path / "fixture.txt"
    source.write_text("fixture")
    connector = LocalFileConnector([tmp_path])

    with pytest.raises(ValueError, match="local file URIs"):
        connector.acquire(
            AcquisitionRequest(
                discovery_hit_id="hit:1",
                locator=source.as_uri() + suffix,
                max_content_bytes=1_000,
            )
        )


def test_local_discovery_does_not_follow_symlink_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("ontology secret")
    (root / "escape.txt").symlink_to(outside)
    connector = LocalFileConnector([root])

    pages = tuple(
        connector.discover(
            DiscoveryRequest(
                query_plan_id="query-plan:1",
                exact_terms=("ontology",),
                match=TermMatch.ANY,
                result_limit=10,
                page_limit=2,
                languages=("en",),
            )
        )
    )
    assert not pages
