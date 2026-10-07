from __future__ import annotations

import os
from pathlib import Path

import pytest

from patent_fetch.config import env_dir, load_env


def test_env_dir_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PATENT_FETCH_ENV_DIR")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert env_dir() == tmp_path / "patent-fetch"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert env_dir() == Path.home() / ".config" / "patent-fetch"


def test_load_env_takes_only_known_keys_and_never_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "a.env").write_text(
        "# comment\n"
        "export EPO_OPS_KEY='k1'\n"
        "PATENT_FETCH_EMAIL=someone@example.org\n"
        "UNRELATED_CLOUD_TOKEN=secret\n"
        "LENS_API_TOKEN=from-the-file\n"
        "not a line\n"
    )
    (tmp_path / "ignored.txt").write_text("SERPAPI_API_KEY=nope\n")
    monkeypatch.setenv("LENS_API_TOKEN", "https://already.example")
    loaded = load_env(tmp_path)
    assert loaded == ["EPO_OPS_KEY", "PATENT_FETCH_EMAIL"]
    assert os.environ["EPO_OPS_KEY"] == "k1"
    assert "UNRELATED_CLOUD_TOKEN" not in os.environ
    assert os.environ["LENS_API_TOKEN"] == "https://already.example"
    assert "SERPAPI_API_KEY" not in os.environ
    monkeypatch.delenv("EPO_OPS_KEY")
    monkeypatch.delenv("PATENT_FETCH_EMAIL")


def test_missing_directory_is_fine(tmp_path: Path) -> None:
    assert load_env(tmp_path / "absent") == []
    assert load_env() == []
