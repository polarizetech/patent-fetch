"""The library's storage: a private key/value tree under one prefix, `patents/`.

## Backends

- `LocalStore` (the default): a directory on disk, `PATENT_FETCH_DATA_DIR` or
  `~/.local/share/patent-fetch` (`$XDG_DATA_HOME/patent-fetch` when that is set).
- `S3Store`: any S3-compatible bucket (AWS S3, DigitalOcean Spaces, MinIO, Cloudflare R2, ...),
  configured by `PATENT_FETCH_S3_*` environment variables. Needs the `s3` extra (boto3).
- `MemoryStore`: for tests.

Choose with `PATENT_FETCH_STORE=local|s3|memory`; see `store_from_env`.

Everything lives under `patents/`, so the same bucket can also hold another library that keeps to
its own prefix (paper-fetch uses `papers/`).

## Private, and asserted from outside

A patent is a public document, but the copy held here came from a service with terms of its own
(EPO OPS, Lens and Google Patents all restrict redistribution of what they serve), and a store of
what a project searched for says what that project is working on. So the store is private, and
after every write of a document the library calls `assert_private`, which checks the way a
stranger would:

- `LocalStore` writes files `0600` in directories `0700`, and `assert_private` refuses any file or
  directory on the path that is group- or world-accessible.
- `S3Store` never sets an ACL (objects are private by default), and `assert_private` makes an
  unsigned GET that must NOT succeed.

## Layout

    patents/index.jsonl                        catalogue, one line per patent (DERIVED)
    patents/docs/<ID>/record.json              AUTHORITATIVE: the record, routes tried, sha256s
    patents/docs/<ID>/fulltext.txt             abstract, description and claims, when obtained
    patents/docs/<ID>/claims.txt               the claims alone, when obtained
    patents/searches/<provider>/<sha256>.json  cached searches
    patents/collections/<name>.json            a project's queries, members and seen patents

`<ID>` is country code plus number without the kind code (`US7861317`); see `ids.py`.

**The catalogue is derived and the per-patent record is authoritative.** A lookup reads the
catalogue first and falls back to the record, so a stale or deleted catalogue can make a lookup
slower but can never cause a patent to be retrieved twice.

Credentials come from the environment, never from a file in a repository.
"""

from __future__ import annotations

import os
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "PREFIX",
    "LocalStore",
    "MemoryStore",
    "NotPrivate",
    "S3Store",
    "Store",
    "default_cache_dir",
    "default_data_dir",
    "store_from_env",
]

PREFIX = "patents/"
_TMP = ".patent-fetch-tmp-"


class NotPrivate(RuntimeError):
    """A stored document is readable by someone other than its owner."""


@runtime_checkable
class Store(Protocol):
    def has(self, key: str) -> bool: ...
    def get(self, key: str) -> bytes: ...
    def put(self, key: str, data: bytes, content_type: str = ...) -> None: ...
    def keys(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...
    def invalidate(self, key: str) -> None: ...
    def assert_private(self, key: str) -> None: ...


def _check(key: str) -> str:
    if not key.startswith(PREFIX) or ".." in key.split("/") or "\\" in key:
        raise ValueError(f"{key!r} is outside {PREFIX!r}. The patent library writes nowhere else.")
    return key


def _xdg(var: str, fallback: str) -> Path:
    base = os.environ.get(var)
    return Path(base) if base else Path.home() / fallback


def default_data_dir() -> Path:
    env = os.environ.get("PATENT_FETCH_DATA_DIR")
    return Path(env).expanduser() if env else _xdg("XDG_DATA_HOME", ".local/share") / "patent-fetch"


def default_cache_dir() -> Path:
    env = os.environ.get("PATENT_FETCH_CACHE")
    return Path(env).expanduser() if env else _xdg("XDG_CACHE_HOME", ".cache") / "patent-fetch"


def _mkdir_private(path: Path) -> None:
    """Create `path` and any missing parents, each 0700 (`Path.mkdir` applies `mode` to the leaf
    only, so missing parents would otherwise get the umask's default, typically 0755)."""
    missing = []
    p = path
    while not p.exists():
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        d.mkdir(mode=0o700, exist_ok=True)


def _write_private(path: Path, data: bytes) -> None:
    """Atomically write `data` to `path`, mode 0600, creating parent directories 0700."""
    _mkdir_private(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=_TMP, dir=path.parent)  # mkstemp creates the file 0600
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------------- memory


class MemoryStore:
    """For tests. Same interface, no disk, no network."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts = 0

    def has(self, key: str) -> bool:
        return _check(key) in self.objects

    def get(self, key: str) -> bytes:
        return self.objects[_check(key)]

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.objects[_check(key)] = bytes(data)
        self.puts += 1

    def keys(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(_check(prefix)))

    def delete(self, key: str) -> None:
        self.objects.pop(_check(key), None)

    def invalidate(self, key: str) -> None:
        """Nothing is cached in front of memory."""

    def assert_private(self, key: str) -> None:
        """Memory is private to the process."""


# ---------------------------------------------------------------------------------- local


class LocalStore:
    """A directory on disk. Keys are relative paths under `root`."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser() if root is not None else default_data_dir()
        _mkdir_private(self.root)

    def _path(self, key: str) -> Path:
        return self.root.joinpath(*_check(key).split("/"))

    def has(self, key: str) -> bool:
        return self._path(key).is_file()

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            raise KeyError(key) from None

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        _write_private(self._path(key), bytes(data))

    def keys(self, prefix: str) -> list[str]:
        _check(prefix)
        # The prefix need not end on a directory boundary ("patents/works/pmcid-"), so walk from
        # the deepest directory it names and filter on the full string.
        top = self.root.joinpath(*prefix.split("/")[:-1])
        if not top.is_dir():
            return []
        out = []
        for p in top.rglob("*"):
            if p.is_file() and not p.name.startswith(_TMP):
                key = p.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    out.append(key)
        return sorted(out)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def invalidate(self, key: str) -> None:
        """Nothing is cached in front of the disk."""

    def assert_private(self, key: str) -> None:
        """Refuse a file that anyone but its owner can read, or that sits under such a directory."""
        if os.name != "posix":  # pragma: no cover -- POSIX permission bits only
            return
        path = self._path(key)
        chain = [path, *[p for p in path.parents if p == self.root or self.root in p.parents]]
        for p in chain:
            mode = p.stat().st_mode
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise NotPrivate(
                    f"{p} is accessible to other users (mode {stat.filemode(mode)}). "
                    "The library must stay private: chmod go-rwx it."
                )


# ---------------------------------------------------------------------------------- S3


class S3Store:
    """Any S3-compatible bucket, with a local read-through cache.

    Reads go through `PATENT_FETCH_CACHE` (default `~/.cache/patent-fetch`); without it every read
    is a round trip and, on most providers, an egress charge.
    """

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        public_url: str | None = None,
        cache: Path | None = None,
        client: Any = None,
    ) -> None:
        self.bucket = bucket
        self.region = region
        self.endpoint_url = endpoint_url
        self.base_url = (public_url or self._virtual_host_url()).rstrip("/")
        self.cache = cache if cache is not None else default_cache_dir() / "s3" / bucket
        if client is None:
            try:
                import boto3  # noqa: PLC0415 -- optional dependency
                from botocore.config import Config  # noqa: PLC0415
            except ImportError as e:  # pragma: no cover -- depends on the environment
                raise ImportError("S3Store needs boto3: pip install 'patent-fetch[s3]'") from e
            client = boto3.client(
                "s3",
                region_name=region,
                endpoint_url=endpoint_url,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                config=Config(retries={"max_attempts": 5, "mode": "standard"}),
            )
        self._s3 = client

    @classmethod
    def from_env(cls) -> S3Store:
        """PATENT_FETCH_S3_BUCKET (required), _ENDPOINT_URL, _REGION, _ACCESS_KEY_ID,
        _SECRET_ACCESS_KEY, _PUBLIC_URL. Unset credentials fall back to boto3's own chain."""
        env = os.environ
        bucket = env.get("PATENT_FETCH_S3_BUCKET")
        if not bucket:
            raise RuntimeError(
                "PATENT_FETCH_STORE=s3 needs PATENT_FETCH_S3_BUCKET (and usually "
                "PATENT_FETCH_S3_ENDPOINT_URL, PATENT_FETCH_S3_REGION, "
                "PATENT_FETCH_S3_ACCESS_KEY_ID, PATENT_FETCH_S3_SECRET_ACCESS_KEY)."
            )
        return cls(
            bucket,
            endpoint_url=env.get("PATENT_FETCH_S3_ENDPOINT_URL") or None,
            region=env.get("PATENT_FETCH_S3_REGION") or None,
            access_key_id=env.get("PATENT_FETCH_S3_ACCESS_KEY_ID") or None,
            secret_access_key=env.get("PATENT_FETCH_S3_SECRET_ACCESS_KEY") or None,
            public_url=env.get("PATENT_FETCH_S3_PUBLIC_URL") or None,
        )

    def _virtual_host_url(self) -> str:
        """Where an anonymous reader would find an object: https://<bucket>.<endpoint host>."""
        if self.endpoint_url:
            u = urllib.parse.urlsplit(self.endpoint_url)
            return f"{u.scheme or 'https'}://{self.bucket}.{u.netloc or u.path}"
        region = f".{self.region}" if self.region and self.region != "us-east-1" else ""
        return f"https://{self.bucket}.s3{region}.amazonaws.com"

    def _local(self, key: str) -> Path:
        return self.cache.joinpath(*_check(key).split("/"))

    def has(self, key: str) -> bool:
        if self._local(key).is_file():
            return True
        try:
            self._s3.head_object(Bucket=self.bucket, Key=key)
        except Exception:  # noqa: BLE001 -- botocore raises ClientError(404); any failure is "no"
            return False
        return True

    def get(self, key: str) -> bytes:
        local = self._local(key)
        if local.is_file():
            return local.read_bytes()
        body = self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        _write_private(local, body)
        return body

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        # No ACL argument, ever: objects are private by default, and publishing what a provider
        # served is a decision this tool is not allowed to make.
        self._s3.put_object(
            Bucket=self.bucket, Key=_check(key), Body=data, ContentType=content_type
        )
        _write_private(self._local(key), bytes(data))

    def keys(self, prefix: str) -> list[str]:
        out: list[str] = []
        token = None
        while True:
            kw: dict[str, Any] = {"Bucket": self.bucket, "Prefix": _check(prefix), "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            r = self._s3.list_objects_v2(**kw)
            out += [o["Key"] for o in r.get("Contents", [])]
            if not r.get("IsTruncated"):
                return out
            token = r["NextContinuationToken"]

    def delete(self, key: str) -> None:
        """Nothing in the library calls this yet; it completes the interface."""
        self._s3.delete_object(Bucket=self.bucket, Key=_check(key))
        self._local(key).unlink(missing_ok=True)

    def invalidate(self, key: str) -> None:
        self._local(key).unlink(missing_ok=True)

    def assert_private(self, key: str) -> None:
        """An unsigned GET must NOT succeed. Asked as a stranger, not by reading the ACL back."""
        url = f"{self.base_url}/{urllib.parse.quote(_check(key))}"
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                status = r.status
        except urllib.error.HTTPError:
            return  # 403 / 404: not anonymously readable
        except OSError:
            return  # unreachable anonymously; nothing more a stranger could do
        if status in (200, 206):
            raise NotPrivate(
                f"{key} is anonymously readable at {url}. The library must stay private."
            )


# ---------------------------------------------------------------------------------- selection


def store_from_env() -> Store:
    """The store named by PATENT_FETCH_STORE: 'local' (default), 's3', or 'memory'."""
    kind = (os.environ.get("PATENT_FETCH_STORE") or "local").strip().lower()
    if kind == "local":
        return LocalStore()
    if kind == "s3":
        return S3Store.from_env()
    if kind == "memory":
        return MemoryStore()
    raise ValueError(f"PATENT_FETCH_STORE={kind!r}; expected 'local', 's3' or 'memory'")
