from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from datetime import datetime
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field

from research_agent.citations import CitationDocumentManager
from research_agent.knowledge import DeterministicThreatScanner
from research_agent.models import (
    SourceVersion,
    StrictModel,
    canonical_json,
    content_id,
    utc_now,
)
from research_agent.sandbox import BubblewrapSandbox, SandboxError
from research_agent.store import ImmutableStore
from research_agent.structure import StructuralDocumentManager


class ParserError(ValueError):
    pass


class TextDerivation(StrictModel):
    id: str
    original_source_version_id: str
    derived_source_version_id: str
    original_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    derived_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_media_type: str
    output_media_type: str = "text/plain"
    parser_id: str
    parser_version: str
    parser_runtime: Literal[
        "in_process_deterministic", "bubblewrap_native", "wasi"
    ] = "in_process_deterministic"
    extraction_scope: str = "body_text"
    extracted_at: datetime
    character_count: int = Field(ge=0)
    warnings: tuple[str, ...] = ()


class ParsedIngestReceipt(StrictModel):
    original_source_version_id: str
    derived_source_version_id: str
    derivation_id: str
    structural_derivation_id: str
    structural_anchor_ids: tuple[str, ...]
    citation_derivation_id: str
    research_identifier_ids: tuple[str, ...] = ()
    bibliographic_reference_ids: tuple[str, ...] = ()
    evidence_fragment_ids: tuple[str, ...] = ()
    threat_observation_ids: tuple[str, ...] = ()
    record_hashes: dict[str, tuple[str, ...]]


class ParsedText(StrictModel):
    text: str
    parser_id: str
    parser_version: str
    parser_runtime: Literal[
        "in_process_deterministic", "bubblewrap_native", "wasi"
    ] = "in_process_deterministic"
    warnings: tuple[str, ...] = ()


class _VisibleHtmlParser(HTMLParser):
    ignored = frozenset({"script", "style", "template", "noscript", "svg"})
    blocks = frozenset(
        {
            "address",
            "article",
            "aside",
            "blockquote",
            "dd",
            "div",
            "dl",
            "dt",
            "footer",
            "header",
            "main",
            "nav",
            "ol",
            "p",
            "pre",
            "section",
            "table",
            "tbody",
            "td",
            "tfoot",
            "th",
            "thead",
            "tr",
            "ul",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in self.ignored:
            self.depth += 1
            return
        if self.depth:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self.parts.append(f"\n\n{'#' * int(tag[1])} ")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "figcaption":
            self.parts.append("\nFigure: ")
        elif tag in self.blocks:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self.ignored and self.depth:
            self.depth -= 1
            return
        if not self.depth and (
            tag in self.blocks
            or tag == "li"
            or tag == "figcaption"
            or re.fullmatch(r"h[1-6]", tag)
        ):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.depth:
            self.parts.append(data)


class DocumentParserRegistry:
    version = "document-parser-registry/1"
    max_input_bytes = 25_000_000
    max_output_characters = 20_000_000
    max_zip_entries = 10_000
    max_zip_uncompressed_bytes = 100_000_000

    def __init__(self, *, native_sandbox: BubblewrapSandbox | None = None) -> None:
        self.native_sandbox = native_sandbox or BubblewrapSandbox()
        self.parsers: dict[str, Callable[[bytes], ParsedText]] = {
            "application/json": self._json,
            "application/pdf": self._pdf,
            "application/xml": self._xml,
            "application/xhtml+xml": self._html,
            "text/html": self._html,
            "text/xml": self._xml,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
                self._office_zip
            ),
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": (
                self._office_zip
            ),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (
                self._office_zip
            ),
            "application/vnd.oasis.opendocument.text": self._office_zip,
        }

    def parse(self, content: bytes, media_type: str) -> ParsedText:
        if len(content) > self.max_input_bytes:
            raise ParserError("document exceeds parser input limit")
        normalized_type = media_type.split(";", 1)[0].strip().casefold()
        if normalized_type.startswith("text/") and normalized_type not in self.parsers:
            parsed = self._plain(content)
        else:
            parser = self.parsers.get(normalized_type)
            if parser is None:
                raise ParserError(f"no deterministic text parser for {normalized_type}")
            parsed = parser(content)
        normalized = unicodedata.normalize("NFC", parsed.text)
        normalized = "".join(
            character
            for character in normalized
            if character in "\n\t\f"
            or not unicodedata.category(character).startswith("C")
        )
        normalized = re.sub(r"[ \t]+\n", "\n", normalized)
        normalized = re.sub(r"\n{4,}", "\n\n\n", normalized).strip() + "\n"
        if len(normalized) > self.max_output_characters:
            raise ParserError("derived text exceeds parser output limit")
        return parsed.model_copy(update={"text": normalized})

    @staticmethod
    def _decode(content: bytes) -> str:
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            return content.decode("utf-8", errors="replace")

    def _plain(self, content: bytes) -> ParsedText:
        return ParsedText(
            text=self._decode(content),
            parser_id="parser:plain-text",
            parser_version="1",
        )

    def _json(self, content: bytes) -> ParsedText:
        try:
            value = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ParserError("invalid JSON document") from None
        return ParsedText(
            text=json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
            parser_id="parser:json-text",
            parser_version="1",
        )

    def _html(self, content: bytes) -> ParsedText:
        parser = _VisibleHtmlParser()
        parser.feed(self._decode(content))
        return ParsedText(
            text="".join(parser.parts),
            parser_id="parser:html-visible-text",
            parser_version="2",
            warnings=(
                "active markup and remote resources were discarded; "
                "block structure was rendered as inert text",
            ),
        )

    def _xml(self, content: bytes) -> ParsedText:
        if re.search(br"<!DOCTYPE|<!ENTITY", content, re.IGNORECASE):
            raise ParserError("XML declarations with DTDs or entities are forbidden")
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            raise ParserError("invalid XML document") from None
        return ParsedText(
            text="\n".join(part.strip() for part in root.itertext() if part.strip()),
            parser_id="parser:xml-text",
            parser_version="1",
            warnings=("markup and external resources were discarded",),
        )

    def _office_zip(self, content: bytes) -> ParsedText:
        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile:
            raise ParserError("invalid office archive") from None
        with archive:
            entries = archive.infolist()
            if len(entries) > self.max_zip_entries:
                raise ParserError("office archive has too many entries")
            total = sum(item.file_size for item in entries)
            if total > self.max_zip_uncompressed_bytes:
                raise ParserError("office archive exceeds uncompressed size limit")
            names = sorted(
                item.filename
                for item in entries
                if self._office_text_part(item.filename)
            )
            parts: list[str] = []
            for name in names:
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts:
                    raise ParserError("office archive contains an unsafe path")
                payload = archive.read(name)
                if re.search(br"<!DOCTYPE|<!ENTITY", payload, re.IGNORECASE):
                    raise ParserError("office XML contains a forbidden declaration")
                try:
                    root = ET.fromstring(payload)
                except ET.ParseError:
                    raise ParserError("invalid office XML") from None
                parts.extend(item.strip() for item in root.itertext() if item.strip())
        return ParsedText(
            text="\n".join(parts),
            parser_id="parser:office-open-xml-text",
            parser_version="1",
            warnings=("layout, macros, embedded media, and external resources were discarded",),
        )

    @staticmethod
    def _office_text_part(name: str) -> bool:
        return (
            name == "content.xml"
            or name == "word/document.xml"
            or bool(re.fullmatch(r"ppt/slides/slide[0-9]+\.xml", name))
            or name == "xl/sharedStrings.xml"
            or bool(re.fullmatch(r"xl/worksheets/sheet[0-9]+\.xml", name))
        )

    def _pdf(self, content: bytes) -> ParsedText:
        executable = shutil.which("pdftotext")
        if executable is None:
            raise ParserError("pdftotext is unavailable")
        try:
            output = self.native_sandbox.run(
                executable,
                ("-enc", "UTF-8", "-", "-"),
                input_bytes=content,
            )
        except SandboxError as error:
            raise ParserError(str(error)) from None
        return ParsedText(
            text=output.decode("utf-8", errors="replace"),
            parser_id="parser:poppler-pdftotext",
            parser_version="2",
            parser_runtime="bubblewrap_native",
            warnings=("images, layout, annotations, actions, and embedded files were discarded",),
        )


class ParsedDocumentManager:
    version = "parsed-document-manager/1"

    def __init__(
        self,
        *,
        store: ImmutableStore,
        registry: DocumentParserRegistry | None = None,
        scanner: DeterministicThreatScanner | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.registry = registry or DocumentParserRegistry()
        self.scanner = scanner or DeterministicThreatScanner(clock=clock)
        self.clock = clock

    def ingest(
        self,
        content: bytes,
        *,
        source_uri: str,
        media_type: str,
        connector_id: str,
        license: str | None,
        acquired_at: datetime | None = None,
    ) -> ParsedIngestReceipt:
        self.store.initialize()
        acquired_at = acquired_at or self.clock()
        original = SourceVersion.from_bytes(
            source_uri=source_uri,
            content=content,
            media_type=media_type,
            connector_id=connector_id,
            license=license,
            acquired_at=acquired_at,
            trust_zone="quarantined",
        )
        original_blob = self.store.put_blob(content)
        if original_blob != original.content_sha256:
            raise RuntimeError("original source and blob hashes diverged")
        original_record_hash = self.store.put_record("source-version", original)
        return self._derive(
            original,
            content,
            original_record_hash,
            persist_receipt=False,
        )

    def parse_source(self, source_version_id: str) -> ParsedIngestReceipt:
        """Parse one already-archived source without recreating its metadata."""
        self.store.initialize()
        sources = sorted(
            (
                SourceVersion.model_validate(value)
                for value in self.store.iter_records("source-version")
                if value.get("id") == source_version_id
            ),
            key=canonical_json,
        )
        if not sources:
            raise ValueError(f"unknown immutable source version: {source_version_id}")
        original = sources[0]
        content = self.store.read_blob(original.content_sha256)
        original_record_hash = self.store.put_record("source-version", original)
        return self._derive(
            original,
            content,
            original_record_hash,
            persist_receipt=True,
        )

    def _derive(
        self,
        original: SourceVersion,
        content: bytes,
        original_record_hash: str,
        *,
        persist_receipt: bool,
    ) -> ParsedIngestReceipt:
        acquired_at = original.acquired_at
        media_type = original.media_type
        connector_id = original.connector_id
        license = original.license
        parsed = self.registry.parse(content, media_type)
        derived_content = parsed.text.encode()
        derived_digest = hashlib.sha256(derived_content).hexdigest()
        if derived_digest == original.content_sha256:
            derived = original
            derived_record_hash = original_record_hash
        else:
            derived = SourceVersion.from_bytes(
                source_uri=f"derived:{original.id}#body-text",
                content=derived_content,
                media_type="text/plain",
                connector_id=f"{connector_id}+{parsed.parser_id}",
                license=license,
                acquired_at=acquired_at,
                predecessor=original.id,
                trust_zone="quarantined",
            )
            derived_blob = self.store.put_blob(derived_content)
            if derived_blob != derived.content_sha256:
                raise RuntimeError("derived source and blob hashes diverged")
            derived_record_hash = self.store.put_record("source-version", derived)
        fields = {
            "original_source_version_id": original.id,
            "derived_source_version_id": derived.id,
            "parser_id": parsed.parser_id,
            "parser_version": parsed.parser_version,
            "parser_runtime": parsed.parser_runtime,
            "extracted_at": acquired_at,
        }
        derivation = TextDerivation(
            id=content_id("text-derivation", fields),
            original_source_version_id=original.id,
            derived_source_version_id=derived.id,
            original_content_sha256=original.content_sha256,
            derived_content_sha256=derived.content_sha256,
            input_media_type=media_type,
            parser_id=parsed.parser_id,
            parser_version=parsed.parser_version,
            parser_runtime=parsed.parser_runtime,
            extracted_at=acquired_at,
            character_count=len(parsed.text),
            warnings=parsed.warnings,
        )
        derivation_record_hash = self.store.put_record(
            "text-derivation",
            derivation,
        )
        findings = self.scanner.scan(derived.id, derived_content)
        fragments = tuple(item[0] for item in findings)
        observations = tuple(item[1] for item in findings)
        fragment_hashes = tuple(
            self.store.put_record("evidence-fragment", item) for item in fragments
        )
        observation_hashes = tuple(
            self.store.put_record("threat-observation", item) for item in observations
        )
        structure = StructuralDocumentManager(
            store=self.store,
            clock=self.clock,
        ).derive(
            parsed.text,
            text_derivation_id=derivation.id,
            source_version_id=derived.id,
            source_content_sha256=derived.content_sha256,
            input_media_type=media_type,
            extracted_at=acquired_at,
        )
        citations = CitationDocumentManager(
            store=self.store,
            clock=self.clock,
        ).derive_stored(structure.structural_derivation_id)
        hashes = {
            "source-version": tuple(
                dict.fromkeys((original_record_hash, derived_record_hash))
            ),
            "text-derivation": (derivation_record_hash,),
            "evidence-fragment": fragment_hashes,
            "threat-observation": observation_hashes,
        }
        hashes.update(structure.record_hashes)
        hashes.update(citations.record_hashes)
        receipt = ParsedIngestReceipt(
            original_source_version_id=original.id,
            derived_source_version_id=derived.id,
            derivation_id=derivation.id,
            structural_derivation_id=structure.structural_derivation_id,
            structural_anchor_ids=structure.structural_anchor_ids,
            citation_derivation_id=citations.citation_derivation_id,
            research_identifier_ids=citations.research_identifier_ids,
            bibliographic_reference_ids=citations.bibliographic_reference_ids,
            evidence_fragment_ids=tuple(item.id for item in fragments),
            threat_observation_ids=tuple(item.id for item in observations),
            record_hashes=hashes,
        )
        if persist_receipt:
            self.store.put_record("parsed-ingest-receipt", receipt)
        return receipt


def select_parsed_sources(
    store: ImmutableStore,
    source_version_ids: tuple[str, ...] = (),
) -> tuple[ParsedIngestReceipt, ...]:
    """Select generic parsed receipts by original or derived immutable identity."""
    requested = set(source_version_ids)
    receipts = tuple(
        ParsedIngestReceipt.model_validate(value)
        for value in store.iter_records("parsed-ingest-receipt")
    )
    covered = {
        item.derivation_id
        for item in receipts
    }
    derivations = tuple(
        TextDerivation.model_validate(value)
        for value in store.iter_records("text-derivation")
        if value.get("id") not in covered
    )
    structures = tuple(store.iter_records("structural-derivation"))
    citations = tuple(store.iter_records("citation-derivation"))
    evidence = tuple(store.iter_records("evidence-fragment"))
    threats = tuple(store.iter_records("threat-observation"))
    synthesized: list[ParsedIngestReceipt] = []
    for derivation in derivations:
        matching_structures = tuple(
            item
            for item in structures
            if item.get("text_derivation_id") == derivation.id
        )
        for structure in matching_structures:
            matching_citations = tuple(
                item
                for item in citations
                if item.get("structural_derivation_id") == structure.get("id")
            )
            citation = matching_citations[-1] if matching_citations else None
            synthesized.append(
                ParsedIngestReceipt(
                    original_source_version_id=derivation.original_source_version_id,
                    derived_source_version_id=derivation.derived_source_version_id,
                    derivation_id=derivation.id,
                    structural_derivation_id=str(structure["id"]),
                    structural_anchor_ids=tuple(structure.get("anchor_ids", ())),
                    citation_derivation_id=(
                        str(citation["id"]) if citation is not None else "citation:none"
                    ),
                    research_identifier_ids=(
                        tuple(citation.get("identifier_ids", ()))
                        if citation is not None
                        else ()
                    ),
                    bibliographic_reference_ids=(
                        tuple(citation.get("reference_ids", ()))
                        if citation is not None
                        else ()
                    ),
                    evidence_fragment_ids=tuple(
                        str(item["id"])
                        for item in evidence
                        if item.get("source_version")
                        == derivation.derived_source_version_id
                    ),
                    threat_observation_ids=tuple(
                        str(item["id"])
                        for item in threats
                        if item.get("target", {}).get("source_version")
                        == derivation.derived_source_version_id
                    ),
                    record_hashes={},
                )
            )
    receipts = (*receipts, *synthesized)
    selected = (
        receipts
        if not requested
        else tuple(
            item
            for item in receipts
            if item.original_source_version_id in requested
            or item.derived_source_version_id in requested
        )
    )
    found = {
        source_id
        for item in selected
        for source_id in (
            item.original_source_version_id,
            item.derived_source_version_id,
        )
    }
    missing = sorted(requested - found)
    if missing:
        raise ValueError(
            "unknown or unparsed source versions: " + ", ".join(missing)
        )
    distinct = {canonical_json(item): item for item in selected}
    return tuple(
        sorted(
            distinct.values(),
            key=lambda item: (
                item.original_source_version_id,
                item.derived_source_version_id,
                item.derivation_id,
            ),
        )
    )
