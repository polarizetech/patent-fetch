"""The provider registry. Providers are named, ordered, and swappable without touching code.

    PATENT_FETCH_PROVIDERS=epo-ops,lens,google-patents,europepmc   # lookup order
    PATENT_FETCH_SEARCH_PROVIDERS=europepmc,epo-ops,lens           # federated search set

An unknown name RAISES with the known list, rather than being dropped: a typo that silently
removed a provider would shrink every search with nothing on screen saying so.

Default lookup order is the services that publish an API and its terms first (EPO OPS, The Lens),
then the Google Patents page, then Europe PMC, which carries no claims. A provider whose key is
not set is skipped and reported, so with no account at all a lookup reads the Google Patents page.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from ..http import HttpClient
from .base import (
    PAUSE_OUTAGE_S,
    Document,
    Patent,
    Provider,
    ProviderBroken,
    ProviderUnavailable,
)
from .keyed import EpoOps, Lens, SerpApi
from .keyless import EuropePMC, GooglePatents

__all__ = [
    "DEFAULT_LOOKUP",
    "DEFAULT_SEARCH",
    "PAUSE_OUTAGE_S",
    "REGISTRY",
    "Document",
    "EpoOps",
    "EuropePMC",
    "GooglePatents",
    "Lens",
    "Patent",
    "Provider",
    "ProviderBroken",
    "ProviderUnavailable",
    "SerpApi",
    "build",
    "describe",
]

REGISTRY: dict[str, type[Provider]] = {
    p.name: p for p in (EuropePMC, EpoOps, Lens, GooglePatents, SerpApi)
}

DEFAULT_LOOKUP = ("epo-ops", "lens", "google-patents", "europepmc")
# `serpapi` is not in the default set: each search is paid for, so a search asks it only when
# told to (`search(also=["serpapi"])`, `--also serpapi`, or by naming it in the set).
DEFAULT_SEARCH = ("europepmc", "epo-ops", "lens")


def _names(names: Sequence[str] | None, env_var: str, default: Sequence[str]) -> list[str]:
    if names is None:
        raw = os.environ.get(env_var)
        names = [n.strip() for n in raw.split(",") if n.strip()] if raw else list(default)
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        raise ValueError(f"unknown provider(s) {unknown}; known: {', '.join(REGISTRY)}")
    return list(names)


def build(
    http: HttpClient, *, names: Sequence[str] | None = None, purpose: str = "lookup"
) -> list[Provider]:
    """Instantiate providers by name (or from the environment, or the defaults) for a purpose."""
    if purpose == "lookup":
        env_var, default = "PATENT_FETCH_PROVIDERS", DEFAULT_LOOKUP
    elif purpose == "search":
        env_var, default = "PATENT_FETCH_SEARCH_PROVIDERS", DEFAULT_SEARCH
    else:
        raise ValueError("purpose is 'lookup' or 'search'")
    out = []
    for n in _names(names, env_var, default):
        cls = REGISTRY[n]
        if not (cls.can_lookup if purpose == "lookup" else cls.can_search):
            raise ValueError(f"provider {n!r} cannot {purpose}")
        out.append(cls(http))
    return out


def describe(http: HttpClient) -> list[dict[str, object]]:
    """Every registered provider: what it can do, what it needs, and its terms. No network."""
    return [cls(http).describe() for cls in REGISTRY.values()]
