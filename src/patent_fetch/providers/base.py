"""What a provider is, so any one of them can be added, removed or swapped by name.

A provider does at most two things, and says which:

- **search**: turn a query into `Patent`s (number, title, abstract, parties, dates, classes)
- **lookup**: given one publication number, return its `Document`: the record and, where the
  service carries them, the description and claims

Storage, merging and deciding what is already held are the library's job, not a provider's. That
is what makes providers swappable: a provider never writes to the store and never decides which
of two records wins.

## A provider reports; it does not conclude

A legal status, an assignee or a priority date is passed on in the service's own words, under the
service's name. None of them is a legal conclusion, and several services say so about their own
data. Nothing here computes whether a patent is in force.

## Failure is per provider, and loud

A 429 or a missing key makes ONE provider `unavailable` or `skipped`, reported by name. It never
empties a federated search silently: "nothing found" and "the service refused us" are different
answers, and only the first is about the patents.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

from ..http import HttpClient, Response
from ..ids import PatentId

__all__ = [
    "PAUSE_OUTAGE_S",
    "PAUSE_QUOTA_S",
    "PAUSE_RATE_LIMIT_S",
    "Document",
    "Patent",
    "Provider",
    "ProviderBroken",
    "ProviderUnavailable",
]

#: How long a provider is left alone after it fails, in seconds (`Library` keeps the clock).
PAUSE_OUTAGE_S = 60.0  # unreachable, a 5xx, a refusal: ask again soon
PAUSE_RATE_LIMIT_S = 300.0  # a 429: asking again at once is how a limit becomes a ban
PAUSE_QUOTA_S = float("inf")  # a spent allowance: not again in this process


class ProviderUnavailable(RuntimeError):
    """An OUTSIDE problem: the service is down, refused us, rate-limited us or has no allowance
    left. Reported per provider, never swallowed, and never a reason to stop: the other providers
    have answered. `pause_s` says how long to leave the provider alone (None: `PAUSE_OUTAGE_S`).
    """

    def __init__(self, message: str, *, pause_s: float | None = None) -> None:
        super().__init__(message)
        self.pause_s = pause_s


class ProviderBroken(RuntimeError):
    """A DEFECT, here or in how we use the service: it rejected what we sent, or answered in a
    shape this code does not read. Waiting will not fix it, so it is not `unavailable`: it is
    reported as `error`, named in the search's `broken` list and logged with its traceback
    (`problems.py`), so it gets fixed instead of being read as "nothing found".
    """


@dataclass
class Patent:
    id: str  # country + number, no kind code: "US7861317" (see ids.py)
    provider: str
    kind: str | None = None
    title: str | None = None
    abstract: str | None = None
    year: int | None = None  # of publication, when the provider gives no full date
    applicants: list[str] = field(default_factory=list)  # assignees, as the provider names them
    inventors: list[str] = field(default_factory=list)
    #: ISO dates keyed `priority`, `filing`, `publication`, `grant`: only those the provider gave.
    dates: dict[str, str] = field(default_factory=dict)
    classifications: list[str] = field(default_factory=list)  # CPC or IPC symbols
    status: str | None = None  # the provider's wording, not a legal conclusion
    url: str | None = None  # a page a person can open
    cites: list[str] = field(default_factory=list)  # publication numbers this one cites
    cited_by: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Patent:
        return cls(**d)


@dataclass
class Document:
    """One publication as a lookup returned it. `description` and `claims` are None where the
    service does not carry them for this office, which is common and is not a failure."""

    patent: Patent
    description: str | None = None
    claims: str | None = None

    @property
    def has_text(self) -> bool:
        return bool(self.description or self.claims)


class Provider:
    name: str = ""
    label: str = ""
    can_search: ClassVar[bool] = False
    can_lookup: ClassVar[bool] = False
    needs: tuple[str, ...] = ()  # environment variables that MUST be set
    min_interval_s: float = 0.0  # polite spacing between this provider's requests
    timeout_s: float = 20.0  # one slow provider must not stall a federated search
    coverage: str = ""  # one line: which offices and which parts of a document
    terms: str = ""  # one line: what the service allows and what it costs
    metered: bool = False  # True where each call is paid for: asked only when named
    #: False where the adapter was written from the service's documentation and has not yet been
    #: run against it. Said in `describe()` so nobody reads an untested adapter as a tested one.
    verified: bool = True

    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self._last = 0.0
        self.calls = 0
        # Requests are spaced per provider. Two threads working out their wait from the same
        # `_last` would both go at once, which is how a per-minute limit gets broken.
        self._pace = threading.Lock()

    def available(self) -> tuple[bool, str]:
        missing = [k for k in self.needs if not os.environ.get(k)]
        return (False, f"needs {', '.join(missing)}") if missing else (True, "")

    def _space(self) -> None:
        with self._pace:
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.calls += 1

    def _checked(self, r: Response) -> Response:
        if r.status == 429:
            raise ProviderUnavailable(
                f"{self.name} rate-limited us (HTTP 429)", pause_s=PAUSE_RATE_LIMIT_S
            )
        return r

    def _get(self, url: str, headers: dict[str, str] | None = None) -> Response:
        self._space()
        try:
            r = self.http.get(url, headers, timeout=self.timeout_s, retries=1)
        except ConnectionError as e:
            raise ProviderUnavailable(f"{self.name} unreachable: {self._brief(e)}") from e
        return self._checked(r)

    def _post(self, url: str, body: bytes, headers: dict[str, str] | None = None) -> Response:
        self._space()
        try:
            r = self.http.post(url, body, headers, timeout=self.timeout_s)
        except ConnectionError as e:
            raise ProviderUnavailable(f"{self.name} unreachable: {self._brief(e)}") from e
        return self._checked(r)

    @staticmethod
    def _brief(e: Exception) -> str:
        """The end of a transport error, without the URL in front of it: a URL can carry a key."""
        return str(e).rsplit(": ", 1)[-1][-120:]

    def _answered(self, r: Response) -> Response:
        """The answer to a QUERY, or the reason there is none.

        A query endpoint answers 200 with an empty list when nothing matches, so anything else is
        a failure, and returning `[]` for it would report an outage or a defect as "no results".
        A 5xx or a 403 is the service's side (`ProviderUnavailable`); a 401 or any other 4xx means
        it rejected our key or our request (`ProviderBroken`). Not for lookups by number, where a
        404 is an honest "not known".
        """
        if r.status == 200:
            return r
        if r.status >= 500 or r.status == 403:
            raise ProviderUnavailable(f"{self.name} answered HTTP {r.status}")
        detail = r.body[:160].decode("utf-8", "replace").strip()
        raise ProviderBroken(f"{self.name} rejected our request (HTTP {r.status}): {detail}")

    def search(self, query: str, *, limit: int = 10) -> list[Patent]:
        raise NotImplementedError(f"{self.name} does not search")

    def lookup(self, pid: PatentId) -> Document | None:
        raise NotImplementedError(f"{self.name} does not look up")

    def describe(self) -> dict[str, Any]:
        ok, why = self.available()
        return {
            "name": self.name,
            "label": self.label,
            "search": self.can_search,
            "lookup": self.can_lookup,
            "needs": list(self.needs),
            "available": ok,
            "why": why,
            "metered": self.metered,
            "verified": self.verified,
            "coverage": self.coverage,
            "terms": self.terms,
        }
