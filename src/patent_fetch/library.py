"""The library: look in what we already hold, then (only then) ask the providers.

    lib = Library.default()                          # store + providers from the environment
    res = lib.search("cochlear implant electrode array")   # federated: every search provider
    res["hits"], res["providers"]                    # merged hits; per-provider status
    rec = lib.fetch("US7861317B2")                   # the record, description and claims
    lib.text(rec["id"]); lib.claims(rec["id"])

    lib.create_collection("hearing", queries=["cochlear implant electrode array"])
    lib.sweep("hearing")["new"]                      # what those queries had not shown before

No language model runs inside the library: searching, merging, collections and sweeps are string
matching and bookkeeping, so their behaviour is testable and repeatable.

**Index-first is the whole point, and it is tested rather than asserted**: the suite fetches a
patent, fetches it again, deletes the catalogue, fetches it a third time, and asserts the network
was touched only the first time.

## What a search can and cannot tell you

A search returns what the enabled providers matched for these words on this day. **An empty
result is not evidence that no patent exists**, and nothing here is a freedom-to-operate, validity
or infringement opinion: those turn on the claims as a patent attorney reads them. A provider
that did not answer is reported as `unavailable` or `error`, never folded into "nothing found".

## Providers are swappable

`Library(store, providers=[...], search_providers=[...])`, or by name through
`PATENT_FETCH_PROVIDERS` / `PATENT_FETCH_SEARCH_PROVIDERS`. See `providers/__init__.py`.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from . import problems
from .http import Http, HttpClient
from .ids import PatentId, normalize
from .providers import (
    PAUSE_OUTAGE_S,
    REGISTRY,
    Document,
    Patent,
    Provider,
    ProviderUnavailable,
    build,
)
from .store import PREFIX, Store, store_from_env

__all__ = ["INDEX", "RETRY_DAYS", "SEARCH_TTL_DAYS", "Library", "NotFound", "Record"]

Record = dict[str, Any]

INDEX = PREFIX + "index.jsonl"
#: A cached search is served for a week: offices publish weekly, so older is stale.
SEARCH_TTL_DAYS = 7
#: Part of every cached search's key. Raise it when `Patent` gains a field that a search result
#: depends on: a search cached before `priorities` existed has none, and would never fold into a
#: family. Older entries are then simply not found, and expire unread.
SEARCH_CACHE_VERSION = 2
#: A patent held without its claims is asked for again after this long (providers add full text).
RETRY_DAYS = 30

_COLLECTION = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_ROW_FIELDS = (
    "id",
    "kind",
    "title",
    "year",
    "applicants",
    "inventors",
    "classifications",
    "dates",
    "status",
    "url",
)


class NotFound(LookupError):
    """No enabled provider knows this publication number, or nothing is held under it."""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fill(base: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    """`base`, with each field it lacks taken from `other`. The first provider's words win; a
    later one only fills gaps. `dates` and `extra` are filled key by key."""
    out = dict(base)
    for key, value in other.items():
        if key in ("dates", "extra"):
            out[key] = {**value, **(out.get(key) or {})}
        elif out.get(key) in (None, "", [], {}):
            out[key] = value
    return out


def _family_keys(row: dict[str, Any]) -> set[str]:
    """What joins a publication to others of the same invention: the applications it claims
    priority from, and its own application. Spelling differences that are only punctuation or a
    trailing kind letter (`US202463551349P`) are ignored; anything else is left to differ, so two
    services that spell a number differently fail to join rather than join wrongly."""
    numbers = [*(row.get("priorities") or []), row.get("application") or ""]
    keys = {re.sub(r"(?<=\d)[A-Z]$", "", re.sub(r"[^0-9A-Z]", "", n.upper())) for n in numbers}
    return {k for k in keys if len(k) >= 6}


_OFFICE_ORDER = {"US": 0, "EP": 1, "WO": 2}


def _collapse(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per invention: hits that share a priority (or are each other's priority) are
    grouped, one is kept, and the rest are listed under its `also_published_as`.

    This is the EXTENDED family, joined through any shared priority, not the EPO's stricter
    "simple family" of identical priority sets: a US patent and the EP application that claims it
    plus one more priority are different simple families and the same invention to a reader. The
    cost is that a continuation-in-part or a divisional, whose claims differ, folds into its
    parent. Nothing is discarded: every publication is still named, with its own link.

    The one kept is a publication already held with its text, else held, else US, EP, WO, then
    any other office, and the latest of those: the latest is the grant where there is one.
    """
    parent = list(range(len(hits)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[str, int] = {}
    for i, hit in enumerate(hits):
        for key in _family_keys(hit):
            if key in owner:
                parent[find(i)] = find(owner[key])
            else:
                owner[key] = i
    groups: dict[int, list[dict[str, Any]]] = {}
    for i, hit in enumerate(hits):
        groups.setdefault(find(i), []).append(hit)

    out = []
    for members in groups.values():
        best = min(
            members,
            key=lambda h: (
                not h["full_text_in_library"],
                not h["in_library"],
                _OFFICE_ORDER.get(h["id"][:2], 3),
                -(h.get("year") or 0),
            ),
        )
        best["also_published_as"] = [
            {k: h.get(k) for k in ("id", "kind", "year", "url", "found_by", "in_library")}
            for h in members
            if h is not best
        ]
        out.append(best)
    return out


class Library:
    def __init__(
        self,
        store: Store,
        *,
        http: HttpClient | None = None,
        providers: Sequence[Provider] | None = None,
        search_providers: Sequence[Provider] | None = None,
    ) -> None:
        self.store = store
        self.http: HttpClient = http if http is not None else Http()
        self._providers = list(providers) if providers is not None else None
        self._search_providers = list(search_providers) if search_providers is not None else None
        self._opted: dict[str, Provider] = {}
        self._index: dict[str, Record] | None = None
        # One Library may serve many threads (a server runs each call on a worker). The lock
        # covers the catalogue, collections and the pause clock; it is never held across a
        # provider call.
        self._lock = threading.RLock()
        self._paused: dict[str, tuple[float, str]] = {}

    @classmethod
    def default(cls) -> Library:
        """The store named by `PATENT_FETCH_STORE` and providers from the environment. Reads no
        `*.env` file: call `patent_fetch.config.load_env()` first if you keep keys in one."""
        return cls(store_from_env())

    # ------------------------------------------------------------------------- providers

    @property
    def providers(self) -> list[Provider]:
        with self._lock:
            if self._providers is None:
                self._providers = build(self.http, purpose="lookup")
            return self._providers

    @property
    def search_providers(self) -> list[Provider]:
        with self._lock:
            if self._search_providers is None:
                self._search_providers = build(self.http, purpose="search")
            return self._search_providers

    def _opt_in(self, name: str) -> Provider:
        """A provider asked for by name on one search (`also=`), built once so it is reused."""
        with self._lock:
            if name not in self._opted:
                self._opted[name] = build(self.http, names=[name], purpose="search")[0]
            return self._opted[name]

    def _pause(self, name: str, seconds: float, why: str) -> None:
        with self._lock:
            self._paused[name] = (time.monotonic() + seconds, why)

    def _paused_why(self, name: str) -> str | None:
        with self._lock:
            until, why = self._paused.get(name, (0.0, ""))
            left = until - time.monotonic()
            if left <= 0:
                self._paused.pop(name, None)
                return None
        wait = "this process" if left == float("inf") else f"{left:.0f} s"
        return f"not asked for {wait}, after: {why}"

    def paused(self) -> dict[str, str]:
        """Providers being left alone after an outside failure, with the reason."""
        with self._lock:
            names = list(self._paused)
        return {n: why for n in names if (why := self._paused_why(n))}

    # ------------------------------------------------------------------------- catalogue

    def _read_catalogue(self) -> dict[str, Record]:
        self.store.invalidate(INDEX)  # another process may have written it since we cached it
        if not self.store.has(INDEX):
            return {}
        rows = (json.loads(ln) for ln in self.store.get(INDEX).decode().splitlines() if ln.strip())
        return {r["id"]: r for r in rows}

    def index(self, refresh: bool = False) -> dict[str, Record]:
        """The catalogue: one row per held patent, keyed by id. Derived from the records."""
        with self._lock:
            if self._index is None or refresh:
                self._index = self._read_catalogue()
            return dict(self._index)

    def _write_catalogue(self, rows: dict[str, Record]) -> None:
        body = "".join(json.dumps(rows[k], sort_keys=True) + "\n" for k in sorted(rows))
        self.store.put(INDEX, body.encode(), "application/x-ndjson")
        self._index = rows

    def _record(self, row: Record) -> None:
        """Add or replace one row, on the store's latest copy rather than this process's: other
        processes sharing the store add rows too, and writing ours back whole would drop theirs."""
        with self._lock:
            rows = self._read_catalogue()
            rows[row["id"]] = row
            self._write_catalogue(rows)

    def _put_json(self, key: str, obj: Any) -> None:
        self.store.put(key, json.dumps(obj, indent=1, sort_keys=True).encode(), "application/json")

    @staticmethod
    def _doc(key: str, name: str) -> str:
        return f"{PREFIX}docs/{key}/{name}"

    def lookup(self, identifier: str | PatentId) -> Record | None:
        """The catalogue row of a held patent, or None. No network. A row missing from the
        catalogue is rebuilt from the patent's own record, which is the authoritative copy."""
        key = normalize(identifier).key
        row = self.index().get(key)
        if row is not None:
            return row
        if not self.store.has(self._doc(key, "record.json")):
            return None
        stored = json.loads(self.store.get(self._doc(key, "record.json")))
        row = self._row(stored)
        self._record(row)
        return row

    @staticmethod
    def _row(stored: dict[str, Any]) -> Record:
        patent, prov = stored["patent"], stored["provenance"]
        row: Record = {k: patent.get(k) for k in _ROW_FIELDS}
        row["classifications"] = (row["classifications"] or [])[:10]
        row["source"] = prov.get("source")
        row["retrieved_at"] = prov.get("retrieved_at")
        row["has_full_text"] = bool(prov.get("has_full_text"))
        if prov.get("retry_after"):
            row["retry_after"] = prov["retry_after"]
        return row

    def rebuild_index(self) -> int:
        """Recreate the catalogue from the per-patent records. Returns the number of rows."""
        rows: dict[str, Record] = {}
        for key in self.store.keys(PREFIX + "docs/"):
            if key.endswith("/record.json"):
                row = self._row(json.loads(self.store.get(key)))
                rows[row["id"]] = row
        with self._lock:
            self._write_catalogue(rows)
        return len(rows)

    # ----------------------------------------------------------------------------- fetch

    def fetch(
        self, identifier: str | PatentId, *, force: bool = False, collection: str | None = None
    ) -> Record:
        """Get one patent into the library and return its catalogue row, with `from` saying
        whether it was already held (`library`) or just retrieved (`retrieved`).

        A held patent is not asked for again, except one held without description or claims
        whose `retry_after` has passed. `force=True` asks the providers regardless.
        """
        pid = normalize(identifier)
        held = self.lookup(pid)
        if held is not None and not force and not self._retry_due(held):
            row = {**held, "from": "library"}
        else:
            row = {**self._retrieve(pid, held), "from": "retrieved"}
        if collection:
            self.collect(collection, [row["id"]], via="fetch")
        return row

    @staticmethod
    def _retry_due(row: Record) -> bool:
        return (
            not row.get("has_full_text")
            and bool(row.get("retry_after"))
            and row["retry_after"] <= _now()
        )

    def _retrieve(self, pid: PatentId, held: Record | None) -> Record:
        routes: list[dict[str, Any]] = []
        best: Document | None = None
        merged: dict[str, Any] = {}
        for p in self.providers:
            ok, why = p.available()
            paused = self._paused_why(p.name)
            if not ok or paused:
                routes.append(
                    {
                        "provider": p.name,
                        "outcome": "unavailable" if ok else "skipped",
                        "detail": paused or why,
                    }
                )
                continue
            try:
                doc = p.lookup(pid)
            except ProviderUnavailable as e:
                self._pause(p.name, e.pause_s if e.pause_s is not None else PAUSE_OUTAGE_S, str(e))
                routes.append({"provider": p.name, "outcome": "unavailable", "detail": str(e)})
                continue
            except Exception as e:  # noqa: BLE001 -- a defect in one adapter must not stop the rest
                row = problems.record(p.name, "lookup", e, {"id": pid.full})
                routes.append({"provider": p.name, "outcome": "error", "detail": row["error"]})
                continue
            if doc is None:
                routes.append({"provider": p.name, "outcome": "not-known"})
                continue
            merged = _fill(merged, doc.patent.to_json()) if merged else doc.patent.to_json()
            routes.append(
                {"provider": p.name, "outcome": "full-text" if doc.has_text else "record-only"}
            )
            if best is None or (doc.has_text and not best.has_text):
                best = doc
            if doc.has_text:
                break
        if best is None:
            if held is not None:  # a forced or retried lookup found nothing new: keep what we hold
                return held
            tried = "; ".join(
                f"{r['provider']}: {r['outcome']}"
                + (f" ({r['detail']})" if r.get("detail") else "")
                for r in routes
            )
            raise NotFound(
                f"{pid.full}: no provider returned it ({tried or 'no providers enabled'})"
            )
        return self._keep(pid, merged, best, routes)

    def _keep(
        self, pid: PatentId, patent: dict[str, Any], doc: Document, routes: list[dict[str, Any]]
    ) -> Record:
        key = patent["id"]
        prov: dict[str, Any] = {
            "asked_for": pid.full,
            "source": doc.patent.provider,
            "retrieved_at": _now(),
            "routes": routes,
            "has_full_text": doc.has_text,
        }
        if doc.has_text:
            parts = [
                patent.get("title") or key,
                *(
                    f"{h}\n\n{body}"
                    for h, body in (
                        ("ABSTRACT", patent.get("abstract")),
                        ("DESCRIPTION", doc.description),
                        ("CLAIMS", doc.claims),
                    )
                    if body
                ),
            ]
            text = ("\n\n".join(parts) + "\n").encode()
            self.store.put(self._doc(key, "fulltext.txt"), text, "text/plain; charset=utf-8")
            self.store.assert_private(self._doc(key, "fulltext.txt"))
            prov["text_sha256"] = _sha256(text)
            if doc.claims:
                claims = (doc.claims + "\n").encode()
                self.store.put(self._doc(key, "claims.txt"), claims, "text/plain; charset=utf-8")
                prov["claims_sha256"] = _sha256(claims)
        else:
            later = datetime.now(UTC) + timedelta(days=RETRY_DAYS)
            prov["retry_after"] = later.strftime("%Y-%m-%dT%H:%M:%SZ")
        stored = {"patent": patent, "provenance": prov}
        self._put_json(self._doc(key, "record.json"), stored)
        row = self._row(stored)
        self._record(row)
        return row

    def _held(self, identifier: str | PatentId) -> Record:
        row = self.lookup(identifier)
        if row is None:
            raise NotFound(f"{normalize(identifier).full} is not in the library; fetch it first")
        return row

    def record(self, identifier: str | PatentId) -> dict[str, Any]:
        """The full stored record: `patent` (every field) and `provenance` (every route tried)."""
        key = self._held(identifier)["id"]
        return json.loads(self.store.get(self._doc(key, "record.json")))

    def _read(self, identifier: str | PatentId, name: str, what: str) -> str:
        row = self._held(identifier)
        key = self._doc(row["id"], name)
        if not self.store.has(key):
            raise NotFound(f"{row['id']} is held without {what}: no provider that answered has it")
        return self.store.get(key).decode()

    def text(self, identifier: str | PatentId) -> str:
        """Title, abstract, description and claims of a held patent, as one text."""
        return self._read(identifier, "fulltext.txt", "its full text")

    def claims(self, identifier: str | PatentId) -> str:
        """The claims of a held patent: the part that says what the patent covers."""
        return self._read(identifier, "claims.txt", "its claims")

    def search_library(self, query: str = "", *, full_text: bool = False) -> list[Record]:
        """Held patents whose catalogue row (or, with `full_text`, stored text) has every word
        of `query`. No network. An empty query lists everything held."""
        words = query.casefold().split()
        out = []
        for row in sorted(self.index().values(), key=lambda r: r["id"]):
            hay = json.dumps(row, ensure_ascii=False).casefold()
            key = self._doc(row["id"], "fulltext.txt")
            if full_text and row.get("has_full_text") and self.store.has(key):
                hay += self.store.get(key).decode().casefold()
            if all(w in hay for w in words):
                out.append(row)
        return out

    # ---------------------------------------------------------------------------- search

    def search(
        self,
        query: str,
        *,
        providers: Sequence[str] | None = None,
        also: Sequence[str] = (),
        limit: int = 10,
        refresh: bool = False,
        countries: Sequence[str] | None = None,
        since: int | None = None,
        collection: str | None = None,
        families: bool = True,
    ) -> dict[str, Any]:
        """Ask every search provider and merge what answers.

        `providers` names the set for this search; `also` adds metered ones (`serpapi`) to the
        usual set. `limit` is per provider. `countries` (office codes) and `since` (a publication
        year) filter the merged hits afterwards; a hit whose year no provider gave is kept, since
        it cannot be shown to be older. With `collection`, each hit says whether that collection
        has `seen` it or lists it as a `member`, and every hit is recorded as seen.

        One invention is usually published several times (an application, its grant, the same
        filing at other offices). With `families` (the default) those are one hit, with the other
        publications named in its `also_published_as`; see `_collapse` for how they are joined
        and which one is kept. `families=False` returns every publication as its own hit.

        Returns `{"query", "hits", "filtered_out", "collapsed", "providers", "broken"}`. Each
        provider reports `ok`, `cache`, `skipped` (no key), `unavailable` (an outside problem) or
        `error` (a defect, also listed in `broken`). **Read the statuses before reading "no hits"
        as "no patents".**
        """
        if not query.strip():
            raise ValueError("an empty query would match everything; give some words")
        if providers is not None:
            asked = build(self.http, names=providers, purpose="search")
        else:
            asked = list(self.search_providers)
        asked += [self._opt_in(n) for n in also if n not in {p.name for p in asked}]

        reports: list[dict[str, Any]] = []
        broken: list[dict[str, Any]] = []
        merged: dict[str, dict[str, Any]] = {}
        for p in asked:
            report, hits = self._ask(p, query, limit, refresh)
            reports.append(report)
            if report.get("broken"):
                broken.append(
                    {"provider": p.name, "error": report["detail"], "log": str(problems.log_path())}
                )
            for hit in hits:
                row = hit.to_json()
                if hit.id in merged:
                    found_by = merged[hit.id]["found_by"]
                    merged[hit.id] = {**_fill(merged[hit.id], row), "found_by": [*found_by, p.name]}
                else:
                    merged[hit.id] = {**row, "found_by": [p.name]}

        wanted = {c.upper() for c in countries} if countries else None
        hits_out = []
        held = self.index()
        for row in merged.values():
            if wanted and row["id"][:2] not in wanted:
                continue
            if since and row.get("year") and row["year"] < since:
                continue
            row.pop("provider", None)
            row["in_library"] = row["id"] in held
            row["full_text_in_library"] = bool(held.get(row["id"], {}).get("has_full_text"))
            hits_out.append(row)
        # Hits more than one provider found come first; within that, provider order is kept.
        hits_out.sort(key=lambda r: -len(r["found_by"]))
        publications = len(hits_out)
        if families:
            hits_out = _collapse(hits_out)
        result: dict[str, Any] = {
            "query": query,
            "hits": hits_out,
            "filtered_out": len(merged) - publications,
            "collapsed": publications - len(hits_out),
            "providers": reports,
            "broken": broken,
        }
        if collection:
            self._note_seen(collection, hits_out)
        return result

    def _ask(
        self, p: Provider, query: str, limit: int, refresh: bool
    ) -> tuple[dict[str, Any], list[Patent]]:
        report: dict[str, Any] = {"provider": p.name, "status": "ok", "hits": 0}
        ok, why = p.available()
        if not ok:
            return {**report, "status": "skipped", "detail": why}, []
        digest = _sha256(json.dumps([SEARCH_CACHE_VERSION, query, limit]).encode())
        cache_key = f"{PREFIX}searches/{p.name}/{digest}.json"
        if not refresh and self.store.has(cache_key):
            cached = json.loads(self.store.get(cache_key))
            oldest = datetime.now(UTC) - timedelta(days=SEARCH_TTL_DAYS)
            if cached["at"] >= oldest.strftime("%Y-%m-%dT%H:%M:%SZ"):
                hits = [Patent.from_json(h) for h in cached["hits"]]
                return {**report, "status": "cache", "hits": len(hits), "at": cached["at"]}, hits
        paused = None if refresh else self._paused_why(p.name)
        if paused:
            return {**report, "status": "unavailable", "paused": True, "detail": paused}, []
        try:
            hits = p.search(query, limit=limit)
        except ProviderUnavailable as e:
            self._pause(p.name, e.pause_s if e.pause_s is not None else PAUSE_OUTAGE_S, str(e))
            return {**report, "status": "unavailable", "detail": str(e)}, []
        except Exception as e:  # noqa: BLE001 -- a defect in one adapter must not stop the rest
            row = problems.record(p.name, "search", e, {"query": query})
            return {**report, "status": "error", "broken": True, "detail": row["error"]}, []
        self._put_json(
            cache_key, {"at": _now(), "query": query, "hits": [h.to_json() for h in hits]}
        )
        if not p.verified:
            report["note"] = "adapter not yet verified against the live service"
        return {**report, "hits": len(hits)}, hits

    # ----------------------------------------------------------------------- collections

    @staticmethod
    def _collection_key(name: str) -> str:
        if not _COLLECTION.match(name):
            raise ValueError(
                f"collection name {name!r}: use lower-case letters, digits and hyphens (max 64)"
            )
        return f"{PREFIX}collections/{name}.json"

    def _load_collection(self, name: str) -> dict[str, Any]:
        key = self._collection_key(name)
        self.store.invalidate(key)
        if not self.store.has(key):
            raise NotFound(f"no collection named {name!r}; create it first")
        return json.loads(self.store.get(key))

    def create_collection(
        self, name: str, *, description: str = "", queries: Sequence[str] = ()
    ) -> dict[str, Any]:
        """A named area of interest: the queries that define it, the patents you decided belong
        to it (`members`), and every patent its searches have shown (`seen`). Creating one that
        exists adds the queries and, when given, replaces the description."""
        key = self._collection_key(name)
        with self._lock:
            self.store.invalidate(key)
            if self.store.has(key):
                c = json.loads(self.store.get(key))
            else:
                c = {
                    "name": name,
                    "description": "",
                    "created": _now(),
                    "queries": [],
                    "members": {},
                    "seen": {},
                    "swept": None,
                }
            c["description"] = description or c["description"]
            c["queries"] = list(dict.fromkeys([*c["queries"], *(q.strip() for q in queries)]))
            self._put_json(key, c)
        return self.collection(name)

    def collection(self, name: str) -> dict[str, Any]:
        """One collection, its members joined to what the library holds of each."""
        c = self._load_collection(name)
        held = self.index()
        members = [
            {
                "id": pid,
                **m,
                "title": held.get(pid, {}).get("title") or m.get("title"),
                "in_library": pid in held,
                "full_text_in_library": bool(held.get(pid, {}).get("has_full_text")),
            }
            for pid, m in sorted(c["members"].items())
        ]
        return {**c, "members": members, "seen": len(c["seen"])}

    def list_collections(self) -> list[dict[str, Any]]:
        out = []
        for key in self.store.keys(PREFIX + "collections/"):
            c = json.loads(self.store.get(key))
            out.append(
                {
                    "name": c["name"],
                    "description": c["description"],
                    "queries": len(c["queries"]),
                    "members": len(c["members"]),
                    "seen": len(c["seen"]),
                    "swept": c["swept"],
                }
            )
        return out

    def collect(self, name: str, identifiers: Sequence[str], *, via: str = "") -> dict[str, Any]:
        """List patents as members of a collection. Nothing is retrieved: a member need not be
        held, and `fetch(collection=)` both retrieves and lists."""
        ids = [normalize(i).key for i in identifiers]
        with self._lock:
            c = self._load_collection(name)
            for pid in ids:
                c["members"].setdefault(pid, {"added": _now(), "via": via})
                c["seen"].setdefault(pid, _now())
            self._put_json(self._collection_key(name), c)
        return self.collection(name)

    def uncollect(self, name: str, identifiers: Sequence[str]) -> dict[str, Any]:
        """Remove members. They stay `seen`, so a sweep does not report them as new again."""
        ids = [normalize(i).key for i in identifiers]
        with self._lock:
            c = self._load_collection(name)
            for pid in ids:
                c["members"].pop(pid, None)
            self._put_json(self._collection_key(name), c)
        return self.collection(name)

    def _note_seen(self, name: str, hits: list[dict[str, Any]]) -> None:
        """Mark each hit `new`/`member` for this collection, then record all of them as seen.

        A hit that stands for a family is new only if NO publication of the family was seen
        before: the grant of an application already seen is the same invention, not a new one.
        Every publication of the family is recorded as seen."""
        with self._lock:
            c = self._load_collection(name)
            for hit in hits:
                ids = [hit["id"], *(m["id"] for m in hit.get("also_published_as", []))]
                hit["new"] = not any(i in c["seen"] for i in ids)
                hit["member"] = any(i in c["members"] for i in ids)
                for i in ids:
                    c["seen"].setdefault(i, _now())
            if hits:
                self._put_json(self._collection_key(name), c)

    def sweep(
        self, name: str, *, limit: int = 25, also: Sequence[str] = (), refresh: bool = True
    ) -> dict[str, Any]:
        """Run every query of a collection again and report what it had not shown before.

        This is how an area is kept current: `new` holds each patent no earlier search for this
        collection returned. Searches are asked fresh (`refresh`), not from the week's cache. A
        query whose provider was `unavailable` may be hiding new patents; the per-query provider
        reports are returned so that can be seen, and `complete` is False when any did not answer.
        """
        c = self._load_collection(name)
        if not c["queries"]:
            raise ValueError(f"collection {name!r} has no queries; add one before sweeping")
        new: dict[str, dict[str, Any]] = {}
        runs, broken, complete = [], [], True
        for query in c["queries"]:
            res = self.search(query, limit=limit, also=also, refresh=refresh, collection=name)
            fresh = [h for h in res["hits"] if h.get("new")]
            for h in fresh:
                new.setdefault(h["id"], {**h, "query": query})
            broken += res["broken"]
            complete = complete and all(
                r["status"] in ("ok", "cache", "skipped") for r in res["providers"]
            )
            runs.append(
                {
                    "query": query,
                    "hits": len(res["hits"]),
                    "new": len(fresh),
                    "providers": res["providers"],
                }
            )
        with self._lock:
            latest = self._load_collection(name)
            latest["swept"] = _now()
            self._put_json(self._collection_key(name), latest)
        return {
            "collection": name,
            "queries": runs,
            "new": list(new.values()),
            "complete": complete,
            "broken": broken,
        }

    # ---------------------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        rows = self.index(refresh=True).values()
        return {
            "store": type(self.store).__name__,
            "patents": len(rows),
            "with_full_text": sum(1 for r in rows if r.get("has_full_text")),
            "collections": len(self.store.keys(PREFIX + "collections/")),
            "lookup_providers": [p.name for p in self.providers],
            "search_providers": [p.name for p in self.search_providers],
            "known_providers": list(REGISTRY),
            "paused": self.paused(),
            "problems_logged": len(problems.recent(0)),
        }
