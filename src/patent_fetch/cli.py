"""Command line.

    patent-fetch search "<query>" [--providers a,b] [--also serpapi]   every search provider, merged
                [--country US,EP] [--since 2015] [--collection C] [-n N] [--refresh]
    patent-fetch fetch <number> [...] [--collection C] [--force]       library first, then providers
    patent-fetch text <number>   title, abstract, description, claims
    patent-fetch claims <number>                                       the claims alone
    patent-fetch record <number>   every field, every route tried
    patent-fetch library ["<query>"] [--full-text]                     search only what is held
    patent-fetch collection [name] [--create] [--description D] [--query Q ...]
    patent-fetch collect <name> <number> [...] [--remove]              list patents in a collection
    patent-fetch sweep <name> [--also serpapi] [-n N]   re-run its queries: what is new
    patent-fetch providers                                             what is on, what each needs
    patent-fetch problems [-n N] [--trace]                             provider defects logged here
    patent-fetch status | rebuild-index

`--json` before the command prints the result as JSON. `python -m patent_fetch ...` is the same
program. A number is a publication number in any common spelling, or a Google Patents URL.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from . import problems
from .config import load_env
from .http import Http
from .library import Library, NotFound
from .providers import describe

__all__ = ["build_parser", "main"]

LEGEND = "  ■ full text in library   □ in library, no full text   + new to the collection"
CAVEAT = (
    "  A search shows what these providers matched for these words. No hits is not evidence that\n"
    "  no patent exists, and nothing here is a freedom-to-operate opinion."
)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="patent-fetch", description="Find existing patents and keep what you read."
    )
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search", help="federated search across providers")
    p.add_argument("query")
    p.add_argument("--providers", help="comma-separated provider names, instead of the usual set")
    p.add_argument("--also", help="comma-separated metered providers to add (serpapi)")
    p.add_argument("--country", help="comma-separated office codes to keep (US,EP,WO)")
    p.add_argument("--since", type=int, help="keep hits published in or after this year")
    p.add_argument("--collection", help="mark hits new to this collection, and record them seen")
    p.add_argument("-n", type=int, default=10, help="hits per provider")
    p.add_argument("--refresh", action="store_true", help="ignore cached answers")
    p.add_argument(
        "--all-publications",
        action="store_true",
        help="list every publication, instead of one entry per invention",
    )

    p = sub.add_parser("fetch", help="get patents into the library")
    p.add_argument("ids", nargs="+")
    p.add_argument("--force", action="store_true", help="ask the providers even if held")
    p.add_argument("--collection", help="also list the patents in this collection")

    for name in ("text", "claims", "record"):
        sub.add_parser(name).add_argument("id")

    p = sub.add_parser("library", help="search held patents (no network)")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--full-text", action="store_true")

    p = sub.add_parser("collection", help="list collections, or show / create one")
    p.add_argument("name", nargs="?", default="")
    p.add_argument("--create", action="store_true")
    p.add_argument("--description", default="")
    p.add_argument("--query", action="append", default=[], help="a query to sweep (repeatable)")

    p = sub.add_parser("collect", help="list patents in a collection (no retrieval)")
    p.add_argument("name")
    p.add_argument("ids", nargs="+")
    p.add_argument("--remove", action="store_true")

    p = sub.add_parser("sweep", help="re-run a collection's queries and show what is new")
    p.add_argument("name")
    p.add_argument("--also", help="comma-separated metered providers to add (serpapi)")
    p.add_argument("-n", type=int, default=25, help="hits per provider, per query")

    sub.add_parser("providers", help="list providers (no network)")

    p = sub.add_parser("problems", help="provider defects logged on this machine")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--trace", action="store_true")

    sub.add_parser("status")
    sub.add_parser("rebuild-index")
    return ap


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _flag(item: dict[str, Any]) -> str:
    if item.get("full_text_in_library") or item.get("has_full_text"):
        return "■"
    return "□" if item.get("in_library") else " "


def _who(item: dict[str, Any]) -> str:
    names = item.get("applicants") or item.get("inventors") or []
    return names[0][:28] if names else ""


def _hit_line(h: dict[str, Any]) -> str:
    new = "+" if h.get("new") else " "
    found = ",".join(h.get("found_by", []))
    title = " ".join((h.get("title") or "").split())[:64]
    line = f" {_flag(h)}{new} {h['id']:16s} {h.get('year') or '':4} {title}  ({_who(h)}) [{found}]"
    others = [m["id"] for m in h.get("also_published_as", [])]
    return line + (f"\n      also published as {', '.join(others)}" if others else "")


def _provider_lines(reports: list[dict[str, Any]]) -> None:
    for r in reports:
        extra = r.get("detail") or r.get("note") or ""
        print(f"  {r['provider']:16s} {r['status']:12s} {r['hits']:3d} hits  {extra}")


def _warn_broken(broken: list[dict[str, Any]]) -> None:
    for b in broken:
        print(
            f"! BROKEN: {b['provider']}: {b['error']}  (see: patent-fetch problems)",
            file=sys.stderr,
        )


def _search(lib: Library, a: argparse.Namespace) -> Any:
    res = lib.search(
        a.query,
        providers=_split(a.providers) or None,
        also=_split(a.also),
        limit=a.n,
        refresh=a.refresh,
        countries=_split(a.country) or None,
        since=a.since,
        collection=a.collection,
        families=not a.all_publications,
    )
    _warn_broken(res["broken"])
    if a.json:
        return res
    _provider_lines(res["providers"])
    notes = [
        f"{n} {what}"
        for n, what in (
            (res["collapsed"], "further publications of the same inventions folded in"),
            (res["filtered_out"], "filtered out"),
        )
        if n
    ]
    print(f"\n{len(res['hits'])} distinct patents" + (f" ({'; '.join(notes)})" if notes else ""))
    for h in res["hits"]:
        print(_hit_line(h))
    print(LEGEND)
    print(CAVEAT)
    return None


def _fetch(lib: Library, a: argparse.Namespace) -> Any:
    out, failed = [], False
    for ident in a.ids:
        try:
            r = lib.fetch(ident, force=a.force, collection=a.collection)
        except (NotFound, ValueError) as e:
            failed = True
            out.append({"asked_for": ident, "error": str(e)})
            if not a.json:
                print(f"✗ {ident}: {e}")
            continue
        out.append(r)
        if not a.json:
            mark = "✓" if r["has_full_text"] else "~"
            text = "full text" if r["has_full_text"] else "record only, no claims"
            print(
                f"{mark} {r['id']}{r.get('kind') or ''}  [{r['from']}]  {r.get('year')}  "
                f"{r.get('title')}"
            )
            print(f"    source={r['source']}  {text}  status={r.get('status')}")
    a.failed = failed
    return out if a.json else None


def _collection(lib: Library, a: argparse.Namespace) -> Any:
    if not a.name:
        rows = lib.list_collections()
        if a.json:
            return rows
        for c in rows:
            print(
                f"  {c['name']:24s} {c['queries']:2d} queries  {c['members']:4d} members  "
                f"{c['seen']:5d} seen  swept {c['swept'] or 'never'}"
            )
        return None
    if a.create or a.query or a.description:
        c = lib.create_collection(a.name, description=a.description, queries=a.query)
    else:
        c = lib.collection(a.name)
    if a.json:
        return c
    print(
        f"{c['name']}  {len(c['members'])} members, {c['seen']} seen, swept {c['swept'] or 'never'}"
    )
    if c["description"]:
        print(f"  {c['description']}")
    for q in c["queries"]:
        print(f"  query: {q}")
    for m in c["members"]:
        print(f" {_flag(m)} {m['id']:16s} {m.get('title') or ''}")
    return None


def _sweep(lib: Library, a: argparse.Namespace) -> Any:
    res = lib.sweep(a.name, limit=a.n, also=_split(a.also))
    _warn_broken(res["broken"])
    if a.json:
        return res
    for run in res["queries"]:
        print(f"{run['query']}: {run['hits']} hits, {run['new']} new")
        _provider_lines(run["providers"])
    print(f"\n{len(res['new'])} patents not shown to {a.name!r} before")
    for h in res["new"]:
        print(_hit_line(h))
    if not res["complete"]:
        print("  ! a provider did not answer: this sweep may be missing patents; run it again")
    print(CAVEAT)
    return None


def _providers(a: argparse.Namespace) -> Any:
    rows = describe(Http())
    if a.json:
        return rows
    for d in rows:
        can = "+".join(c for c in ("search", "lookup") if d[c]) or "-"
        state = "ready" if d["available"] else f"off ({d['why']})"
        notes = "  PAID, opt-in" if d["metered"] else ""
        notes += "" if d["verified"] else "  NOT YET RUN LIVE"
        print(f"  {d['name']:16s} {can:14s} {state}{notes}")
        print(f"  {'':16s} {d['coverage']}\n  {'':16s} {d['terms']}")
    return None


def _problems(a: argparse.Namespace) -> Any:
    rows = problems.recent(a.n)
    if a.json:
        return rows
    for r in rows:
        print(f"{r['at']}  {r['provider']}  {r['operation']}  {r['error']}")
        if a.trace:
            print(r["traceback"])
    print(f"  {len(rows)} problem(s) in {problems.log_path()}")
    return None


def _run(lib: Library, a: argparse.Namespace) -> Any:
    if a.cmd == "search":
        return _search(lib, a)
    if a.cmd == "fetch":
        return _fetch(lib, a)
    if a.cmd in ("text", "claims"):
        print(lib.text(a.id) if a.cmd == "text" else lib.claims(a.id), end="")
        return None
    if a.cmd == "record":
        return lib.record(a.id)
    if a.cmd == "library":
        rows = lib.search_library(a.query, full_text=a.full_text)
        if a.json:
            return rows
        for r in rows:
            print(
                f" {_flag(r)} {r['id']:16s} {r.get('year') or '':4} {r.get('title') or ''}  "
                f"({_who(r)})"
            )
        print(f"  {len(rows)} held")
        return None
    if a.cmd == "collection":
        return _collection(lib, a)
    if a.cmd == "collect":
        fn = lib.uncollect if a.remove else lib.collect
        c = fn(a.name, a.ids)
        if a.json:
            return c
        print(f"{c['name']}: {len(c['members'])} members")
        return None
    if a.cmd == "sweep":
        return _sweep(lib, a)
    if a.cmd == "status":
        return lib.status()
    n = lib.rebuild_index()
    return {"rebuilt": n}


def main(argv: Sequence[str] | None = None, *, library: Library | None = None) -> int:
    """Run one command. `library` is for tests and embedding; otherwise it is built from the
    environment after the `*.env` files are read."""
    a = build_parser().parse_args(argv)
    load_env()
    a.failed = False
    try:
        if a.cmd == "providers":
            out = _providers(a)
        elif a.cmd == "problems":
            out = _problems(a)
        else:
            out = _run(library if library is not None else Library.default(), a)
    except (NotFound, ValueError) as e:
        print(f"patent-fetch: {e}", file=sys.stderr)
        return 1
    if out is not None:
        print(json.dumps(out, indent=1, ensure_ascii=False))
    return 1 if a.failed else 0
