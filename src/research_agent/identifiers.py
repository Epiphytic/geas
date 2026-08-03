from __future__ import annotations

import re
import urllib.parse

_DOI = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
_PMID = re.compile(r"^[1-9][0-9]{0,8}$")
_PMCID = re.compile(r"^PMC[1-9][0-9]*$", re.IGNORECASE)
_ROR = re.compile(r"^0[a-hj-km-np-tv-z0-9]{6}[0-9]{2}$")


def normalize_doi(value: str) -> str:
    normalized = urllib.parse.unquote(value.strip())
    lowered = normalized.casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if lowered.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    normalized = normalized.strip().rstrip(".,;")
    if not _DOI.fullmatch(normalized) or any(character.isspace() for character in normalized):
        raise ValueError("invalid DOI")
    return normalized.casefold()


def doi_locator(doi: str) -> str:
    normalized = normalize_doi(doi)
    return f"https://doi.org/{urllib.parse.quote(normalized, safe='/():._-;')}"


def normalize_pmid(value: str) -> str:
    normalized = value.strip()
    for prefix in ("https://pubmed.ncbi.nlm.nih.gov/", "pmid:"):
        if normalized.casefold().startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    normalized = normalized.strip().rstrip("/")
    if not _PMID.fullmatch(normalized):
        raise ValueError("invalid PMID")
    return normalized


def normalize_pmcid(value: str) -> str:
    normalized = value.strip()
    for prefix in (
        "https://europepmc.org/article/PMC/",
        "https://www.ncbi.nlm.nih.gov/pmc/articles/",
        "pmcid:",
    ):
        if normalized.casefold().startswith(prefix.casefold()):
            normalized = normalized[len(prefix) :]
            break
    normalized = normalized.strip().rstrip("/")
    if not _PMCID.fullmatch(normalized):
        raise ValueError("invalid PMCID")
    return normalized.upper()


def normalize_orcid(value: str) -> str:
    normalized = value.strip()
    for prefix in ("https://orcid.org/", "http://orcid.org/", "orcid:"):
        if normalized.casefold().startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    compact = normalized.replace("-", "").upper()
    if not re.fullmatch(r"[0-9]{15}[0-9X]", compact):
        raise ValueError("invalid ORCID")
    total = 0
    for character in compact[:15]:
        total = (total + int(character)) * 2
    expected = (12 - (total % 11)) % 11
    check = "X" if expected == 10 else str(expected)
    if compact[-1] != check:
        raise ValueError("invalid ORCID checksum")
    return "-".join((compact[:4], compact[4:8], compact[8:12], compact[12:]))


def normalize_ror(value: str) -> str:
    normalized = value.strip().casefold()
    for prefix in ("https://ror.org/", "http://ror.org/", "ror.org/", "ror:"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    normalized = normalized.rstrip("/")
    if not _ROR.fullmatch(normalized):
        raise ValueError("invalid ROR")
    return normalized


def normalize_issn(value: str) -> str:
    compact = value.strip().replace("-", "").upper()
    if not re.fullmatch(r"[0-9]{7}[0-9X]", compact):
        raise ValueError("invalid ISSN")
    total = sum(
        int(character) * weight
        for character, weight in zip(compact[:7], range(8, 1, -1), strict=True)
    )
    remainder = (11 - total % 11) % 11
    check = "X" if remainder == 10 else str(remainder)
    if compact[-1] != check:
        raise ValueError("invalid ISSN checksum")
    return f"{compact[:4]}-{compact[4:]}"
