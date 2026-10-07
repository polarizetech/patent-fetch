"""The two providers that need no account: Europe PMC and Google Patents' document pages.

Both were run live on 2026-10-07; the fixtures in `tests/fixtures/` are cut from what they sent.
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from html.parser import HTMLParser
from typing import Any

from ..ids import PatentId, normalize
from .base import Document, Patent, Provider, ProviderBroken, ProviderUnavailable

__all__ = ["EuropePMC", "GooglePatents", "number_or_none"]


def number_or_none(text: str | None) -> PatentId | None:
    """A provider's spelling of a publication number, or None when it is not one."""
    if not text:
        return None
    try:
        return normalize(text)
    except ValueError:
        return None


# ------------------------------------------------------------------------------- Europe PMC


class EuropePMC(Provider):
    """Europe PMC's patent records (`SRC:PAT`): the EPO's biomedical and life-science patents,
    **published up to 2012 and not added to since**.

    Counted 2026-10-07 with `SRC:PAT AND PUB_YEAR:<year>`: 373,750 records for 2011, 126,514 for
    2012, and none for any later year; 4,229,296 in all. So this is an archive: useful as prior
    art, which only has to be old, and useless for what was filed recently.

    Measured 2026-10-07, no key: `SRC:PAT AND "cochlear implant"` answered 200 with 633 records;
    a record carries title, abstract, inventors, applicant, IPC and EPO classes, application and
    priority numbers, and links to Espacenet and SureChEMBL. It does not carry the description or
    the claims, and it has no patent outside the life sciences: US7861317 (a nose cover) is not
    in it. The record id is the DOCDB number without a kind code.
    """

    name = "europepmc"
    label = "Europe PMC patents"
    can_search = True
    can_lookup = True
    min_interval_s = 0.2
    coverage = "life-science patents published up to 2012 (an archive); abstract, no claims"
    terms = "free, no key; Europe PMC asks for reasonable use"
    BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"

    def search(self, query: str, *, limit: int = 10) -> list[Patent]:
        q = urllib.parse.quote(f"SRC:PAT AND ({query})")
        size = max(1, min(limit, 100))
        r = self._answered(
            self._get(f"{self.BASE}/search?query={q}&format=json&resultType=core&pageSize={size}")
        )
        rows = json.loads(r.body).get("resultList", {}).get("result", [])
        return [p for p in (self._patent(row) for row in rows) if p]

    def lookup(self, pid: PatentId) -> Document | None:
        r = self._get(f"{self.BASE}/article/PAT/{pid.docdb}?resultType=core&format=json")
        if r.status == 404:
            return None
        row = json.loads(self._answered(r).body).get("result")
        patent = self._patent(row) if row else None
        return Document(patent) if patent else None

    def _patent(self, row: dict[str, Any]) -> Patent | None:
        pid = number_or_none(row.get("id"))
        if pid is None:
            return None
        details = row.get("patentDetails") or {}
        dates: dict[str, str] = {}
        filed = (details.get("application") or {}).get("applicationDate")
        if filed:
            dates["filing"] = filed
        priorities = [
            p["priorityDate"]
            for p in (details.get("priorityList") or {}).get("priority", [])
            if p.get("priorityDate")
        ]
        if priorities:
            dates["priority"] = min(priorities)
        classes = [
            c["classification"]
            for c in (details.get("classifierList") or {}).get("classifier", [])
            if c.get("classification")
        ]
        year = row.get("pubYear")
        return Patent(
            id=pid.key,
            provider=self.name,
            kind=details.get("typeCode") or pid.kind,
            title=row.get("title"),
            abstract=row.get("abstractText"),
            year=int(year) if year and str(year).isdigit() else None,
            applicants=[row["affiliation"]] if row.get("affiliation") else [],
            inventors=[
                a["fullName"]
                for a in (row.get("authorList") or {}).get("author", [])
                if a.get("fullName")
            ],
            dates=dates,
            classifications=list(dict.fromkeys(classes)),
            url=f"https://europepmc.org/article/PAT/{row['id']}",
        )


# --------------------------------------------------------------------------- Google Patents


class _Text(HTMLParser):
    """The readable text of an HTML fragment, one block element per line."""

    _BLOCK = frozenset({"div", "p", "heading", "li", "br", "claim-statement", "h2", "tr"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _text(fragment: str) -> str:
    p = _Text()
    p.feed(fragment)
    lines = (re.sub(r"\s+", " ", ln).strip() for ln in "".join(p.parts).split("\n"))
    return "\n".join(ln for ln in lines if ln)


class GooglePatents(Provider):
    """One patent's page on Google Patents, read by number. **Lookup only: it never searches.**

    Google Patents has no API. Its `robots.txt` (read 2026-10-07) is `Disallow: /*` with
    `Allow: /patent/`, so a document page may be requested and the search (`/xhr/query`) may not;
    this adapter builds one URL, `/patent/<number>/en`, and nothing else. Searching Google
    Patents goes through `serpapi` (paid) or Google's BigQuery dataset, never through here.

    The page is HTML and this is a scrape of it, which is the first kind of adapter to break: a
    page that answers 200 without a publication number raises `ProviderBroken` rather than
    returning an empty record. Measured 2026-10-07 on US7861317B2, WO2012016009A1 and
    EP2264377A2: each carried the description and the claims in English; an unknown number
    answered 404. A number without its kind code is served the latest publication.
    """

    name = "google-patents"
    label = "Google Patents document page"
    can_lookup = True
    min_interval_s = 2.0
    coverage = "100+ offices; description and claims, machine-translated where not English"
    terms = (
        "no API and no key; robots.txt allows /patent/ pages only, so one page per lookup, "
        "spaced 2 s; do not republish what it serves"
    )
    BASE = "https://patents.google.com/patent"

    def lookup(self, pid: PatentId) -> Document | None:
        url = f"{self.BASE}/{pid.full}/en"
        r = self._get(url)
        if r.status == 404:
            return None
        if r.status != 200:
            # Google answers automated traffic it dislikes with 429 or 503; neither is a defect.
            raise ProviderUnavailable(f"{self.name} answered HTTP {r.status}")
        return self.parse(r.body.decode("utf-8", "replace"), url)

    def parse(self, page: str, url: str) -> Document:
        found = number_or_none(_first(page, r'<dd itemprop="publicationNumber">([^<]+)</dd>'))
        if found is None:
            raise ProviderBroken(f"{self.name}: no publication number on the page at {url}")
        kind = _first(page, r'<meta itemprop="kindCode" content="([^"]*)"') or found.kind
        dates = {
            key: value
            for key, prop in (
                ("priority", "priorityDate"),
                ("filing", "filingDate"),
                ("publication", "publicationDate"),
            )
            if (value := _first(page, rf'<time itemprop="{prop}" datetime="([^"]+)"'))
        }
        # A full CPC symbol has a slash; the page also lists every ancestor (A, A62, A62B).
        classes = [c for c in _all(page, r'<span itemprop="Code">([^<]+)</span>') if "/" in c]
        patent = Patent(
            id=found.key,
            provider=self.name,
            kind=kind or None,
            title=_first(page, r'<meta name="DC.title" content="([^"]*)"'),
            abstract=self._section(page, "abstract"),
            year=int(dates["publication"][:4]) if "publication" in dates else None,
            applicants=_all(page, r'<dd itemprop="assigneeOriginal" repeat>(.*?)</dd>'),
            inventors=_all(page, r'<dd itemprop="inventor" repeat>(.*?)</dd>'),
            dates=dates,
            classifications=list(dict.fromkeys(classes)),
            status=_first(page, r'<span itemprop="ifiStatus">([^<]+)</span>'),
            url=url,
            cites=self._references(page, "backwardReferences"),
            cited_by=self._references(page, "forwardReferencesOrig"),
            extra={
                k: v
                for k, v in (
                    ("pdf", _first(page, r'<meta name="citation_pdf_url" content="([^"]+)"')),
                    ("application", _first(page, r'<dd itemprop="applicationNumber">([^<]+)<')),
                    (
                        "current_assignee",
                        _first(page, r'<dd itemprop="assigneeCurrent" repeat>(.*?)</dd>'),
                    ),
                )
                if v
            },
        )
        return Document(patent, self._section(page, "description"), self._section(page, "claims"))

    @staticmethod
    def _section(page: str, prop: str) -> str | None:
        m = re.search(rf'<section itemprop="{prop}"[^>]*>(.*?)</section>', page, re.DOTALL)
        if not m:
            return None
        body = re.sub(r"<h2>.*?</h2>", "", m.group(1), count=1, flags=re.DOTALL)
        return _text(body) or None

    @staticmethod
    def _references(page: str, prop: str) -> list[str]:
        out = []
        for row in re.findall(rf'<tr itemprop="{prop}"[^>]*>(.*?)</tr>', page, re.DOTALL):
            pid = number_or_none(_first(row, r'<span itemprop="publicationNumber">([^<]+)<'))
            if pid:
                out.append(pid.full)
        return list(dict.fromkeys(out))


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _first(page: str, pattern: str) -> str | None:
    m = re.search(pattern, page, re.DOTALL)
    return (_clean(m.group(1)) or None) if m else None


def _all(page: str, pattern: str) -> list[str]:
    found = (_clean(v) for v in re.findall(pattern, page, re.DOTALL))
    return list(dict.fromkeys(v for v in found if v))
