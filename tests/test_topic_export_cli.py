from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import research_agent.cli as cli
from research_agent.projection import TopicView


def _topic() -> TopicView:
    return TopicView(
        topic_concept_id="concept:test",
        descendant_concept_ids=("concept:test",),
        concepts=(),
        sources=(),
        claims=(),
        controversies=(),
        gaps=(),
        threats=(),
        projection_snapshot_id="snapshot:test",
    )


def _run_main(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> None:
    monkeypatch.setattr(sys, "argv", ["geas", *arguments])
    cli.main()


def test_topic_export_turtle_writes_utf8_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "topic.ttl"
    topic = _topic()
    monkeypatch.setattr(cli, "_resolve_portable_database", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(
        cli,
        "KnowledgeQueryEngine",
        lambda database: SimpleNamespace(topic=lambda *args, **kwargs: topic),
    )
    monkeypatch.setattr(cli, "render_topic_turtle", lambda received: "Turtle: café\n")

    _run_main(monkeypatch, "topic-export", "concept:test", str(output), "--format", "turtle")

    expected = b"Turtle: caf\xc3\xa9\n"
    assert output.read_bytes() == expected
    assert json.loads(capsys.readouterr().out) == {
        "bytes": len(expected),
        "format": "turtle",
        "output": str(output.resolve()),
        "snapshot_id": "snapshot:test",
        "topic_concept_id": "concept:test",
    }


@pytest.mark.parametrize(
    ("flag", "message"),
    (
        ("--force", "--force"),
        ("--vault-link=obsidian/index.md", "--vault-link"),
        ("--vault-link=", "--vault-link"),
    ),
)
def test_topic_export_turtle_rejects_incompatible_flags_before_database_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    message: str,
) -> None:
    accessed = False

    def resolve_database(*args: object, **kwargs: object) -> Path:
        nonlocal accessed
        accessed = True
        raise AssertionError("database resolution must not occur")

    monkeypatch.setattr(cli, "_resolve_portable_database", resolve_database)

    with pytest.raises(ValueError, match=message):
        _run_main(
            monkeypatch,
            "topic-export",
            "concept:test",
            str(tmp_path / "topic.ttl"),
            "--format",
            "turtle",
            flag,
        )

    assert not accessed


def test_topic_export_turtle_missing_concept_keeps_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_resolve_portable_database", lambda *args, **kwargs: tmp_path)

    def missing_topic(*args: object, **kwargs: object) -> TopicView:
        raise ValueError("unknown concept: concept:missing")

    monkeypatch.setattr(
        cli,
        "KnowledgeQueryEngine",
        lambda database: SimpleNamespace(topic=missing_topic),
    )

    with pytest.raises(ValueError, match="unknown concept"):
        _run_main(
            monkeypatch,
            "topic-export",
            "concept:missing",
            str(tmp_path / "missing.ttl"),
            "--format",
            "turtle",
        )
