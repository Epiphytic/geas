from __future__ import annotations

import hashlib
import json
import posixpath
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from research_agent.agent_skills import (
    GeasIdentity,
    OntologyIdentity,
    ProjectionIdentity,
    SkillFile,
    SkillIdentity,
    SkillManifest,
    canonical_manifest_bytes,
    snapshot_digest,
    validate_snapshot,
)
from research_agent.projection import TopicView

COMMIT = "a" * 40
SHA256 = "b" * 64


def _manifest(*, files: tuple[SkillFile, ...] | None = None) -> SkillManifest:
    inventory = files or (SkillFile(path="SKILL.md", sha256=SHA256),)
    return SkillManifest(
        format_version=1,
        skill=SkillIdentity(name="test-skill"),
        ontology=OntologyIdentity(
            name="test-ontology",
            repository_url="https://example.test/ontology.git",
            branch="main",
            commit=COMMIT,
        ),
        geas=GeasIdentity(
            project_url="https://github.com/Epiphytic/geas",
            version="1.2.3",
            commit=None,
        ),
        projection=ProjectionIdentity(
            snapshot_id="truth:sha256:example", topic_concept_id="concept:root"
        ),
        files=inventory,
        snapshot_sha256=snapshot_digest(inventory),
    )


def test_manifest_round_trip_is_canonical_and_has_one_trailing_newline() -> None:
    """Catches a non-portable manifest encoding or inventory digest."""
    manifest = _manifest()

    encoded = canonical_manifest_bytes(manifest)

    assert encoded.endswith(b"\n")
    assert not encoded.endswith(b"\n\n")
    decoded = json.loads(encoded)
    assert list(decoded) == sorted(decoded)
    assert SkillManifest.model_validate(decoded) == manifest


def test_manifest_requires_format_version_and_safe_ontology_identity() -> None:
    """Catches manifests that silently default versions or embed unsafe identity text."""
    payload = _manifest().model_dump(mode="json")
    payload.pop("format_version")

    with pytest.raises(ValidationError, match="format_version"):
        SkillManifest.model_validate(payload)
    with pytest.raises(ValidationError, match="name"):
        OntologyIdentity(
            name="Test ontology",
            repository_url="https://example.test/ontology.git",
            branch="main",
            commit=COMMIT,
        )
    with pytest.raises(ValidationError, match="repository_url"):
        OntologyIdentity(
            name="test-ontology",
            repository_url="file:///private/ontology.git",
            branch="main",
            commit=COMMIT,
        )
    with pytest.raises(ValidationError, match="repository_url"):
        OntologyIdentity(
            name="test-ontology",
            repository_url="https://localhost/ontology.git",
            branch="main",
            commit=COMMIT,
        )
    with pytest.raises(ValidationError, match="branch"):
        OntologyIdentity(
            name="test-ontology",
            repository_url="https://example.test/ontology.git",
            branch="../outside",
            commit=COMMIT,
        )


@pytest.mark.parametrize(
    ("factory", "message"),
    (
        (lambda: SkillIdentity(name="Not Valid"), "name"),
        (
            lambda: OntologyIdentity(
                name="test-ontology",
                repository_url="https://example.test/ontology.git",
                branch="main",
                commit="A" * 40,
            ),
            "commit",
        ),
        (lambda: SkillFile(path="/SKILL.md", sha256=SHA256), "path"),
        (lambda: SkillFile(path="references/../SKILL.md", sha256=SHA256), "path"),
    ),
)
def test_manifest_records_reject_invalid_identity_or_path(factory: object, message: str) -> None:
    """Catches permissive identities and paths that could escape a snapshot."""
    with pytest.raises(ValidationError, match=message):
        factory()  # type: ignore[operator]


def test_manifest_rejects_extra_keys_unsorted_inventory_duplicates_and_bad_digest() -> None:
    """Catches a manifest accepting ambiguous or tampered inventory data."""
    first = SkillFile(path="SKILL.md", sha256="1" * 64)
    second = SkillFile(path="references/index.md", sha256="2" * 64)
    base = _manifest(files=(first, second)).model_dump(mode="json")

    with pytest.raises(ValidationError, match="Extra inputs"):
        SkillManifest.model_validate({**base, "unexpected": True})
    with pytest.raises(ValidationError, match="sorted"):
        SkillManifest.model_validate({**base, "files": [second.model_dump(), first.model_dump()]})
    with pytest.raises(ValidationError, match="duplicate"):
        SkillManifest.model_validate({**base, "files": [first.model_dump(), first.model_dump()]})
    with pytest.raises(ValidationError, match="snapshot_sha256"):
        SkillManifest.model_validate({**base, "snapshot_sha256": "0" * 64})


def test_validate_snapshot_rejects_missing_or_mishashed_inventory_file(tmp_path: Path) -> None:
    """Catches validation that trusts the manifest instead of the actual snapshot."""
    skill = tmp_path / "SKILL.md"
    skill.write_bytes(b"skill\n")
    manifest = _manifest(
        files=(SkillFile(path="SKILL.md", sha256=hashlib.sha256(b"skill\n").hexdigest()),)
    )
    (tmp_path / "geas-skill.json").write_bytes(canonical_manifest_bytes(manifest))
    assert validate_snapshot(tmp_path) == manifest

    (tmp_path / "extra.md").write_text("unlisted\n")
    with pytest.raises(ValueError, match="inventory"):
        validate_snapshot(tmp_path)
    (tmp_path / "extra.md").unlink()
    skill.write_text("tampered\n")
    with pytest.raises(ValueError, match="hash"):
        validate_snapshot(tmp_path)


def test_validate_snapshot_rejects_symbolic_linked_root_or_file(tmp_path: Path) -> None:
    """Catches a validator following links outside the managed snapshot."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "SKILL.md").write_text("skill\n")
    manifest = _manifest(
        files=(SkillFile(path="SKILL.md", sha256=hashlib.sha256(b"skill\n").hexdigest()),)
    )
    (target / "geas-skill.json").write_bytes(canonical_manifest_bytes(manifest))
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        validate_snapshot(linked_root)

    (target / "linked.md").symlink_to(target / "SKILL.md")
    with pytest.raises(ValueError, match="symbolic"):
        validate_snapshot(target)


def test_validate_snapshot_rejects_symlinked_parent_and_noncanonical_manifest(
    tmp_path: Path,
) -> None:
    """Catches validation that resolves an indirect root or accepts equivalent JSON."""
    parent = tmp_path / "parent"
    snapshot = parent / "skill"
    snapshot.mkdir(parents=True)
    (snapshot / "SKILL.md").write_text("skill\n")
    manifest = _manifest(
        files=(SkillFile(path="SKILL.md", sha256=hashlib.sha256(b"skill\n").hexdigest()),)
    )
    (snapshot / "geas-skill.json").write_bytes(canonical_manifest_bytes(manifest))
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        validate_snapshot(linked_parent / "skill")

    (snapshot / "geas-skill.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2) + "\n"
    )
    with pytest.raises(ValueError, match="canonical"):
        validate_snapshot(snapshot)


def _topic(*, reverse: bool = False) -> TopicView:
    concepts = (
        {
            "id": "concept:root",
            "label": "Root",
            "description": "Root concept",
            "broader": "",
            "synonyms": "",
        },
        {
            "id": "concept:child",
            "label": "Child",
            "description": "Child concept\n# hostile concept heading",
            "broader": "concept:root",
            "synonyms": "child term",
        },
    )
    sources = (
        {
            "id": "source:two",
            "title": "Second source",
            "source_uri": "https://archive.example.test/two",
            "original_locator": "https://original.example.test/two",
            "content_sha256": "2" * 64,
            "trust_zone": "untrusted",
            "license": "CC-BY-4.0",
            "publisher": "Example Publisher",
        },
        {
            "id": "source:one",
            "title": "First source",
            "source_uri": "https://archive.example.test/one",
            "original_locator": "https://original.example.test/one",
            "content_sha256": "1" * 64,
            "trust_zone": "trusted",
            "license": "CC-BY-4.0",
            "publisher": "Example Publisher",
        },
    )
    claims = (
        {
            "id": "claim:two",
            "subject": "concept:child",
            "predicate": "related_to",
            "object_json": '"concept:root"',
            "stance": "asserts",
            "epistemic_status": "observed",
            "asserted_by": "source:two",
            "qualifiers_json": "{}",
            "evidence_id": "evidence:two",
            "source_id": "source:two",
            "source_uri": "https://original.example.test/two",
            "exact_text": "Exact evidence two.",
        },
        {
            "id": "claim:one",
            "subject": "concept:root",
            "predicate": "supports",
            "object_json": '"concept:child"',
            "stance": "asserts",
            "epistemic_status": "observed",
            "asserted_by": "source:one",
            "qualifiers_json": "{}",
            "evidence_id": "evidence:one",
            "source_id": "source:one",
            "source_uri": "https://original.example.test/one",
            "exact_text": "Exact evidence one.",
            "selector_type": "text_quote",
            "selector_prefix": "Before evidence.",
            "selector_suffix": "After evidence.",
            "selector_start": 7,
            "selector_end": 26,
            "selector_pointer": "/claims/0",
        },
    )
    controversies = (
        {
            "id": "controversy:one",
            "question": "Which claim?",
            "description": "Competing claims\n# hostile controversy heading",
            "status": "open",
            "claim_ids": "claim:one,claim:two",
        },
    )
    gaps = (
        {
            "id": "gap:one",
            "question": "What is missing?",
            "rationale": "Missing evidence\n# hostile gap heading",
            "topic_concept_id": "concept:root",
            "kind": "evidence",
            "status": "open",
            "priority": 1,
            "related_claim_ids": "claim:one",
        },
    )
    threats = (
        {
            "id": "threat:one",
            "source_version": "source:two",
            "source_uri": "https://archive.example.test/two",
            "threat_type": "prompt_injection",
            "status": "suspected",
            "severity": "high",
            "detector_kind": "deterministic_rule",
            "detector_id": "rule-1",
            "policy_rule": "source-policy",
        },
    )
    references = (
        {
            "id": "citation:one",
            "identifier_kind": "doi",
            "identifier_value": "10.1000/example",
            "relation": "cites",
            "canonical_locator": "https://doi.org/10.1000/example",
            "source_id": "source:one",
            "source_uri": "https://original.example.test/one",
            "structural_anchor_id": "anchor:one",
            "start": 10,
            "end": 20,
            "signal": "exact",
        },
    )

    def ordered(values: tuple[object, ...]) -> tuple[object, ...]:
        return tuple(reversed(values)) if reverse else values

    return TopicView(
        topic_concept_id="concept:root",
        descendant_concept_ids=ordered(("concept:root", "concept:child")),
        concepts=ordered(concepts),
        sources=ordered(sources),
        claims=ordered(claims),
        controversies=ordered(controversies),
        gaps=ordered(gaps),
        threats=ordered(threats),
        references=ordered(references),
        projection_snapshot_id="truth:sha256:projection",
    )


def test_render_ontology_skill_is_deterministic_and_bounded_to_one_hop_references() -> None:
    """Catches renderer output that depends on projection ordering or leaks local metadata."""
    from research_agent.render import render_ontology_skill

    first = render_ontology_skill(
        _topic(),
        skill_name="test-skill",
        ontology_name="test-ontology",
        repository_url="https://example.test/ontology.git",
        branch="main",
        ontology_commit=COMMIT,
        geas_version="1.2.3",
        geas_commit=None,
    )
    reversed_input = render_ontology_skill(
        _topic(reverse=True),
        skill_name="test-skill",
        ontology_name="test-ontology",
        repository_url="https://example.test/ontology.git",
        branch="main",
        ontology_commit=COMMIT,
        geas_version="1.2.3",
        geas_commit=None,
    )

    assert first == reversed_input
    assert Path("SKILL.md") in first
    assert Path("geas-skill.json") in first
    assert Path("references/index.md") in first
    manifest = SkillManifest.model_validate_json(first[Path("geas-skill.json")])
    assert tuple(item.path for item in manifest.files) == tuple(
        path.as_posix() for path in first if path.name != "geas-skill.json"
    )
    assert any(path.parts[:2] == ("references", "concepts") for path in first)
    assert any(path.parts[:2] == ("references", "claims") for path in first)
    assert any(path.parts[:2] == ("references", "citations") for path in first)
    skill = first[Path("SKILL.md")].decode()
    assert "references/index.md" in skill
    assert "references/concepts" not in skill
    rendered = b"".join(first.values()).decode()
    for expected in (
        "https://original.example.test/one",
        "claim:one",
        "evidence:one",
        "citation:one",
        "Selector type: `text_quote`",
        "Selector prefix (untrusted data):",
        "Selector suffix (untrusted data):",
        "Selector range: `7..26`",
        "Selector pointer (untrusted data):",
        "Untrusted concept description",
        "Untrusted controversy description",
        "Untrusted gap rationale",
    ):
        assert expected in rendered
    for selector in ("Before evidence.", "After evidence.", "/claims/0"):
        assert f"\n        {selector}" in rendered
    for hostile in (
        "# hostile concept heading",
        "# hostile controversy heading",
        "# hostile gap heading",
    ):
        assert f"\n    {hostile}" in rendered
        assert f"\n{hostile}" not in rendered
    for forbidden in ("FULL-DOCUMENT-SENTINEL", "/tmp/", "generated_at:", "username:", "hostname:"):
        assert forbidden not in rendered
    assert all(value.endswith(b"\n") and not value.endswith(b"\n\n") for value in first.values())


def test_render_ontology_skill_quarantines_hostile_claim_and_identity_scalars() -> None:
    """Catches untrusted scalars escaping Markdown data fields into instructions."""
    from research_agent.render import render_ontology_skill

    topic = _topic()
    hostile_claim = {
        **topic.claims[0],
        "predicate": "predicate`\n# hostile-predicate",
        "object_json": '"object`\n# hostile-object"',
        "qualifiers_json": '{"note":"`\n# hostile-qualifiers"}',
        "asserted_by": "model:test`\n# hostile-asserted-by",
    }
    hostile_reference = {
        **topic.references[0],
        "identifier_kind": "doi`\n# hostile-identifier-kind",
        "identifier_value": "10.1000/test`\n# hostile-identifier-value",
    }
    hostile_source = {
        **topic.sources[0],
        "original_locator": "opaque`\n# hostile-source-locator",
    }
    hostile_topic = topic.model_copy(
        update={
            "topic_concept_id": "concept:root`\n# hostile-topic-id",
            "projection_snapshot_id": "truth:test`\n# hostile-snapshot-id",
            "claims": (hostile_claim, *topic.claims[1:]),
            "references": (hostile_reference,),
            "sources": (hostile_source, *topic.sources[1:]),
        }
    )

    files = render_ontology_skill(
        hostile_topic,
        skill_name="test-skill",
        ontology_name="test-ontology",
        repository_url="https://example.test/ontology.git",
        branch="main",
        ontology_commit=COMMIT,
        geas_version="1.2.3",
        geas_commit=None,
    )
    rendered = b"".join(files.values()).decode()

    for label in (
        "Predicate",
        "Object",
        "Qualifiers",
        "Asserted by",
        "Identifier kind",
        "Identifier value",
        "Topic concept",
        "Projection snapshot",
    ):
        assert f"- {label} (untrusted data):" in rendered
    for marker in (
        "hostile-predicate",
        "hostile-object",
        "hostile-qualifiers",
        "hostile-asserted-by",
        "hostile-identifier-kind",
        "hostile-identifier-value",
        "hostile-topic-id",
        "hostile-snapshot-id",
    ):
        assert f"\n        # {marker}" in rendered
        assert f"\n# {marker}" not in rendered
    assert "opaqueˋ # hostile-source-locator" in rendered
    assert "\n# hostile-source-locator" not in rendered


def test_render_ontology_skill_inerts_hostile_topic_and_concept_record_ids() -> None:
    """Catches record IDs closing inline code and creating active Markdown headings."""
    from research_agent.render import render_ontology_skill

    hostile_id = "concept:hostile`\n# hostile-record-id"
    topic = _topic()
    hostile_topic = topic.model_copy(
        update={
            "topic_concept_id": hostile_id,
            "descendant_concept_ids": (hostile_id, "concept:child"),
            "concepts": ({**topic.concepts[0], "id": hostile_id}, *topic.concepts[1:]),
        }
    )

    files = render_ontology_skill(
        hostile_topic,
        skill_name="test-skill",
        ontology_name="test-ontology",
        repository_url="https://example.test/ontology.git",
        branch="main",
        ontology_commit=COMMIT,
        geas_version="1.2.3",
        geas_commit=None,
    )
    rendered = b"".join(files.values()).decode()

    assert "Record ID: `concept:hostileˋ # hostile-record-id`" in rendered
    assert "\n# hostile-record-id" not in rendered


def test_render_ontology_skill_typed_reference_links_resolve_from_their_page() -> None:
    """Catches typed-page links being emitted relative to the snapshot root."""
    from research_agent.render import render_ontology_skill

    files = render_ontology_skill(
        _topic(),
        skill_name="test-skill",
        ontology_name="test-ontology",
        repository_url="https://example.test/ontology.git",
        branch="main",
        ontology_commit=COMMIT,
        geas_version="1.2.3",
        geas_commit=None,
    )

    checked = 0
    for page, content in files.items():
        if page.suffix != ".md" or len(page.parts) < 3:
            continue
        for target in re.findall(r"\]\(([^)]+)\)", content.decode()):
            if "://" in target:
                continue
            resolved = Path(posixpath.normpath(posixpath.join(page.parent.as_posix(), target)))
            assert resolved in files, f"{page} contains broken link {target}"
            checked += 1
    assert checked > 0


def test_render_ontology_skill_preserves_zero_citation_offsets() -> None:
    """Catches valid offset zero being rendered as unknown through truthiness."""
    from research_agent.render import render_ontology_skill

    topic = _topic()
    reference = {**topic.references[0], "start": 0, "end": 0}
    files = render_ontology_skill(
        topic.model_copy(update={"references": (reference,)}),
        skill_name="test-skill",
        ontology_name="test-ontology",
        repository_url="https://example.test/ontology.git",
        branch="main",
        ontology_commit=COMMIT,
        geas_version="1.2.3",
        geas_commit=None,
    )
    citation = next(
        content.decode() for path, content in files.items() if "citations" in path.parts
    )

    assert "- Exact range: 0..0" in citation


def test_render_ontology_skill_uses_explicit_snapshot_path_commands() -> None:
    """Catches lifecycle instructions relying on an ambiguous current directory."""
    from research_agent.render import render_ontology_skill

    entrypoint = render_ontology_skill(
        _topic(),
        skill_name="test-skill",
        ontology_name="test-ontology",
        repository_url="https://example.test/ontology.git",
        branch="main",
        ontology_commit=COMMIT,
        geas_version="1.2.3",
        geas_commit=None,
    )[Path("SKILL.md")].decode()

    assert "/absolute/path/to/directory-containing-this-SKILL" in entrypoint
    for command in ("skill-update", "skill-unlink", "skill-remove"):
        assert f"geas {command} ." not in entrypoint
