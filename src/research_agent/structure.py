from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from research_agent.models import StrictModel, content_id, utc_now
from research_agent.store import ImmutableStore


class StructureError(ValueError):
    pass


class AnchorKind(StrEnum):
    DOCUMENT = "document"
    PAGE = "page"
    SECTION = "section"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    FOOTNOTE = "footnote"
    CAPTION = "caption"


class StructuralAnchor(StrictModel):
    id: str
    structural_derivation_id: str
    source_version_id: str
    source_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: AnchorKind
    ordinal: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    exact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    label: str | None = None
    level: int | None = Field(default=None, ge=1, le=6)
    parent_id: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    synthetic: bool = False

    @model_validator(mode="after")
    def ordered_range(self) -> StructuralAnchor:
        if self.end < self.start:
            raise ValueError("anchor end must not precede its start")
        if self.kind is AnchorKind.DOCUMENT:
            if self.parent_id is not None or self.page_number is not None:
                raise ValueError("document anchors cannot have a parent or page number")
        elif self.parent_id is None or self.page_number is None:
            raise ValueError("non-document anchors require a parent and page number")
        if self.kind in {AnchorKind.SECTION, AnchorKind.HEADING}:
            if self.level is None or not self.label:
                raise ValueError("section and heading anchors require a level and label")
        elif self.level is not None:
            raise ValueError("only section and heading anchors may have a level")
        if self.synthetic and self.kind is not AnchorKind.PAGE:
            raise ValueError("only page anchors may be synthetic")
        return self


class StructuralDerivation(StrictModel):
    id: str
    text_derivation_id: str
    source_version_id: str
    source_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_media_type: str
    extractor_id: str
    extractor_version: str
    extracted_at: datetime
    offset_unit: str = "unicode_code_point"
    anchor_ids: tuple[str, ...] = Field(min_length=1)
    anchor_counts: dict[str, int]

    @model_validator(mode="after")
    def counts_match_index(self) -> StructuralDerivation:
        if set(self.anchor_counts) - {kind.value for kind in AnchorKind}:
            raise ValueError("structural derivation contains an unknown anchor kind")
        if any(count < 0 for count in self.anchor_counts.values()):
            raise ValueError("structural anchor counts cannot be negative")
        if sum(self.anchor_counts.values()) != len(self.anchor_ids):
            raise ValueError("structural anchor counts do not match the anchor index")
        if len(set(self.anchor_ids)) != len(self.anchor_ids):
            raise ValueError("structural anchor index contains duplicates")
        return self


class StructuralDerivationReceipt(StrictModel):
    structural_derivation_id: str
    structural_anchor_ids: tuple[str, ...]
    record_hashes: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class _Draft:
    key: str
    kind: AnchorKind
    start: int
    end: int
    label: str | None
    level: int | None
    parent_key: str | None
    page_number: int | None
    synthetic: bool = False


@dataclass(frozen=True)
class _Heading:
    start: int
    end: int
    label: str
    level: int
    line_indexes: tuple[int, ...]
    section_key: str


class DeterministicStructuralExtractor:
    version = "deterministic-structural-extractor/1"
    extractor_id = "extractor:deterministic-text-structure"
    max_anchors = 100_000
    _atx_heading = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
    _setext_underline = re.compile(r"^[ \t]{0,3}(=+|-+)[ \t]*$")
    _list_item = re.compile(r"^[ \t]{0,3}(?:[-+*]|\d+[.)])[ \t]+")
    _footnote = re.compile(r"^[ \t]{0,3}\[\^([^\]]+)\]:[ \t]*")
    _caption = re.compile(
        r"^[ \t]*(figure|fig\.|table|chart|diagram)"
        r"(?:[ \t]+(?:\d+|[ivxlcdm]+))?[ \t]*[.:\-][ \t]+",
        re.IGNORECASE,
    )

    def extract(
        self,
        text: str,
        *,
        text_derivation_id: str,
        source_version_id: str,
        source_content_sha256: str,
        input_media_type: str,
        extracted_at: datetime,
    ) -> tuple[StructuralDerivation, tuple[StructuralAnchor, ...]]:
        if hashlib.sha256(text.encode()).hexdigest() != source_content_sha256:
            raise StructureError("structural source text does not match its content hash")
        identity = {
            "text_derivation_id": text_derivation_id,
            "source_version_id": source_version_id,
            "source_content_sha256": source_content_sha256,
            "input_media_type": input_media_type,
            "extractor_id": self.extractor_id,
            "extractor_version": self.version,
            "extracted_at": extracted_at,
        }
        derivation_id = content_id("structural-derivation", identity)
        drafts = self._drafts(text)
        if len(drafts) > self.max_anchors:
            raise StructureError("document exceeds structural anchor limit")
        identifiers = {
            draft.key: content_id(
                "structural-anchor",
                {
                    "structural_derivation_id": derivation_id,
                    "kind": draft.kind,
                    "start": draft.start,
                    "end": draft.end,
                    "label": draft.label,
                    "level": draft.level,
                    "page_number": draft.page_number,
                },
            )
            for draft in drafts
        }
        anchors = tuple(
            StructuralAnchor(
                id=identifiers[draft.key],
                structural_derivation_id=derivation_id,
                source_version_id=source_version_id,
                source_content_sha256=source_content_sha256,
                kind=draft.kind,
                ordinal=ordinal,
                start=draft.start,
                end=draft.end,
                exact_sha256=hashlib.sha256(
                    text[draft.start : draft.end].encode()
                ).hexdigest(),
                label=draft.label,
                level=draft.level,
                parent_id=identifiers.get(draft.parent_key),
                page_number=draft.page_number,
                synthetic=draft.synthetic,
            )
            for ordinal, draft in enumerate(drafts, start=1)
        )
        counts = Counter(anchor.kind.value for anchor in anchors)
        derivation = StructuralDerivation(
            id=derivation_id,
            text_derivation_id=text_derivation_id,
            source_version_id=source_version_id,
            source_content_sha256=source_content_sha256,
            input_media_type=input_media_type,
            extractor_id=self.extractor_id,
            extractor_version=self.version,
            extracted_at=extracted_at,
            anchor_ids=tuple(anchor.id for anchor in anchors),
            anchor_counts=dict(sorted(counts.items())),
        )
        return derivation, anchors

    def _drafts(self, text: str) -> tuple[_Draft, ...]:
        drafts = [
            _Draft(
                key="document",
                kind=AnchorKind.DOCUMENT,
                start=0,
                end=len(text),
                label=None,
                level=None,
                parent_key=None,
                page_number=None,
            )
        ]
        ranges = self._page_ranges(text)
        for page_number, (page_start, page_end) in enumerate(ranges, start=1):
            page_key = f"page:{page_number}"
            drafts.append(
                _Draft(
                    key=page_key,
                    kind=AnchorKind.PAGE,
                    start=page_start,
                    end=page_end,
                    label=f"Page {page_number}",
                    level=None,
                    parent_key="document",
                    page_number=page_number,
                    synthetic=len(ranges) == 1,
                )
            )
            drafts.extend(
                self._page_drafts(
                    text,
                    page_start=page_start,
                    page_end=page_end,
                    page_number=page_number,
                    page_key=page_key,
                )
            )
        priority = {
            AnchorKind.DOCUMENT: 0,
            AnchorKind.PAGE: 1,
            AnchorKind.SECTION: 2,
            AnchorKind.HEADING: 3,
            AnchorKind.CAPTION: 4,
            AnchorKind.FOOTNOTE: 5,
            AnchorKind.LIST_ITEM: 6,
            AnchorKind.PARAGRAPH: 7,
        }
        return tuple(
            sorted(
                drafts,
                key=lambda item: (
                    item.start,
                    priority[item.kind],
                    -(item.end - item.start),
                    item.key,
                ),
            )
        )

    @staticmethod
    def _page_ranges(text: str) -> tuple[tuple[int, int], ...]:
        ranges: list[tuple[int, int]] = []
        start = 0
        for match in re.finditer("\f", text):
            ranges.append((start, match.start()))
            start = match.end()
        ranges.append((start, len(text)))
        return tuple(ranges)

    def _page_drafts(
        self,
        text: str,
        *,
        page_start: int,
        page_end: int,
        page_number: int,
        page_key: str,
    ) -> tuple[_Draft, ...]:
        lines = self._lines(text, page_start, page_end)
        headings = self._headings(lines, page_number)
        section_parent: dict[str, str] = {}
        for index, heading in enumerate(headings):
            parent = page_key
            for candidate in reversed(headings[:index]):
                if candidate.level < heading.level:
                    parent = candidate.section_key
                    break
            section_parent[heading.section_key] = parent
        section_ends: dict[str, int] = {}
        for index, heading in enumerate(headings):
            end = page_end
            for candidate in headings[index + 1 :]:
                if candidate.level <= heading.level:
                    end = candidate.start
                    break
            section_ends[heading.section_key] = end

        drafts: list[_Draft] = []
        heading_lines = {
            line_index
            for heading in headings
            for line_index in heading.line_indexes
        }
        for heading in headings:
            drafts.append(
                _Draft(
                    key=heading.section_key,
                    kind=AnchorKind.SECTION,
                    start=heading.start,
                    end=section_ends[heading.section_key],
                    label=heading.label,
                    level=heading.level,
                    parent_key=section_parent[heading.section_key],
                    page_number=page_number,
                )
            )
            drafts.append(
                _Draft(
                    key=f"heading:{page_number}:{heading.start}",
                    kind=AnchorKind.HEADING,
                    start=heading.start,
                    end=heading.end,
                    label=heading.label,
                    level=heading.level,
                    parent_key=heading.section_key,
                    page_number=page_number,
                )
            )

        index = 0
        while index < len(lines):
            start, end, value = lines[index]
            stripped = value.strip()
            if not stripped or index in heading_lines:
                index += 1
                continue
            parent = self._containing_section(
                start,
                headings,
                section_ends,
                page_key,
            )
            footnote = self._footnote.match(value)
            if footnote:
                drafts.append(
                    self._block(
                        AnchorKind.FOOTNOTE,
                        start,
                        end,
                        f"Footnote {footnote.group(1)}",
                        parent,
                        page_number,
                    )
                )
                index += 1
                continue
            if self._caption.match(value):
                drafts.append(
                    self._block(
                        AnchorKind.CAPTION,
                        start,
                        end,
                        stripped[:200],
                        parent,
                        page_number,
                    )
                )
                index += 1
                continue
            if self._list_item.match(value):
                drafts.append(
                    self._block(
                        AnchorKind.LIST_ITEM,
                        start,
                        end,
                        None,
                        parent,
                        page_number,
                    )
                )
                index += 1
                continue
            paragraph_start = start
            paragraph_end = end
            index += 1
            while index < len(lines):
                next_start, next_end, next_value = lines[index]
                if (
                    not next_value.strip()
                    or index in heading_lines
                    or self._footnote.match(next_value)
                    or self._caption.match(next_value)
                    or self._list_item.match(next_value)
                ):
                    break
                paragraph_end = next_end
                index += 1
            drafts.append(
                self._block(
                    AnchorKind.PARAGRAPH,
                    paragraph_start,
                    paragraph_end,
                    None,
                    parent,
                    page_number,
                )
            )
        return tuple(drafts)

    @staticmethod
    def _lines(
        text: str,
        page_start: int,
        page_end: int,
    ) -> tuple[tuple[int, int, str], ...]:
        lines = []
        offset = page_start
        for line in text[page_start:page_end].splitlines(keepends=True):
            value = line.rstrip("\r\n")
            lines.append((offset, offset + len(value), value))
            offset += len(line)
        if offset < page_end or not lines:
            value = text[offset:page_end]
            lines.append((offset, page_end, value))
        return tuple(lines)

    def _headings(
        self,
        lines: tuple[tuple[int, int, str], ...],
        page_number: int,
    ) -> tuple[_Heading, ...]:
        headings: list[_Heading] = []
        index = 0
        while index < len(lines):
            start, end, value = lines[index]
            atx = self._atx_heading.match(value)
            if atx:
                headings.append(
                    _Heading(
                        start=start,
                        end=end,
                        label=atx.group(2).strip(),
                        level=len(atx.group(1)),
                        line_indexes=(index,),
                        section_key=f"section:{page_number}:{start}",
                    )
                )
                index += 1
                continue
            if value.strip() and index + 1 < len(lines):
                _, underline_end, underline = lines[index + 1]
                match = self._setext_underline.match(underline)
                if match:
                    headings.append(
                        _Heading(
                            start=start,
                            end=underline_end,
                            label=value.strip(),
                            level=1 if match.group(1).startswith("=") else 2,
                            line_indexes=(index, index + 1),
                            section_key=f"section:{page_number}:{start}",
                        )
                    )
                    index += 2
                    continue
            index += 1
        return tuple(headings)

    @staticmethod
    def _containing_section(
        position: int,
        headings: tuple[_Heading, ...],
        section_ends: dict[str, int],
        page_key: str,
    ) -> str:
        containing = [
            heading
            for heading in headings
            if heading.start <= position < section_ends[heading.section_key]
        ]
        if not containing:
            return page_key
        return max(containing, key=lambda item: (item.level, item.start)).section_key

    @staticmethod
    def _block(
        kind: AnchorKind,
        start: int,
        end: int,
        label: str | None,
        parent: str,
        page_number: int,
    ) -> _Draft:
        return _Draft(
            key=f"{kind.value}:{page_number}:{start}",
            kind=kind,
            start=start,
            end=end,
            label=label,
            level=None,
            parent_key=parent,
            page_number=page_number,
        )


class StructuralDocumentManager:
    version = "structural-document-manager/1"

    def __init__(
        self,
        *,
        store: ImmutableStore,
        extractor: DeterministicStructuralExtractor | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.extractor = extractor or DeterministicStructuralExtractor()
        self.clock = clock

    def derive(
        self,
        text: str,
        *,
        text_derivation_id: str,
        source_version_id: str,
        source_content_sha256: str,
        input_media_type: str,
        extracted_at: datetime | None = None,
    ) -> StructuralDerivationReceipt:
        self.store.initialize()
        derivation, anchors = self.extractor.extract(
            text,
            text_derivation_id=text_derivation_id,
            source_version_id=source_version_id,
            source_content_sha256=source_content_sha256,
            input_media_type=input_media_type,
            extracted_at=extracted_at or self.clock(),
        )
        derivation_hash = self.store.put_record(
            "structural-derivation",
            derivation,
        )
        batch_hash, anchor_hashes = self.store.put_record_batch(
            "structural-anchor",
            list(anchors),
        )
        return StructuralDerivationReceipt(
            structural_derivation_id=derivation.id,
            structural_anchor_ids=derivation.anchor_ids,
            record_hashes={
                "structural-derivation": (derivation_hash,),
                "structural-anchor-batch": (batch_hash,),
                "structural-anchor": anchor_hashes,
            },
        )

    def derive_stored(self, text_derivation_id: str) -> StructuralDerivationReceipt:
        from research_agent.parsing import TextDerivation

        matches = tuple(
            TextDerivation.model_validate(value)
            for value in self.store.iter_records("text-derivation")
            if value.get("id") == text_derivation_id
        )
        if len(matches) != 1:
            raise StructureError("text derivation was not found uniquely")
        item = matches[0]
        text = self.store.read_blob(item.derived_content_sha256).decode(
            "utf-8",
            errors="strict",
        )
        return self.derive(
            text,
            text_derivation_id=item.id,
            source_version_id=item.derived_source_version_id,
            source_content_sha256=item.derived_content_sha256,
            input_media_type=item.input_media_type,
            extracted_at=item.extracted_at,
        )
