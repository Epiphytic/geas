from __future__ import annotations

import re
import urllib.parse

_DOI = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)


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
