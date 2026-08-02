import os
from pathlib import Path

from research_agent.secrets import load_env_file


def test_env_loader_reads_only_allowlisted_names_without_overwriting(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "MOJEEK_API_KEY='fixture-key'\nBRAVE_API_KEY=ignored-key\nEXTRA_SECRET=must-not-load\n"
    )
    monkeypatch.setenv("BRAVE_API_KEY", "existing")
    monkeypatch.delenv("MOJEEK_API_KEY", raising=False)
    monkeypatch.delenv("EXTRA_SECRET", raising=False)

    loaded = load_env_file(
        path,
        allowed_names=frozenset({"MOJEEK_API_KEY", "BRAVE_API_KEY"}),
    )

    assert loaded == frozenset({"MOJEEK_API_KEY"})
    assert os.environ["MOJEEK_API_KEY"] == "fixture-key"
    assert os.environ["BRAVE_API_KEY"] == "existing"
    assert "EXTRA_SECRET" not in os.environ
