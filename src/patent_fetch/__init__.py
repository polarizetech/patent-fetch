"""patent-fetch: find existing patents through swappable providers, and keep what you read.

    from patent_fetch import Library

    lib = Library.default()                        # local store + providers from the environment
    res = lib.search("cochlear implant electrode array")
    rec = lib.fetch("US7861317B2")
    claims = lib.claims(rec["id"])

The command line is ``patent-fetch`` (or ``python -m patent_fetch``).
"""

from .http import Http, Response
from .ids import PatentId, normalize
from .library import Library, NotFound
from .providers import (
    REGISTRY,
    Document,
    Patent,
    Provider,
    ProviderBroken,
    ProviderUnavailable,
    build,
    describe,
)
from .store import PREFIX, LocalStore, MemoryStore, NotPrivate, S3Store, Store, store_from_env

__version__ = "0.1.0"

__all__ = [
    "PREFIX",
    "REGISTRY",
    "Document",
    "Http",
    "Library",
    "LocalStore",
    "MemoryStore",
    "NotFound",
    "NotPrivate",
    "Patent",
    "PatentId",
    "Provider",
    "ProviderBroken",
    "ProviderUnavailable",
    "Response",
    "S3Store",
    "Store",
    "__version__",
    "build",
    "describe",
    "normalize",
    "store_from_env",
]
