"""Patent publication numbers, normalised so that every provider's spelling means one document.

    normalize("US 7,861,317 B2")                                   -> US7861317 (kind B2)
    normalize("https://patents.google.com/patent/EP2264377A2/en")  -> EP2264377 (kind A2)
    normalize("WO 2012/016009")                                    -> WO2012016009
    normalize("US 2012/0029596 A1")                                -> US20120029596 (kind A1)

## Identity

A patent is keyed by **country code plus number, without the kind code** (`US7861317`). The kind
code (`A1`, `B2`, ...) says which publication of that number it is: an EP application and its
grant share a number and differ only in kind, so they are one entry here, and the record says
which kind was retrieved. A US application publication and the patent granted from it have
different numbers, so they are two entries; the record of each names the other when a provider
says so.

## The one spelling difference that matters

A US application publication is `US 2012/0029596 A1` at the USPTO and on Google Patents (a year
and SEVEN digits) but `US2012029596` in the EPO's DOCDB (a year and SIX digits), which is the
spelling EPO OPS and Europe PMC use. The key is the eleven-digit form; `PatentId.docdb` gives the
ten-digit one to the providers that need it.

## What is refused

An **application number** (`US 10/962,874`) is not a publication number and no provider here looks
one up, so it raises rather than being read as a patent with a similar number. A bare number with
no country code raises too: `7861317` exists at more than one office.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

__all__ = ["PatentId", "normalize"]

# Country code, an optional US document-type prefix (design, reissue, plant, ...), digits, and an
# optional kind code: a letter and at most one digit.
_NUMBER = re.compile(r"^([A-Z]{2})(D|RE|PP|H|T|AI)?(\d{4,})([A-Z]\d?)?$")
_US_APPLICATION = re.compile(r"^US\s*\d{2}\s*/\s*\d{3}\s*,?\s*\d{3}$", re.IGNORECASE)
_URL_NUMBER = re.compile(r"/patent/([A-Za-z]{2}[A-Za-z0-9]+)")


@dataclass(frozen=True)
class PatentId:
    country: str
    number: str  # digits, with a US document-type prefix where there is one ("D536139")
    kind: str | None = None

    @property
    def key(self) -> str:
        """What the library stores under: `US7861317`."""
        return f"{self.country}{self.number}"

    @property
    def full(self) -> str:
        """With the kind code when it is known: `US7861317B2`."""
        return self.key + (self.kind or "")

    @property
    def docdb(self) -> str:
        """The EPO's spelling: a US application publication loses the zero after its year."""
        n = self.number
        if self.country == "US" and len(n) == 11 and n[:2] in ("19", "20") and n[4] == "0":
            n = n[:4] + n[5:]
        return f"{self.country}{n}"

    def __str__(self) -> str:
        return self.full


def _from_url(text: str) -> str:
    """The number inside a Google Patents or Espacenet URL, else the text unchanged."""
    if "://" not in text:
        return text
    u = urllib.parse.urlsplit(text)
    m = _URL_NUMBER.search(u.path)
    if m:
        return m.group(1)
    q = urllib.parse.parse_qs(u.query)
    if q.get("CC") and q.get("NR"):  # Espacenet: ?CC=EP&NR=2264377A2&KC=A2
        return q["CC"][0] + q["NR"][0]
    raise ValueError(f"no patent number in the URL {text!r}")


def normalize(identifier: str | PatentId) -> PatentId:
    """Parse a publication number in any common spelling, or raise `ValueError` saying why."""
    if isinstance(identifier, PatentId):
        return identifier
    text = _from_url(identifier.strip())
    if _US_APPLICATION.match(text):
        raise ValueError(
            f"{identifier!r} is a US application number, not a publication number. Use the number "
            "the application was published or granted under (US 2012/0029596 A1, US 7,861,317)."
        )
    compact = re.sub(r"[\s,./:\-]", "", text).upper()
    m = _NUMBER.match(compact)
    if not m:
        if compact.isdigit():
            raise ValueError(
                f"{identifier!r} has no country code; write it as US{compact}, EP{compact}, ..."
            )
        raise ValueError(f"{identifier!r} is not a patent publication number")
    country, prefix, digits, kind = m.groups()
    # DOCDB's ten-digit US application publication becomes the USPTO's eleven-digit one.
    if country == "US" and not prefix and len(digits) == 10 and digits[:2] in ("19", "20"):
        digits = digits[:4] + "0" + digits[4:]
    return PatentId(country, (prefix or "") + digits, kind)
