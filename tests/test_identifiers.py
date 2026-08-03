from collections.abc import Callable

import pytest

from research_agent.identifiers import (
    normalize_issn,
    normalize_orcid,
    normalize_pmcid,
    normalize_pmid,
    normalize_ror,
)


def test_normalizes_scholarly_identifiers() -> None:
    assert normalize_pmid("PMID: 29867326") == "29867326"
    assert normalize_pmcid("https://europepmc.org/article/PMC/PMC123456/") == "PMC123456"
    assert normalize_orcid("https://orcid.org/0000-0002-1825-0097") == (
        "0000-0002-1825-0097"
    )
    assert normalize_ror("https://ror.org/03yrm5c26") == "03yrm5c26"
    assert normalize_issn("2049-3630") == "2049-3630"


@pytest.mark.parametrize(
    ("normalizer", "value"),
    [
        (normalize_pmid, "0"),
        (normalize_pmcid, "123456"),
        (normalize_orcid, "0000-0002-1825-0098"),
        (normalize_ror, "03yrm5i26"),
        (normalize_issn, "2049-3631"),
    ],
)
def test_invalid_scholarly_identifiers_fail_closed(
    normalizer: Callable[[str], str],
    value: str,
) -> None:
    with pytest.raises(ValueError):
        normalizer(value)
