from __future__ import annotations

import hashlib
import ipaddress
import re
import urllib.parse
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from research_agent.identifiers import doi_locator, normalize_doi, normalize_pmcid, normalize_pmid
from research_agent.models import StrictModel, content_id, utc_now
from research_agent.store import ImmutableStore
from research_agent.structure import AnchorKind, StructuralAnchor, StructuralDerivation


class CitationError(ValueError):
    pass


class IdentifierKind(StrEnum):
    DOI = "doi"
    PMID = "pmid"
    PMCID = "pmcid"
    ARXIV = "arxiv"
    URL = "url"


class ReferenceRelation(StrEnum):
    MENTIONS = "mentions"
    CITES = "cites"
    UPDATES = "updates"
    CORRECTS = "corrects"
    RETRACTS = "retracts"
    REVIEWS = "reviews"
    REPLIES_TO = "replies_to"


class ResearchIdentifier(StrictModel):
    id: str
    kind: IdentifierKind
    value: str = Field(min_length=1)
    canonical_locator: str


class BibliographicReference(StrictModel):
    id: str
    citation_derivation_id: str
    structural_anchor_id: str
    source_version_id: str
    source_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    identifier_id: str
    relation: ReferenceRelation
    signal: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    exact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def range_is_ordered(self) -> BibliographicReference:
        if self.end <= self.start:
            raise ValueError("reference end must follow its start")
        return self


class CitationDerivation(StrictModel):
    id: str
    structural_derivation_id: str
    source_version_id: str
    source_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    extractor_id: str
    extractor_version: str
    extracted_at: datetime
    identifier_ids: tuple[str, ...]
    reference_ids: tuple[str, ...]
    relation_counts: dict[str, int]

    @model_validator(mode="after")
    def indexes_are_unique(self) -> CitationDerivation:
        if len(set(self.identifier_ids)) != len(self.identifier_ids):
            raise ValueError("citation identifier index contains duplicates")
        if len(set(self.reference_ids)) != len(self.reference_ids):
            raise ValueError("citation reference index contains duplicates")
        if set(self.relation_counts) - {item.value for item in ReferenceRelation}:
            raise ValueError("citation derivation contains an unknown relation")
        if sum(self.relation_counts.values()) != len(self.reference_ids):
            raise ValueError("citation relation counts do not match reference index")
        return self


class CitationDerivationReceipt(StrictModel):
    citation_derivation_id: str
    research_identifier_ids: tuple[str, ...]
    bibliographic_reference_ids: tuple[str, ...]
    record_hashes: dict[str, tuple[str, ...]]


class DeterministicCitationExtractor:
    """Extract identifier occurrences without interpreting source instructions."""

    extractor_id = "extractor:deterministic-citations"
    version = "deterministic-citation-extractor/1"
    max_references = 100_000
    _doi = re.compile(r"(?<![A-Za-z0-9])(?:doi:\s*|https?://doi\.org/)?(10\.\d{4,9}/\S+)", re.I)
    _pmid = re.compile(r"\bPMID\s*:\s*([1-9][0-9]{0,8})\b", re.I)
    _pmcid = re.compile(r"\b(?:PMCID\s*:\s*)?(PMC[1-9][0-9]*)\b", re.I)
    _arxiv = re.compile(r"\barXiv\s*:\s*(\d{4}\.\d{4,5}(?:v[1-9][0-9]*)?)\b", re.I)
    _url = re.compile(r"https?://[^\s<>{}\[\]\"']+", re.I)
    _relation_signals = (
        (
            ReferenceRelation.RETRACTS,
            re.compile(r"\b(?:retracts?|retraction\s+(?:of|for))\b", re.I),
        ),
        (
            ReferenceRelation.CORRECTS,
            re.compile(r"\b(?:corrects?|correction\s+(?:of|to|for))\b", re.I),
        ),
        (ReferenceRelation.UPDATES, re.compile(r"\b(?:updates?|updated\s+version\s+of)\b", re.I)),
        (ReferenceRelation.REVIEWS, re.compile(r"\b(?:reviews?|review\s+of)\b", re.I)),
        (ReferenceRelation.REPLIES_TO, re.compile(r"\b(?:replies?\s+to|response\s+to)\b", re.I)),
    )

    def extract(
        self,
        text: str,
        *,
        structural_derivation: StructuralDerivation,
        anchors: tuple[StructuralAnchor, ...],
    ) -> tuple[
        CitationDerivation,
        tuple[ResearchIdentifier, ...],
        tuple[BibliographicReference, ...],
    ]:
        if hashlib.sha256(text.encode()).hexdigest() != structural_derivation.source_content_sha256:
            raise CitationError("citation source text does not match its content hash")
        if tuple(anchor.id for anchor in sorted(anchors, key=lambda item: item.ordinal)) != (
            structural_derivation.anchor_ids
        ):
            raise CitationError("structural derivation anchor index mismatch")
        derivation_fields = {
            "structural_derivation_id": structural_derivation.id,
            "source_version_id": structural_derivation.source_version_id,
            "source_content_sha256": structural_derivation.source_content_sha256,
            "extractor_id": self.extractor_id,
            "extractor_version": self.version,
            "extracted_at": structural_derivation.extracted_at,
        }
        derivation_id = content_id("citation-derivation", derivation_fields)
        occurrences = self._occurrences(text)
        if len(occurrences) > self.max_references:
            raise CitationError("document exceeds citation reference limit")
        identifiers: dict[str, ResearchIdentifier] = {}
        references: list[BibliographicReference] = []
        for start, end, kind, value, locator in occurrences:
            identifier_fields = {"kind": kind, "value": value, "canonical_locator": locator}
            identifier = ResearchIdentifier(
                id=content_id("research-identifier", identifier_fields),
                **identifier_fields,
            )
            identifiers[identifier.id] = identifier
            anchor = self._anchor_for_range(anchors, start, end)
            relation, signal = self._relation(text, anchors, anchor, start)
            reference_fields = {
                "citation_derivation_id": derivation_id,
                "structural_anchor_id": anchor.id,
                "source_version_id": structural_derivation.source_version_id,
                "source_content_sha256": structural_derivation.source_content_sha256,
                "identifier_id": identifier.id,
                "relation": relation,
                "signal": signal,
                "start": start,
                "end": end,
                "exact_sha256": hashlib.sha256(text[start:end].encode()).hexdigest(),
            }
            references.append(
                BibliographicReference(
                    id=content_id("bibliographic-reference", reference_fields),
                    **reference_fields,
                )
            )
        references.sort(key=lambda item: (item.start, item.end, item.identifier_id))
        counts = Counter(item.relation.value for item in references)
        derivation = CitationDerivation(
            id=derivation_id,
            **derivation_fields,
            identifier_ids=tuple(sorted(identifiers)),
            reference_ids=tuple(item.id for item in references),
            relation_counts=dict(sorted(counts.items())),
        )
        return derivation, tuple(identifiers[key] for key in sorted(identifiers)), tuple(references)

    def _occurrences(
        self,
        text: str,
    ) -> tuple[tuple[int, int, IdentifierKind, str, str], ...]:
        candidates: list[tuple[int, int, IdentifierKind, str, str]] = []
        protected: list[tuple[int, int]] = []
        for pattern, kind, normalizer, locator in (
            (self._doi, IdentifierKind.DOI, normalize_doi, doi_locator),
            (
                self._pmid,
                IdentifierKind.PMID,
                normalize_pmid,
                lambda value: f"https://pubmed.ncbi.nlm.nih.gov/{value}/",
            ),
            (
                self._pmcid,
                IdentifierKind.PMCID,
                normalize_pmcid,
                lambda value: f"https://www.ncbi.nlm.nih.gov/pmc/articles/{value}/",
            ),
            (
                self._arxiv,
                IdentifierKind.ARXIV,
                lambda value: value.casefold(),
                lambda value: f"https://arxiv.org/abs/{value}",
            ),
        ):
            for match in pattern.finditer(text):
                raw = self._trim_token(match.group(1))
                end = match.start(1) + len(raw)
                try:
                    value = normalizer(raw)
                except ValueError:
                    continue
                candidates.append((match.start(1), end, kind, value, locator(value)))
                protected.append((match.start(), match.end()))
        for match in self._url.finditer(text):
            raw = self._trim_token(match.group(0))
            end = match.start() + len(raw)
            if any(match.start() >= start and end <= stop for start, stop in protected):
                continue
            try:
                value = self._canonical_public_url(raw)
            except ValueError:
                continue
            candidates.append((match.start(), end, IdentifierKind.URL, value, value))
        return tuple(sorted(set(candidates), key=lambda item: (item[0], item[1], item[2].value)))

    @staticmethod
    def _trim_token(value: str) -> str:
        value = value.rstrip(".,;:!?'\">")
        for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
            while value.endswith(closing) and value.count(closing) > value.count(opening):
                value = value[:-1]
        return value

    @staticmethod
    def _canonical_public_url(value: str) -> str:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("reference URL must use HTTP(S)")
        if parsed.username or parsed.password:
            raise ValueError("reference URL must not contain credentials")
        hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
        if hostname == "localhost" or hostname.endswith(".local"):
            raise ValueError("reference URL must not be local")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError("reference URL must not use a non-public address")
        port = parsed.port
        netloc = hostname
        if port and not (
            (parsed.scheme.casefold() == "http" and port == 80)
            or (parsed.scheme.casefold() == "https" and port == 443)
        ):
            netloc = f"{hostname}:{port}"
        return urllib.parse.urlunsplit(
            (parsed.scheme.casefold(), netloc, parsed.path or "/", parsed.query, "")
        )

    @staticmethod
    def _anchor_for_range(
        anchors: tuple[StructuralAnchor, ...],
        start: int,
        end: int,
    ) -> StructuralAnchor:
        containers = [item for item in anchors if item.start <= start and item.end >= end]
        if not containers:
            raise CitationError("reference is not covered by a structural anchor")
        priority = {
            AnchorKind.FOOTNOTE: 0,
            AnchorKind.CAPTION: 1,
            AnchorKind.LIST_ITEM: 2,
            AnchorKind.PARAGRAPH: 3,
            AnchorKind.HEADING: 4,
            AnchorKind.SECTION: 5,
            AnchorKind.PAGE: 6,
            AnchorKind.DOCUMENT: 7,
        }
        return min(
            containers,
            key=lambda item: (priority[item.kind], item.end - item.start, item.ordinal),
        )

    def _relation(
        self,
        text: str,
        anchors: tuple[StructuralAnchor, ...],
        anchor: StructuralAnchor,
        start: int,
    ) -> tuple[ReferenceRelation, str]:
        context = text[max(anchor.start, start - 96) : start]
        signals: list[tuple[int, ReferenceRelation, str]] = []
        for relation, pattern in self._relation_signals:
            signals.extend(
                (match.start(), relation, match.group(0).casefold())
                for match in pattern.finditer(context)
            )
        signals.extend(
            (match.start(), ReferenceRelation.MENTIONS, match.group(0).casefold())
            for match in re.finditer(r"\bmentions?\b", context, re.I)
        )
        signals.extend(
            (match.start(), ReferenceRelation.CITES, match.group(0).casefold())
            for match in re.finditer(r"\b(?:cite[sd]?|citation)\b", context, re.I)
        )
        if signals:
            _, relation, signal = max(signals, key=lambda item: item[0])
            return relation, signal
        anchors_by_id = {item.id: item for item in anchors}
        ancestor: StructuralAnchor | None = anchor
        while ancestor is not None:
            label = (ancestor.label or "").casefold()
            if re.search(r"\b(?:references?|bibliography|works cited|sources)\b", label):
                return ReferenceRelation.CITES, "reference-section"
            ancestor = anchors_by_id.get(ancestor.parent_id or "")
        if "](" in context[-8:] or re.search(
            r"(?:^|\n)[ \t]*#{1,6}[ \t]+(?:references?|bibliography|works cited|sources)\b",
            context,
            re.I,
        ):
            return ReferenceRelation.CITES, "citation-syntax"
        return ReferenceRelation.MENTIONS, "identifier-occurrence"


def normalize_research_identifier(
    kind: IdentifierKind,
    value: str,
) -> tuple[str, str]:
    if kind is IdentifierKind.DOI:
        normalized = normalize_doi(value)
        return normalized, doi_locator(normalized)
    if kind is IdentifierKind.PMID:
        normalized = normalize_pmid(value)
        return normalized, f"https://pubmed.ncbi.nlm.nih.gov/{normalized}/"
    if kind is IdentifierKind.PMCID:
        normalized = normalize_pmcid(value)
        return normalized, f"https://www.ncbi.nlm.nih.gov/pmc/articles/{normalized}/"
    if kind is IdentifierKind.ARXIV:
        normalized = value.strip().casefold()
        if not re.fullmatch(r"\d{4}\.\d{4,5}(?:v[1-9][0-9]*)?", normalized):
            raise ValueError("invalid arXiv identifier")
        return normalized, f"https://arxiv.org/abs/{normalized}"
    normalized = DeterministicCitationExtractor._canonical_public_url(value)
    return normalized, normalized


class CitationDocumentManager:
    def __init__(
        self,
        *,
        store: ImmutableStore,
        extractor: DeterministicCitationExtractor | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.extractor = extractor or DeterministicCitationExtractor()
        self.clock = clock

    def derive(
        self,
        text: str,
        *,
        structural_derivation: StructuralDerivation,
        anchors: tuple[StructuralAnchor, ...],
    ) -> CitationDerivationReceipt:
        derivation, identifiers, references = self.extractor.extract(
            text,
            structural_derivation=structural_derivation,
            anchors=anchors,
        )
        values: dict[str, tuple[StrictModel, ...]] = {
            "research-identifier": identifiers,
            "bibliographic-reference": references,
            "citation-derivation": (derivation,),
        }
        hashes = {
            kind: tuple(self.store.put_record(kind, item) for item in records)
            for kind, records in values.items()
        }
        return CitationDerivationReceipt(
            citation_derivation_id=derivation.id,
            research_identifier_ids=derivation.identifier_ids,
            bibliographic_reference_ids=derivation.reference_ids,
            record_hashes=hashes,
        )

    def derive_stored(self, structural_derivation_id: str) -> CitationDerivationReceipt:
        self.store.initialize()
        matches = [
            StructuralDerivation.model_validate(value)
            for value in self.store.iter_records("structural-derivation")
            if value.get("id") == structural_derivation_id
        ]
        if len(matches) != 1:
            raise CitationError("structural derivation does not exist or is ambiguous")
        derivation = matches[0]
        anchors = tuple(
            sorted(
                (
                    StructuralAnchor.model_validate(value)
                    for value in self.store.iter_records("structural-anchor")
                    if value.get("structural_derivation_id") == derivation.id
                ),
                key=lambda item: item.ordinal,
            )
        )
        text = self.store.read_blob(derivation.source_content_sha256).decode(
            "utf-8",
            errors="strict",
        )
        return self.derive(text, structural_derivation=derivation, anchors=anchors)
