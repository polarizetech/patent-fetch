"""The providers that need an account: EPO Open Patent Services, The Lens, and SerpApi.

EPO OPS was run live on 2026-10-13 and its fixtures are cut from what it sent. **The Lens and
SerpApi have not been run against their services yet** (`verified = False`): no account was at
hand. Each of those two is written from the service's published documentation, its fixture in
`tests/fixtures/` is built from that documentation rather than cut from a live answer, and
`patent-fetch providers` says so. An answer in a shape the adapter does not read raises
`ProviderBroken`, which is logged and reported, never read as "no patents". The first live run of
each belongs in CLAUDE.md under "Honest status", with what it changed.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any

from ..http import HttpClient, Response
from ..ids import PatentId
from .base import (
    PAUSE_QUOTA_S,
    PAUSE_RATE_LIMIT_S,
    Document,
    Patent,
    Provider,
    ProviderBroken,
    ProviderUnavailable,
)
from .keyless import number_or_none

__all__ = ["EpoOps", "Lens", "SerpApi"]


def _iso(yyyymmdd: str | None) -> str | None:
    """`20110104` -> `2011-01-04`; anything else is passed through."""
    d = (yyyymmdd or "").strip()
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 and d.isdigit() else (d or None)


def _unique(values: Iterator[str | None] | list[str | None]) -> list[str]:
    return list(dict.fromkeys(v.strip() for v in values if v and v.strip()))


# ---------------------------------------------------------------------------------- EPO OPS


def _local(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _find(el: ET.Element, *path: str) -> Iterator[ET.Element]:
    """Descendants along `path`, matching local names so the three OPS namespaces do not matter."""
    if not path:
        yield el
        return
    for child in el.iter():
        if child is not el and _local(child) == path[0]:
            yield from _find(child, *path[1:])


def _txt(el: ET.Element | None) -> str | None:
    return (" ".join("".join(el.itertext()).split()) or None) if el is not None else None


def _english(elements: list[ET.Element]) -> ET.Element | None:
    """The English one of several language versions, else the first."""
    for el in elements:
        if (el.get("lang") or "").lower() == "en":
            return el
    return elements[0] if elements else None


class EpoOps(Provider):
    """EPO Open Patent Services 3.2: the EPO's worldwide bibliographic data (DOCDB), with full
    text for EP and WO documents and some other offices.

    Registration is free at developers.epo.org and gives a consumer key and secret; the free tier
    is 4 GB of data a week. An anonymous request is refused: measured 2026-10-07, a search without
    a token answered **403** "rejected due to the violation of Fair Use policy". A token comes
    from `/auth/accesstoken` (client credentials, Basic auth), lasts about 20 minutes and is kept
    in memory only.

    **Run live 2026-10-13**, and every guess from the documentation held: the plain-query CQL, the
    `Range` parameter, the 404 for an empty search (`SERVER.EntityNotFound`), a token good for
    1,199 s, and full text for EP but not US (404 `CLIENT.InvalidCountryCode`, read here as "no
    full text"). One thing it showed that the documentation did not: a cited reference may be an
    application (`dnum-type="application number"`), which is not a publication and is left out
    of `cites`. A search of two records was 42 kB and a description 68 kB of the weekly 4 GB.

    A plain query is sent as `ta all "<query>"` (every word, in title or abstract); a query that
    already uses CQL (`pa=`, `cpc=`, `ti all`, ...) is sent as written. OPS answers an empty
    search with **404**, which is therefore "no results" here, not an error.
    """

    name = "epo-ops"
    label = "EPO Open Patent Services"
    can_search = True
    can_lookup = True
    needs = ("EPO_OPS_KEY", "EPO_OPS_SECRET")
    min_interval_s = 0.5
    coverage = "100+ offices' bibliographic data; description and claims for EP, WO and some others"
    terms = "free with registration (developers.epo.org); 4 GB a week; EPO fair-use charter applies"
    BASE = "https://ops.epo.org/3.2"
    _CQL = re.compile(
        r"\b(ti|ab|ta|txt|pa|in|ia|pn|ap|pr|num|pd|cpc|ipc|cl|ct)\s*(=|\ball\b|\bany\b|\bwithin\b)",
        re.IGNORECASE,
    )

    def __init__(self, http: HttpClient) -> None:
        super().__init__(http)
        self._token: str | None = None
        self._expires = 0.0
        self._auth = threading.Lock()

    def _bearer(self, *, renew: bool = False) -> str:
        with self._auth:
            if self._token and not renew and time.monotonic() < self._expires:
                return self._token
            pair = f"{os.environ['EPO_OPS_KEY']}:{os.environ['EPO_OPS_SECRET']}".encode()
            r = self._post(
                f"{self.BASE}/auth/accesstoken",
                b"grant_type=client_credentials",
                {
                    "Authorization": "Basic " + base64.b64encode(pair).decode(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            if r.status != 200:
                raise ProviderBroken(
                    f"{self.name} refused EPO_OPS_KEY / EPO_OPS_SECRET (HTTP {r.status})"
                )
            answer = json.loads(r.body)
            self._token = str(answer["access_token"])
            self._expires = time.monotonic() + float(answer.get("expires_in", 1200)) - 60
            return self._token

    def _call(self, path: str) -> Response:
        url = f"{self.BASE}/rest-services/{path}"
        r = self._get(url, {"Authorization": f"Bearer {self._bearer()}"})
        if r.status in (400, 401) and b"access_token" in r.body.lower():  # expired on their clock
            r = self._get(url, {"Authorization": f"Bearer {self._bearer(renew=True)}"})
        if r.status == 403:  # the fair-use throttle, or the week's allowance
            raise ProviderUnavailable(
                f"{self.name} refused the request (HTTP 403, fair use or quota)",
                pause_s=PAUSE_RATE_LIMIT_S,
            )
        return r

    def search(self, query: str, *, limit: int = 10) -> list[Patent]:
        cql = query if self._CQL.search(query) else 'ta all "{}"'.format(query.replace('"', " "))
        n = max(1, min(limit, 100))
        r = self._call(f"published-data/search/biblio?q={urllib.parse.quote(cql)}&Range=1-{n}")
        if r.status == 404:
            return []
        out: dict[str, Patent] = {}
        for doc in _find(self._xml(self._answered(r)), "exchange-document"):
            patent = self._patent(doc)
            if patent:
                out.setdefault(patent.id, patent)
        return list(out.values())

    def lookup(self, pid: PatentId) -> Document | None:
        ref = f"published-data/publication/epodoc/{pid.docdb}"
        r = self._call(f"{ref}/biblio")
        if r.status == 404:
            return None
        patents = [
            p
            for p in map(self._patent, _find(self._xml(self._answered(r)), "exchange-document"))
            if p
        ]
        if not patents:
            return None
        # Several kinds can share a number (EP A1 and B1): the one asked for, else the latest.
        patent = next((p for p in patents if pid.kind and p.kind == pid.kind), patents[-1])
        return Document(patent, self._fulltext(ref, "description"), self._fulltext(ref, "claims"))

    def _fulltext(self, ref: str, part: str) -> str | None:
        r = self._call(f"{ref}/{part}")
        if r.status == 404:  # OPS has no full text for this office: common, not a failure
            return None
        versions = list(_find(self._xml(self._answered(r)), part))
        chosen = _english(versions)
        if chosen is None:
            return None
        unit = "claim-text" if part == "claims" else "p"
        lines = [_txt(el) for el in chosen.iter() if _local(el) == unit]
        return "\n".join(ln for ln in lines if ln) or None

    def _xml(self, r: Response) -> ET.Element:
        try:
            return ET.fromstring(r.body)
        except ET.ParseError as e:
            raise ProviderBroken(f"{self.name} did not answer in XML: {e}") from e

    def _patent(self, doc: ET.Element) -> Patent | None:
        pid = number_or_none((doc.get("country") or "") + (doc.get("doc-number") or ""))
        if pid is None:
            return None

        def docdb_date(*path: str) -> str | None:
            dates = [
                _txt(next(_find(d, "date"), None))
                for d in _find(doc, *path, "document-id")
                if d.get("document-id-type") in ("docdb", "epodoc")
            ]
            return _iso(min((d for d in dates if d), default=None))

        def names(group: str, item: str) -> list[str]:
            found = [
                (el.get("data-format"), _txt(next(_find(el, "name"), None)))
                for el in _find(doc, group, item)
            ]
            epodoc = [n for fmt, n in found if fmt == "epodoc"]
            return _unique(epodoc or [n for _, n in found])

        classes = []
        for c in _find(doc, "patent-classification"):
            part = {_local(el): (el.text or "").strip() for el in c}
            if part.get("section") and part.get("main-group"):
                classes.append(
                    f"{part['section']}{part.get('class', '')}{part.get('subclass', '')}"
                    f"{part['main-group']}/{part.get('subgroup', '')}"
                )
        dates = {
            k: v
            for k, v in (
                ("priority", docdb_date("priority-claim")),
                ("filing", docdb_date("application-reference")),
                ("publication", docdb_date("publication-reference")),
            )
            if v
        }
        cited = [
            (_txt(next(_find(d, "country"), None)) or "")
            + (_txt(next(_find(d, "doc-number"), None)) or "")
            for cit in _find(doc, "references-cited", "patcit")
            # A citation can name an APPLICATION (a provisional, say): not a publication number.
            if "application" not in (cit.get("dnum-type") or "")
            for d in _find(cit, "document-id")
            if d.get("document-id-type") == "docdb"
        ]
        abstract = _english(list(_find(doc, "abstract")))

        def epodoc(*path: str) -> list[str]:
            return _unique(
                [
                    _txt(next(_find(d, "doc-number"), None))
                    for d in _find(doc, *path, "document-id")
                    if d.get("document-id-type") == "epodoc"
                ]
            )

        return Patent(
            id=pid.key,
            provider=self.name,
            kind=doc.get("kind"),
            title=_txt(_english(list(_find(doc, "invention-title")))),
            abstract=_txt(abstract),
            year=int(dates["publication"][:4]) if "publication" in dates else None,
            applicants=names("applicants", "applicant"),
            inventors=names("inventors", "inventor"),
            dates=dates,
            classifications=_unique(classes),
            application=next(iter(epodoc("application-reference")), None),
            priorities=epodoc("priority-claim"),
            url=f"https://worldwide.espacenet.com/patent/search?q=pn%3D{pid.docdb}",
            cites=_unique([p.key for p in map(number_or_none, cited) if p]),
            extra={"family": doc.get("family-id")} if doc.get("family-id") else {},
        )


# ------------------------------------------------------------------------------------ Lens


class Lens(Provider):
    """The Lens patent API: 150M+ patent records from 100+ offices, with claims and description
    for many, joined to the scholarly works they cite.

    Access is by token (`LENS_API_TOKEN`), requested at lens.org: free trial and non-commercial
    access exist, commercial use is a paid plan, and the allowance is per plan. An anonymous
    request answered **401** "Missing/Incorrect Authorization Header" (measured 2026-10-07).
    """

    name = "lens"
    label = "The Lens patent API"
    can_search = True
    can_lookup = True
    needs = ("LENS_API_TOKEN",)
    min_interval_s = 1.0
    verified = False
    coverage = "100+ offices; abstract, claims and description where The Lens holds them"
    terms = "token from lens.org; free for trial and non-commercial use, paid plans otherwise"
    URL = "https://api.lens.org/patent/search"
    _SUMMARY = (
        "lens_id",
        "jurisdiction",
        "doc_number",
        "kind",
        "date_published",
        "biblio",
        "abstract",
        "legal_status",
    )

    def _ask(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        r = self._post(
            self.URL,
            json.dumps(body).encode(),
            {
                "Authorization": f"Bearer {os.environ['LENS_API_TOKEN']}",
                "Content-Type": "application/json",
            },
        )
        return json.loads(self._answered(r).body).get("data", [])

    def search(self, query: str, *, limit: int = 10) -> list[Patent]:
        rows = self._ask(
            {
                "query": {
                    "query_string": {
                        "query": query,
                        "fields": ["title", "abstract", "claim"],
                        "default_operator": "and",
                    }
                },
                "size": max(1, min(limit, 100)),
                "include": list(self._SUMMARY),
            }
        )
        return [p for p in (self._patent(row) for row in rows) if p]

    def lookup(self, pid: PatentId) -> Document | None:
        must = [{"term": {"jurisdiction": pid.country}}, {"term": {"doc_number": pid.number}}]
        rows = self._ask({"query": {"bool": {"must": must}}, "size": 10})
        if not rows:
            return None
        row = next((r for r in rows if pid.kind and r.get("kind") == pid.kind), rows[-1])
        patent = self._patent(row)
        if patent is None:
            return None
        claims = [
            " ".join(c.get("claim_text") or [])
            for group in row.get("claims") or []
            for c in group.get("claims") or []
        ]
        description = row.get("description") or {}
        return Document(
            patent,
            description.get("text") if isinstance(description, dict) else None,
            "\n".join(c for c in claims if c) or None,
        )

    def _patent(self, row: dict[str, Any]) -> Patent | None:
        pid = number_or_none(f"{row.get('jurisdiction') or ''}{row.get('doc_number') or ''}")
        if pid is None:
            return None
        biblio = row.get("biblio") or {}
        parties = biblio.get("parties") or {}

        def english(items: list[dict[str, Any]] | None) -> str | None:
            items = items or []
            best = next((i for i in items if i.get("lang") == "en"), items[0] if items else {})
            return best.get("text")

        def names(key: str) -> list[str]:
            return _unique(
                [(p.get("extracted_name") or {}).get("value") for p in parties.get(key) or []]
            )

        legal = row.get("legal_status") or {}
        dates = {
            k: v
            for k, v in (
                (
                    "priority",
                    ((biblio.get("priority_claims") or {}).get("earliest_claim") or {}).get("date"),
                ),
                ("filing", (biblio.get("application_reference") or {}).get("date")),
                ("publication", row.get("date_published")),
                ("grant", legal.get("grant_date")),
            )
            if v
        }
        cited = [
            f"{d.get('jurisdiction') or ''}{d.get('doc_number') or ''}"
            for c in (biblio.get("references_cited") or {}).get("citations") or []
            if (d := (c.get("patcit") or {}).get("document_id"))
        ]
        return Patent(
            id=pid.key,
            provider=self.name,
            kind=row.get("kind"),
            title=english(biblio.get("invention_title")),
            abstract=english(row.get("abstract")),
            year=int(dates["publication"][:4]) if "publication" in dates else None,
            applicants=names("applicants"),
            inventors=names("inventors"),
            dates=dates,
            classifications=_unique(
                [
                    c.get("symbol")
                    for c in (biblio.get("classifications_cpc") or {}).get("classifications") or []
                ]
            ),
            application="{}{}".format(
                (biblio.get("application_reference") or {}).get("jurisdiction") or "",
                (biblio.get("application_reference") or {}).get("doc_number") or "",
            )
            or None,
            priorities=_unique(
                [
                    f"{c.get('jurisdiction') or ''}{c.get('doc_number') or ''}"
                    for c in (biblio.get("priority_claims") or {}).get("claims") or []
                ]
            ),
            status=legal.get("patent_status"),
            url=f"https://www.lens.org/lens/patent/{row['lens_id']}"
            if row.get("lens_id")
            else None,
            cites=_unique([p.key for p in map(number_or_none, cited) if p]),
        )


# --------------------------------------------------------------------------------- SerpApi


class SerpApi(Provider):
    """Google Patents' search, through SerpApi's `google_patents` engine. **Paid, so opt-in.**

    This is the sanctioned way to search Google Patents from a program: Google offers no search
    API and its `robots.txt` disallows the search endpoint. SerpApi charges per search, so it is
    `metered`: a search asks it only when it is named (`also=["serpapi"]`, `--also serpapi`, or
    in `PATENT_FETCH_SEARCH_PROVIDERS`), never by default.

    SerpApi takes the key as a URL parameter. Nothing here stores or logs a URL: a transport
    error is reported without it (`Provider._brief`), and a cached search holds only the hits.
    """

    name = "serpapi"
    label = "Google Patents search via SerpApi"
    can_search = True
    needs = ("SERPAPI_API_KEY",)
    metered = True
    verified = False
    timeout_s = 40.0
    coverage = "Google Patents' search index (100+ offices); title and snippet, no claims"
    terms = "paid per search (serpapi.com), with a small free monthly allowance"
    URL = "https://serpapi.com/search"

    def search(self, query: str, *, limit: int = 10) -> list[Patent]:
        params = urllib.parse.urlencode(
            {
                "engine": "google_patents",
                "q": query,
                "num": max(10, min(limit, 100)),  # the engine takes 10 to 100
                "api_key": os.environ["SERPAPI_API_KEY"],
            }
        )
        r = self._get(f"{self.URL}?{params}")
        if r.status == 401:
            raise ProviderBroken(f"{self.name} refused SERPAPI_API_KEY (HTTP 401)")
        answer = json.loads(self._answered(r).body)
        error = str(answer.get("error") or "")
        if "run out of searches" in error:
            raise ProviderUnavailable(f"{self.name}: {error}", pause_s=PAUSE_QUOTA_S)
        if error and "hasn't returned any results" not in error:
            raise ProviderBroken(f"{self.name}: {error[:200]}")
        hits = [self._patent(row) for row in answer.get("organic_results") or []]
        return [p for p in hits if p][:limit]

    def _patent(self, row: dict[str, Any]) -> Patent | None:
        pid = number_or_none(row.get("publication_number"))
        if pid is None:
            return None
        dates = {
            k: row[f"{k}_date"]
            for k in ("priority", "filing", "publication", "grant")
            if row.get(f"{k}_date")
        }

        def people(value: str | None) -> list[str]:
            return [value] if value else []

        return Patent(
            id=pid.key,
            provider=self.name,
            kind=pid.kind,
            title=row.get("title"),
            year=int(dates["publication"][:4]) if "publication" in dates else None,
            applicants=people(row.get("assignee")),
            inventors=people(row.get("inventor")),
            dates=dates,
            url=row.get("patent_link"),
            extra={"snippet": row["snippet"]} if row.get("snippet") else {},
        )
