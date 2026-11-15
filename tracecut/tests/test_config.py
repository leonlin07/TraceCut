from pathlib import Path

from tracecut.config import load_env_file


def test_load_env_does_not_override(monkeypatch, tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("A=from-file\nB=second\n", encoding="utf-8")
    monkeypatch.setenv("A", "existing")
    monkeypatch.delenv("B", raising=False)

    load_env_file(env_file)

    import os

    assert os.environ["A"] == "existing"
    assert os.environ["B"] == "second"
