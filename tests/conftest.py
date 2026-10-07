"""Shared offline test doubles. Nothing in the suite opens a socket.

`FakeHttp` replaces `patent_fetch.http.Http`: it serves canned `Response`s by URL substring and
counts calls, which is how "a held patent is never retrieved again" is tested.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from patent_fetch import Library, MemoryStore
from patent_fetch.http import Response
from patent_fetch.ids import PatentId
from patent_fetch.providers import REGISTRY, Document, Patent, Provider

FIXTURES = Path(__file__).parent / "fixtures"

DESCRIPTION = "The sense of hearing in human beings involves the use of hair cells in the cochlea."
CLAIMS = "1. A method comprising: fitting a first cochlear implant."


def raw(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture(name: str) -> Any:
    return json.loads(raw(name))


def J(obj: Any, status: int = 200) -> Response:
    return Response(status, {}, json.dumps(obj).encode(), "")


def R(body: bytes | str = b"", status: int = 200) -> Response:
    return Response(status, {}, body.encode() if isinstance(body, str) else body, "")


Route = Response | Callable[[str], Response]


class FakeHttp:
    """Serves the first route whose key is a substring of the URL; 404 otherwise."""

    def __init__(self, routes: Mapping[str, Route] | None = None) -> None:
        self.routes: Mapping[str, Route] = routes or {}
        self.calls = 0
        self.log: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.posts: list[tuple[str, bytes]] = []

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> Response:
        self.calls += 1
        self.log.append(url)
        self.headers.append(dict(headers or {}))
        for pat, resp in self.routes.items():
            if pat in url:
                return resp(url) if callable(resp) else resp
        return Response(404, {}, b"", url)

    def post(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str] | None = None,
        *,
        timeout: float | None = None,
    ) -> Response:
        """Served from the same routes as `get`; the body is kept in `posts`."""
        self.posts.append((url, body))
        return self.get(url, headers)


class DeadHttp:
    calls = 0

    def get(self, *_a: Any, **_k: Any) -> Response:
        raise ConnectionError("GET https://secret.example/?api_key=K failed: timed out")

    def post(self, *_a: Any, **_k: Any) -> Response:
        raise ConnectionError("POST https://secret.example/ failed: timed out")


def patent(number: str = "EP2598092", provider: str = "fake", **kw: Any) -> Patent:
    kw.setdefault("title", "Fitting a bilateral cochlear implant")
    kw.setdefault("year", 2018)
    return Patent(id=number, provider=provider, **kw)


class FakeProvider(Provider):
    """A provider whose answers are given, not fetched."""

    can_lookup = True
    can_search = True

    def __init__(
        self,
        name: str = "fake",
        *,
        hits: list[Patent] | None = None,
        doc: Document | None = None,
        raise_: Exception | None = None,
        needs: tuple[str, ...] = (),
        metered: bool = False,
    ) -> None:
        super().__init__(FakeHttp())
        self.name = name
        self.needs = needs
        self.metered = metered
        self.hits = hits or []
        self.doc = doc
        self._raise = raise_
        self.searches: list[str] = []
        self.lookups = 0

    def search(self, query: str, *, limit: int = 10) -> list[Patent]:
        self.searches.append(query)
        if self._raise:
            raise self._raise
        return self.hits

    def lookup(self, pid: PatentId) -> Document | None:
        self.lookups += 1
        if self._raise:
            raise self._raise
        return self.doc


def full_doc(number: str = "EP2598092", provider: str = "fake") -> Document:
    return Document(patent(number, provider, kind="B1"), DESCRIPTION, CLAIMS)


def make_library(
    *,
    store: Any = None,
    providers: list[Provider] | None = None,
    search_providers: list[Provider] | None = None,
) -> Library:
    return Library(
        store if store is not None else MemoryStore(),
        http=FakeHttp(),
        providers=providers if providers is not None else [FakeProvider(doc=full_doc())],
        search_providers=search_providers if search_providers is not None else [],
    )


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No real configuration, no polite sleeps, and every default directory under tmp_path."""
    for key in list(os.environ):
        if key.startswith("PATENT_FETCH_") or key in (
            "EPO_OPS_KEY",
            "EPO_OPS_SECRET",
            "LENS_API_TOKEN",
            "SERPAPI_API_KEY",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
        ):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PATENT_FETCH_ENV_DIR", str(tmp_path / "no-env"))
    monkeypatch.setenv("PATENT_FETCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PATENT_FETCH_CACHE", str(tmp_path / "cache"))
    for cls in REGISTRY.values():
        monkeypatch.setattr(cls, "min_interval_s", 0.0)
