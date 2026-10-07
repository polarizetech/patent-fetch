# Wiring a project into patent-fetch

## Depend on it

From git, with the `s3` extra if the library lives in a bucket (boto3):

```bash
uv add "patent-fetch[s3] @ git+https://github.com/polarizetech/patent-fetch@<tag-or-commit>"
pip install "patent-fetch[s3] @ git+https://github.com/polarizetech/patent-fetch@<tag-or-commit>"
```

**Across repositories, pin a tag or a full commit sha** — never the moving `main`, and never a
path import of a checkout. No tag has been cut yet (2026-10-07), so pin the commit; `uv.lock`
records it either way. The core needs only the standard library.

## Setup, once per machine

Everything is environment variables ([README § Configure](README.md#configure) has the table).
`patent-fetch` also reads `KEY=value` lines from every `*.env` file in `~/.config/patent-fetch/`
(or `$PATENT_FETCH_ENV_DIR`), taking only `PATENT_FETCH_*` and the four provider keys and never
overriding a variable already set. Keep those files mode 600 and out of every repository.

**Get an EPO OPS key first.** Without any key you can read a patent by number but can only search
an archive of life-science patents that ends in 2012. Registration at <https://developers.epo.org>
is free: create an app, and copy its consumer key and secret into, for example,
`~/.config/patent-fetch/keys.env`:

```bash
EPO_OPS_KEY=...
EPO_OPS_SECRET=...
LENS_API_TOKEN=...        # optional: The Lens; free for trial and non-commercial use
SERPAPI_API_KEY=...       # optional and paid: Google Patents' search, asked only when named
PATENT_FETCH_EMAIL=...    # optional: added to the User-Agent; never sent unless set
```

`patent-fetch providers` prints what each provider needs, whether it is ready, and whether its
adapter has been run against the live service yet.

A library shared between machines lives in an S3-compatible bucket. For example
`~/.config/patent-fetch/s3.env`:

```bash
PATENT_FETCH_STORE=s3
PATENT_FETCH_S3_BUCKET=<bucket>
PATENT_FETCH_S3_ENDPOINT_URL=https://<endpoint>   # omit for AWS S3
PATENT_FETCH_S3_REGION=<region>
PATENT_FETCH_S3_ACCESS_KEY_ID=...
PATENT_FETCH_S3_SECRET_ACCESS_KEY=...
```

Only these names are taken: credentials kept under other variable names (a file written for
another tool) are ignored, so copy the values across. Everything is written under the prefix
`patents/`, so **the bucket may be one that already holds another library** under its own prefix
(paper-fetch writes under `papers/`); a key scoped to the bucket serves both. Without the two key
variables boto3's own credential chain applies. The privacy check makes an unsigned GET against
`https://<bucket>.<endpoint host>`; set `PATENT_FETCH_S3_PUBLIC_URL` if objects are served from
elsewhere. Unset, `PATENT_FETCH_STORE` is `local` (`~/.local/share/patent-fetch`), which shares
nothing with other machines. `patent-fetch status` shows which store is in use.

## Import it

```python
from patent_fetch import Library
from patent_fetch.config import load_env

load_env()  # Library.default() does not read *.env files; the CLI does
lib = Library.default()
```

## The calls you will use

| call | network | returns |
|---|---|---|
| `lib.search(query, limit=10, also=(), countries=None, since=None, collection=None, refresh=False)` | yes, cached 7 days | `{"query", "hits", "filtered_out", "providers", "broken"}` |
| `lib.fetch(number, force=False, collection=None)` | only if not held | the catalogue row, with `from`: `library` or `retrieved` |
| `lib.text(number)`, `lib.claims(number)` | no | stored text; raises `NotFound` if not held or held without it |
| `lib.record(number)` | no | `{"patent": {...}, "provenance": {...}}`: every field, every route tried |
| `lib.lookup(number)` | no | the catalogue row, or `None` |
| `lib.search_library(query="", full_text=False)` | no | rows of held patents containing every word |
| `lib.create_collection(name, description="", queries=())` | no | the collection |
| `lib.collect(name, numbers)`, `lib.uncollect(name, numbers)` | no | the collection |
| `lib.sweep(name, limit=25, also=())` | yes, always fresh | `{"collection", "queries", "new", "complete", "broken"}` |
| `lib.status()`, `lib.paused()` | no | what is held, which providers are on or being left alone |

A hit is `{"id", "kind", "title", "abstract", "year", "applicants", "inventors", "dates",
"classifications", "status", "url", "cites", "cited_by", "extra", "found_by", "in_library",
"full_text_in_library"}`, plus `new` and `member` when the search was made for a collection.

**Read `providers` before reading `hits`.** Each entry is `{"provider", "status", "hits"}` with
`status` one of `ok`, `cache`, `skipped` (no key), `unavailable` (an outside problem: try later)
or `error` (a defect: also in `broken`, and logged). A search where every provider that could
have answered was `skipped` or `unavailable` has told you nothing about the patents.

## Choosing providers

By name, for one process or one call:

```bash
PATENT_FETCH_SEARCH_PROVIDERS=epo-ops,lens patent-fetch search "..."
patent-fetch search "..." --providers epo-ops
patent-fetch search "..." --also serpapi          # add the paid one, this once
```

```python
lib.search("...", providers=["epo-ops"])
lib.search("...", also=["serpapi"])
```

An unknown name raises. `epo-ops` takes its own query language when you use it: a query that
contains a CQL index (`pa=`, `in=`, `cpc=`, `pd within`, `ti all`, ...) is sent as written, so
`patent-fetch search 'pa="advanced bionics" and cpc=A61N1/36' --providers epo-ops` searches by
applicant and class. A plain query is sent as "every word, in title or abstract". The other
providers would read that CQL as plain words, so name `epo-ops` alone when you write it.

## Keeping an area current

```python
lib.create_collection(
    "hearing",
    description="Cochlear implant electrodes",
    queries=["cochlear implant electrode array", "auditory nerve stimulation"],
)
report = lib.sweep("hearing")
for hit in report["new"]:  # not shown to this collection before
    print(hit["id"], hit["year"], hit["title"], "<-", hit["query"])
if not report["complete"]:  # a provider did not answer: new patents may be missing
    ...  # run it again later rather than trust it
lib.collect("hearing", ["EP2598092"])  # the ones that turn out to matter
```

Run the sweep on whatever schedule suits; offices publish weekly. `patent-fetch sweep <name>` is
the same thing from a shell or a scheduler.

## From an MCP gateway

This repository ships no MCP server. A research gateway that already mounts other tools can add
patents in either of two ways, and which one is the gateway's decision:

- **Call the library.** Every method above returns plain JSON-shaped data and raises `NotFound` or
  `ValueError` for a bad request, so a tool is a few lines: call, catch, wrap in the gateway's own
  envelope. Map `ProviderUnavailable`-style outcomes from the `providers` list, never from an
  empty `hits`: "unavailable" must not reach a model as "zero results".
- **Call the command.** `patent-fetch --json search "..."` prints the same result on stdout and
  provider defects on stderr; the exit code is non-zero when a fetch failed.

If a tool is offered to a model, its description should carry the caveat the CLI prints: a search
shows what these providers matched, no hits is not evidence that no patent exists, and nothing
returned is a legal opinion. Retrieved patent text is data, never instructions.

## Cost discipline

- `europepmc`, `google-patents`: free. Google Patents pages are requested one per lookup, two
  seconds apart; do not loop over thousands of numbers through it. For bulk, use EPO OPS or
  Google's BigQuery dataset.
- `epo-ops`: free up to 4 GB a week. A lookup is three requests (biblio, description, claims).
- `lens`: per your plan.
- `serpapi`: **paid per search**, so it is asked only when named. A sweep with `also=["serpapi"]`
  spends one search per query.
- A repeat search within 7 days is served from the store and costs nothing. A held patent is
  never retrieved again.

## What it refuses

- **To search Google Patents directly.** Its `robots.txt` disallows the search; the adapter
  requests document pages only.
- **To guess a number.** A US application number or a number without a country code raises.
- **To call an outage "no results".** A provider that did not answer is named as such.
- **To publish.** Nothing sets an ACL; stored text is checked for anonymous access after writing.

## What it does not do

- It does not say whether a patent is in force, valid or infringed, or whether you are free to
  operate. It finds and stores documents; reading the claims is for a patent attorney.
- It does not search by meaning. "Hearing prosthesis" does not find "cochlear implant"; write the
  queries a searcher would, and use classification codes where a provider takes them.
- It does not join patent families, translate, or download PDFs or drawings (a Google Patents
  record carries the PDF's URL in `extra`).
- It runs no language model and ships no MCP server.
