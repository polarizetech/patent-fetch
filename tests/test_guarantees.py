"""Structural guarantees, checked against the source rather than promised in prose."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "patent_fetch"
FILES = {p.relative_to(SRC).as_posix(): p.read_text() for p in SRC.rglob("*.py")}


def _code(text: str) -> str:
    """Source with docstrings and comments removed."""
    no_docs = re.sub(r'"""[\s\S]*?"""', "", text)
    return "\n".join(line.split("#", 1)[0] for line in no_docs.splitlines())


CODE = {name: _code(text) for name, text in FILES.items()}


def test_no_code_path_sets_an_object_acl() -> None:
    assert not [n for n, c in CODE.items() if "ACL" in c]


def test_no_email_address_is_hard_coded() -> None:
    assert not [n for n, c in CODE.items() if re.search(r"[\w.+-]+@[\w-]+\.\w{2,}", c)]


def test_google_patents_is_read_one_document_page_at_a_time() -> None:
    """Its robots.txt allows /patent/ and disallows the rest: no search endpoint, no scholar."""
    assert not [n for n, c in CODE.items() if "xhr/query" in c or "scholar.google" in c]
    hosts = {n for n, c in CODE.items() if "patents.google.com" in c}
    assert hosts == {"providers/keyless.py"}
    assert CODE["providers/keyless.py"].count("patents.google.com") == 1


def test_no_language_model_and_no_legal_conclusion_is_computed() -> None:
    banned = ("anthropic", "openai", "ollama", "in_force", "freedom_to_operate", "infring")
    assert not [(n, w) for n, c in CODE.items() for w in banned if w in c.lower()]


def test_only_the_http_module_and_store_open_urls() -> None:
    users = sorted(n for n, c in CODE.items() if "urlopen" in c)
    assert users == ["http.py", "store.py"]


def test_the_core_imports_only_the_standard_library() -> None:
    optional = {"store.py": {"boto3", "botocore"}}
    for name, text in FILES.items():
        mods: set[str] = set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
        third_party = {m for m in mods if m not in sys.stdlib_module_names}
        assert third_party <= optional.get(name, set()), (name, third_party)


def test_no_project_of_ours_is_named_in_the_package() -> None:
    """The package is for anyone's research: nothing in it names a bucket, a host or a project."""
    assert not [n for n, c in CODE.items() if re.search(r"digitalocean|polarize", c, re.IGNORECASE)]
