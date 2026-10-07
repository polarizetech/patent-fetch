"""Configuration from the environment, optionally seeded from `*.env` files.

A program that launches patent-fetch (a scheduler, an MCP gateway) has no shell to `source`
anything, so the CLI reads `KEY=value` (or `export KEY=value`) lines from every `*.env` file in
`PATENT_FETCH_ENV_DIR`, default `~/.config/patent-fetch/` (`$XDG_CONFIG_HOME/patent-fetch` when
set).

Only the variables patent-fetch itself uses are taken (`PATENT_FETCH_*` and the provider keys
below), so a file that also holds unrelated secrets does not leak them into the process. A
variable already set in the environment always wins. Keep these files out of any repository
(`chmod 600`).
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["PROVIDER_KEYS", "env_dir", "load_env"]

#: Non-`PATENT_FETCH_*` variables patent-fetch reads. Everything else in an env file is ignored.
PROVIDER_KEYS = (
    "EPO_OPS_KEY",
    "EPO_OPS_SECRET",
    "LENS_API_TOKEN",
    "SERPAPI_API_KEY",
)


def env_dir() -> Path:
    explicit = os.environ.get("PATENT_FETCH_ENV_DIR")
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "patent-fetch"


def _wanted(key: str) -> bool:
    return key.startswith("PATENT_FETCH_") or key in PROVIDER_KEYS


def load_env(directory: Path | None = None) -> list[str]:
    """Set known variables from `directory/*.env` without overriding the environment.

    Returns the names that were set, in the order they were read.
    """
    directory = directory if directory is not None else env_dir()
    if not directory.is_dir():
        return []
    loaded: list[str] = []
    for path in sorted(directory.glob("*.env")):
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            line = line.removeprefix("export ").strip()
            key, sep, value = line.partition("=")
            key = key.strip()
            if sep and _wanted(key) and key not in os.environ:
                os.environ[key] = value.strip().strip("'\"")
                loaded.append(key)
    return loaded
