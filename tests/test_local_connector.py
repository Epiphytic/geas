import hashlib
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


def test_hash_pinned_fixture_checkout_preserves_canonical_lf_bytes() -> None:
    corpus = Path("tests/fixtures/fluoridation_corpus")
    expected = {
        "01_cdc.md": "e779ad7566105a7cdc25b104871cce0927a469cf53add1f852757d1b913e868b",
        "02_cochrane.md": "89091affcd36b4dcd67da90d7eaa5be9347ad173cbf4ef6325284192e6f97c31",
        "03_ntp.md": "2d5d8d7f65cb132b0526b7395ac231674cb9ca869a4dc7be5ade527653cd69b6",
        "04_epa.md": "a508269dfbfcfb2f6a4804fbc01a5035614f6b6bcaa7b1d888ef4b6e663bbb8d",
        "99_poisoned_marketing.md": (
            "1cfcb858a1d0bf42162e356df39f4e5c9cc1acd4d49893f291ea9d236602e2a3"
        ),
    }

    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(corpus.glob("*.md"))
    } == expected


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
