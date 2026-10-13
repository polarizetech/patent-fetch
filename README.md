# patent-fetch

Find existing patents, and keep a deduplicated, private library of the ones you read. Give it some
words, or a publication number; it asks a set of swappable patent services, merges what they
answer, and says by name which of them answered and which did not. A patent retrieved once is
stored with its description, its claims and a provenance record, and is never retrieved again.

It ships as a Python library and a command line (`patent-fetch`). It runs no language model:
searching, merging and bookkeeping are deterministic.

```console
$ patent-fetch search "cochlear implant electrode array" -n 5
  europepmc        ok             5 hits
  epo-ops          skipped        0 hits  needs EPO_OPS_KEY, EPO_OPS_SECRET
  lens             skipped        0 hits  needs LENS_API_TOKEN

5 distinct patents
    US20110264183    2011 PRE-CURVED COCHLEAR IMPLANT ELECTRODE ARRAY  (GIBSON PETER) [europepmc]
    US7272449        2007 Cochlear implant electrode array  (DADD FYSH) [europepmc]
    ...
$ patent-fetch fetch US7861317B2
✓ US7861317B2  [retrieved]  2011  Nose cover
    source=google-patents  full text  status=Expired - Fee Related
$ patent-fetch fetch "US 7,861,317"                    # second time: no network at all
✓ US7861317B2  [library]  2011  Nose cover
$ patent-fetch claims US7861317
I claim:
1. A flexible nose cover configured to attach to eyewear and shield the nose from frostbite ...
```

## What it is for, and what it is not

Two jobs: a one-off search ("is there a patent on this?"), and keeping track of the patents in an
area you work in.

- **A collection** is a named area: the queries that define it, the patents you decided belong to
  it, and every patent its searches have ever shown. **A sweep** runs the queries again and
  reports only what was not shown before, so an area can be re-checked without re-reading it.

  ```console
  $ patent-fetch collection hearing --create --query "cochlear implant electrode array"
  $ patent-fetch sweep hearing                 # everything is new the first time
  $ patent-fetch collect hearing US7272449     # this one matters to us
  $ patent-fetch sweep hearing                 # next month: only what has appeared since
  ```

- **One entry per invention.** An invention is usually published several times: an application,
  its grant, the same filing at other offices. Publications that share a priority are folded into
  one hit, with the others listed under it (`also published as ...`), so one patent family does
  not fill the results. `--all-publications` lists each separately.
- **It is not a legal opinion.** A search shows what the enabled providers matched for these words
  on this day. No hits is not evidence that no patent exists, and nothing here says whether you
  are free to operate, whether a patent is valid, or whether it is in force: those turn on the
  claims as a patent attorney reads them. A legal status, an assignee or a priority date is passed
  on in the provider's own words, and several providers say of their own data that it is not a
  legal conclusion.
- **Loud, per-provider failure, of two kinds.** A provider that is down, rate-limited or out of
  allowance is reported by name as `unavailable`, the other providers answer, and it is left alone
  for a while. A provider that fails because of a **defect** (it rejected our request, or its
  answer changed shape) is reported as `error`, named in the search's `broken` list and logged
  with its traceback (`patent-fetch problems`). Neither is ever folded into "no results".
- **Private.** A patent is public, but a copy came from a service with terms of its own, and a
  record of what you searched for says what you are working on. Files are `0600` in directories
  `0700`; S3 objects never get an ACL and are checked with an anonymous request.

## Providers

| name | search | lookup | account | what it gives |
|---|---|---|---|---|
| `europepmc` | yes | yes | none | Europe PMC's patent records: **life-science patents only, and only up to 2012** (4.2 million, from the EPO's data; nothing later has been added). Abstract and classes, no claims. |
| `google-patents` | | yes | none | One patent's page on Google Patents, by number: description and claims for 100+ offices. **Lookup only**; see below. |
| `epo-ops` | yes | yes | free registration | EPO Open Patent Services: bibliographic data for 100+ offices, full text for EP, WO and some others. 4 GB a week free. |
| `lens` | yes | yes | token | The Lens: 150M+ records with claims. Free for trial and non-commercial use; paid otherwise. |
| `serpapi` | yes | | **paid** | Google Patents' search, through SerpApi. Opt-in per search (`--also serpapi`). |

`europepmc`, `google-patents` and `epo-ops` have been run against their services. **`lens` and
`serpapi` have not**: they are written from each service's documentation and marked
`NOT YET RUN LIVE` by `patent-fetch providers` until someone with an account runs them.

**With no account at all you can read any patent by its number, but you cannot search for recent
ones**: the only keyless search is an archive of life-science patents that ends in 2012. To search
what exists today, in any field, you need at least one keyed provider. **`epo-ops` is the one to
get first**: free, official and worldwide. Until then a sweep can never report anything new.

**Google Patents has no API.** Its `robots.txt` allows `/patent/` pages and disallows everything
else, including its search. So `google-patents` requests one document page per lookup, two seconds
apart, and never searches. Searching Google Patents from a program means a paid intermediary
(`serpapi`) or Google's own [BigQuery patents dataset](https://console.cloud.google.com/marketplace/product/google_patents_public_datasets/google-patents-public-data).

### Services looked at and not built

| service | why not (as of October 2026) |
|---|---|
| USPTO PatentsView | Its API was shut down on 20 March 2026; the data moved to the USPTO Open Data Portal as bulk downloads. |
| USPTO Open Data Portal | Free key, but it needs a USPTO.gov account verified through ID.me, and its search is over application file wrappers rather than patent text. The next provider to add for US prosecution history. |
| Google BigQuery `patents-public-data` | Full-text SQL over 90M+ publications, 1 TB of queries a month free. Needs a Google Cloud project; a good fit for a bulk landscape, not for a search box. |
| WIPO PATENTSCOPE | Its web service is a paid subscription. |
| EPO linked open data | A keyless SPARQL endpoint over EP publications answered. A candidate for keyless EP search; not designed yet. |
| SureChEMBL | Serves a patent's contents by number without a key (chemistry-annotated patents); its search endpoint answered 500. A candidate for a second keyless lookup. |
| PQAI | Natural-language prior-art search; its API now needs a token (401 without one) and its current terms were not checked. |
| Patent MCP servers | Commercial connectors exist (Patlytics, Solve Intelligence, PatSnap) and need their own subscriptions; community servers wrap the same USPTO, EPO and BigQuery sources listed here. |

## Install

Python 3.11+. Not on PyPI; install from this repository:

```bash
pip install "patent-fetch @ git+https://github.com/polarizetech/patent-fetch"
pip install "patent-fetch[s3] @ git+https://github.com/polarizetech/patent-fetch"   # plus the S3-compatible store (boto3)
# or, from a checkout:
uv sync && uv run patent-fetch --help
```

The core depends only on the standard library. `boto3` is needed only for the S3 store.

## Configure

Everything is configured with environment variables; none is required. `patent-fetch` also reads
`KEY=value` lines from `*.env` files in `~/.config/patent-fetch/` (or `$PATENT_FETCH_ENV_DIR`),
taking only the variables below and never overriding the environment. Keep those files out of
repositories (`chmod 600`).

| Variable | Purpose |
|---|---|
| `EPO_OPS_KEY`, `EPO_OPS_SECRET` | EPO Open Patent Services consumer key and secret ([register](https://developers.epo.org), free). |
| `LENS_API_TOKEN` | The Lens patent API token ([request](https://www.lens.org/lens/user/subscriptions)). |
| `SERPAPI_API_KEY` | SerpApi key. Paid per search, so asked only when named. |
| `PATENT_FETCH_EMAIL` | Optional contact address, added to the User-Agent. Never sent unless set. |
| `PATENT_FETCH_PROVIDERS` | Comma-separated lookup order. Default `epo-ops,lens,google-patents,europepmc`. |
| `PATENT_FETCH_SEARCH_PROVIDERS` | Comma-separated search set. Default `europepmc,epo-ops,lens`. |
| `PATENT_FETCH_STORE` | `local` (default), `s3` or `memory`. |
| `PATENT_FETCH_DATA_DIR` | Local store directory. Default `~/.local/share/patent-fetch` (`$XDG_DATA_HOME/patent-fetch`). |
| `PATENT_FETCH_CACHE` | Read-through cache for the S3 store. Default `~/.cache/patent-fetch`. |
| `PATENT_FETCH_S3_BUCKET` | S3 store: bucket (required when `PATENT_FETCH_STORE=s3`). |
| `PATENT_FETCH_S3_ENDPOINT_URL`, `PATENT_FETCH_S3_REGION` | S3 store: endpoint for non-AWS services (DigitalOcean Spaces, MinIO, R2, ...) and region. |
| `PATENT_FETCH_S3_ACCESS_KEY_ID`, `PATENT_FETCH_S3_SECRET_ACCESS_KEY` | S3 store credentials; if unset, boto3's usual credential chain applies. |
| `PATENT_FETCH_S3_PUBLIC_URL` | S3 store: the anonymous base URL used to prove objects are *not* public. Default `https://<bucket>.<endpoint host>`. |
| `PATENT_FETCH_PROBLEMS` | Where provider defects are logged, one JSON object per line. Default `provider-problems.jsonl` in the local store's directory. Never in the store. |

Everything is stored under the prefix `patents/`, so one bucket can hold this library beside
another that keeps to its own prefix.

## Numbers

A patent is named by its publication number, in any common spelling: `US7861317B2`,
`US 7,861,317`, `EP 2264377 A2`, `WO 2012/016009`, `US 2012/0029596 A1`, or a Google Patents URL.
It is stored under country code plus number without the kind code (`US7861317`). A US
*application* number (`US 10/962,874`) is refused, as is a number with no country code.

## Python

```python
from patent_fetch import Library
from patent_fetch.config import load_env

load_env()  # keys from ~/.config/patent-fetch/*.env, if you keep them there
lib = Library.default()

res = lib.search("cochlear implant electrode array", since=2015, countries=["US", "EP", "WO"])
for report in res["providers"]:  # read these before reading "no hits"
    print(report["provider"], report["status"])
for hit in res["hits"]:
    print(hit["id"], hit["year"], hit["title"], hit["found_by"])

rec = lib.fetch("US7861317B2")  # the library first, then the providers
print(lib.claims(rec["id"]))
print(lib.record(rec["id"])["provenance"])  # which provider, when, every route tried
```

More in [INTEGRATION.md](INTEGRATION.md).

## Development

```bash
uv sync
uv run pytest                     # offline: no credentials, no network
uv run ruff check . && uv run ruff format --check .
uv run pyright
```

The suite never opens a socket: HTTP is a fake that serves `tests/fixtures/`. [CLAUDE.md](CLAUDE.md)
records what was measured against each live service and how to add a provider.

## Licence

MIT. See [LICENSE](LICENSE).
