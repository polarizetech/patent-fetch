# CLAUDE.md — patent-fetch

**Find existing patents through FIVE swappable providers, and keep one private, deduplicated
library of the ones that were read, in a local directory or any S3-compatible bucket, checked
before anything is retrieved.** A patent retrieved once is never retrieved again, by any project,
on any machine that shares the store.

Run (from a checkout; an installed copy drops the `uv run`):

```
uv run patent-fetch search "cochlear implant electrode array"      # every search provider, merged
uv run patent-fetch fetch US7861317B2                              # record, description, claims
uv run patent-fetch claims US7861317
uv run patent-fetch providers                                      # what is on, what each needs; no network
uv run patent-fetch collection hearing --create --query "cochlear implant electrode array"
uv run patent-fetch sweep hearing                                  # what those queries had not shown before
```

Configuration is `PATENT_FETCH_*` plus four provider keys, read from the environment and from
`~/.config/patent-fetch/*.env` — the table is in [README § Configure](README.md#configure). The
store is `PATENT_FETCH_STORE=local` (default, `~/.local/share/patent-fetch`), `s3` or `memory`.

Tests: `uv run pytest` — offline, no credentials, no network (HTTP is a fake serving
`tests/fixtures/`). There are no live tests. Lint and types: [README § Development](README.md#development).
CI requires 90% coverage. Wiring a project in → [`INTEGRATION.md`](INTEGRATION.md).

## What this repository is, and what stays out of it

- **A sibling of `polarizetech/paper-fetch`**, built to the same conventions: `uv`, `hatchling`,
  `ruff`, `pyright`, an offline `pytest` suite over a fake HTTP client, the same provider contract
  and the same two kinds of failure. `http.py`, `store.py`, `config.py` and `problems.py` began as
  copies of paper-fetch's and should stay recognisably the same; a fix to one usually belongs in
  both.
- **It is written for anyone's research, not ours.** Nothing here names a project, a bucket, a
  host or a person: `tests/test_guarantees.py` fails if the package does. The store is "any
  S3-compatible bucket"; which one is the operator's `*.env` file, outside every repository.
- **No MCP server, on purpose (2026-10-07).** The research pipeline in
  `polarizetech/retrieval-augmented-generation` uses this repository as a *companion*
  (`research_pipeline/companions.py` there, merged 2026-10-07): when its planner judges a question
  is about an invention, it runs `patent-fetch --json search "<words>"` once per query and prints
  the hits under "Existing patents on this". Nothing is fetched and nothing becomes evidence.
  **So the JSON that `--json search` prints is a contract with another repository**: `hits` (with
  `id`, `title`, `year`, `applicants`, `inventors`, `classifications`, `status`, `url`,
  `also_published_as`) and `providers` (with `provider`, `status`). Add fields freely; renaming or removing one means a change there
  too. A server here later would be a thin wrapper over the same `Library` methods. See
  INTEGRATION.md § "From an MCP gateway".
- **No language model runs inside it.** Searching, merging, collections and sweeps are string
  matching and bookkeeping, so behaviour is testable and repeatable.
- **Visibility: public**, decided 2026-10-07 by the operator, like paper-fetch and
  retrieval-augmented-generation (dataset-fetch is private). Public is the irreversible direction.

## ⭐ Not a legal opinion — and the code may not pretend otherwise

A search returns what the enabled providers matched for these words on this day. **An empty
result is not evidence that no patent exists.** Nothing computes whether a patent is in force,
valid or infringed, and `tests/test_guarantees.py` asserts no such name appears in the code. A
status, an assignee or a priority date is passed on in the provider's own words under the
provider's name; Google's own page says of its assignee and priority date that they are not legal
conclusions. The CLI prints the caveat under every search and sweep. Do not remove it, and do not
add a field that summarises "risk".

## ⭐ Providers — named, ordered, swappable

`src/patent_fetch/providers/` holds one adapter per service behind one contract: a provider may
**search** (query → `Patent`s) and/or **lookup** (one publication number → a `Document`: the
record plus description and claims where the service has them). It never stores or merges
anything — that is the library's job, which is what makes providers interchangeable.

```bash
PATENT_FETCH_PROVIDERS=epo-ops,lens,google-patents,europepmc   # lookup order
PATENT_FETCH_SEARCH_PROVIDERS=europepmc,epo-ops,lens           # search set
```

Those are the defaults. An **unknown name raises** with the known list. A lookup-only provider
named in the search set (or the reverse) also raises.

| provider | search | lookup | account | run live? | notes (measured 2026-10-07 unless said) |
|---|---|---|---|---|---|
| `europepmc` | ✓ | record, no claims | — | **yes** | `SRC:PAT`: life-science patents from the EPO's data. **An archive that ends in 2012**: 373,750 records for 2011, 126,514 for 2012, none for any later year, 4,229,296 in all. Good for prior art, useless for recent filings. |
| `google-patents` | — never | description + claims | — | **yes** | One `/patent/<number>/en` page per lookup, 2 s apart. US7861317B2, WO2012016009A1 and EP2264377A2 each returned description and claims in English; an unknown number answered 404; a number without a kind code is served the latest publication. |
| `epo-ops` | ✓ CQL | biblio + description + claims (EP, WO, some others) | free registration | **yes, 2026-10-13** | Anonymous search answered 403 "violation of Fair Use policy". With a key: `ta all "cochlear implant electrode array"` matched 270 publications, newest first (2025, 2026); EP4483941 came back with description and claims; US documents have no full text (404 `CLIENT.InvalidCountryCode`); an empty search is 404 `SERVER.EntityNotFound`; the token lasts 1,199 s. |
| `lens` | ✓ | record + claims + description | token | **no** | Anonymous request answered 401 "Missing/Incorrect Authorization Header". Everything else is from the documentation. |
| `serpapi` | ✓ **opt-in** | — | paid | **no** | Google Patents' search through SerpApi's `google_patents` engine. Written from its documentation page. |

### `verified = False` means exactly that

`lens` and `serpapi` were written without an account. Their fixtures (`lens_search.json`,
`serpapi_search.json`) are **built from the published documentation, not cut from a live answer**,
so their tests prove the adapter reads the shape the documentation describes and nothing more.
`Provider.verified` is False for each, which `patent-fetch providers` prints as
`NOT YET RUN LIVE` and a search report carries as a `note`.

**The first time one is run with a real key:** replace its fixture with a (trimmed) real answer,
fix what breaks, set `verified = True`, and add a dated line under "Honest status" saying what the
live service did differently. The points most likely to be wrong: Lens's `query_string` field
names (`title`, `abstract`, `claim`), its `include` list, and the `doc_number` term used for
lookup; SerpApi's error strings.

`epo-ops` went through exactly that on 2026-10-13: its three fixtures (`epo_ops_search.xml`,
`epo_ops_biblio.xml`, `epo_ops_claims.xml`, plus a description cut to its first paragraphs) are
real answers, and the one defect the live run showed is fixed and tested (below).

### Google Patents: the page, never the search

Google Patents has no API. Its `robots.txt` (read 2026-10-07):

```
User-agent: *
Disallow: /*
Allow: /$
Allow: /advanced$
Allow: /patent/
Allow: /sitemap/
```

So a document page may be requested and the search endpoint (`/xhr/query`) may not.
`GooglePatents` builds one URL and nothing else, and `tests/test_guarantees.py` asserts the host
appears exactly once in the package and `xhr/query` never. Searching Google Patents from a program
is `serpapi` (paid) or the BigQuery dataset.

The page is HTML and the adapter is a scrape: `<meta name="DC.title">`, `<dd itemprop="...">`,
`<time itemprop="..." datetime="...">`, and `<section itemprop="abstract|description|claims">`.
It is the first adapter that will break. A page that answers 200 without a publication number
raises `ProviderBroken` (logged, reported) instead of returning an empty record. When it breaks:
save the new page, cut it down the way `tests/fixtures/google_patent.html` was (first rows of each
table, first paragraphs of the description), and fix `GooglePatents.parse` against it.

### `serpapi` is opt-in because every search is paid for

It is `metered`: not in `DEFAULT_SEARCH`, asked only through `also=["serpapi"]` (`--also serpapi`)
or by being named in the set. **SerpApi takes the key as a URL parameter**, so no URL is ever kept:
a transport error is reported without it (`Provider._brief`), `problems.record` is given the query
and never the URL, and a cached search holds only the hits. `tests/test_library.py` reads back
everything a search stored and asserts the key is not in it.

## ⭐ When a provider fails: wait out an outside problem, flag a defect

Same rule as paper-fetch. A search asks every provider and merges what answers, so one failing
never stops the rest.

- **An outside problem is `ProviderUnavailable`**: unreachable, a 5xx, a 403, a 429, a spent
  allowance. The report says `unavailable` and why, and `Library` then leaves that provider alone:
  60 s by default, 300 s after a 429 or an OPS fair-use refusal, the rest of the process after
  SerpApi's allowance is spent. While paused its report is `unavailable` with `paused: true`. A
  cached answer is still served; `refresh=True` asks anyway.
- **A defect is `ProviderBroken`, or any exception an adapter did not expect**: the service
  rejected the request or the key, or answered in a shape the adapter does not read. It is never
  paused and never quiet: the report says `error` with `broken: true`, the result carries a
  top-level `broken` list, the CLI prints `! BROKEN: ...` on stderr, and `problems.py` appends the
  traceback to `provider-problems.jsonl` (`patent-fetch problems --trace`).
- **A query helper never returns `[]` for a failure.** `Provider._answered` turns any non-200 into
  one kind or the other. It is for QUERY endpoints; a lookup by number may take a 404 as "not
  known". OPS is the documented exception: it answers an empty search with 404.

## Identity: country + number, no kind code

`ids.py`. `US7861317B2`, `US 7,861,317` and a Google Patents URL are one key, `US7861317`; the
kind code is kept on the record. One spelling difference matters: a US application publication is
`US20120029596` (year + seven digits) at the USPTO and Google, and `US2012029596` (year + six) in
the EPO's DOCDB, which OPS and Europe PMC use. The key is the eleven-digit form and
`PatentId.docdb` gives the other. A US application number (`US 10/962,874`) and a bare number are
refused with a reason. Not handled: old two-digit-year WO numbers (`WO99/12345`), which Google
spells `WO1999012345`; they are stored as written.

## Families: one hit per invention

One invention is published several times: an application, its grant, the same filing at other
offices. Before 2026-10-13 each was its own hit, and a pipeline run showed the cost: four of the
five patents it listed were the US, EP, WO and CA publications of one Cochlear patent.
`search()` now folds them (`library._collapse`): one hit is kept and the others are named in its
`also_published_as` (`id`, `kind`, `year`, `url`, `found_by`, `in_library`). The result says how
many were folded in `collapsed`. `families=False` / `--all-publications` turns it off.

- **Joined on a shared priority.** `Patent.priorities` (the applications a publication claims
  priority from) and `Patent.application` (its own) are the keys; two hits that share one are one
  family. Europe PMC and EPO OPS spell these the same way (`US19950414656`), measured on the
  Cochlear family above, so hits join **across providers** with no extra request. Punctuation and
  a trailing kind letter (`US202463551349P`) are ignored. `google-patents` and `serpapi` give no
  priority numbers, and Lens's spelling is unverified: their hits fail to join rather than join
  wrongly.
- **This is the extended family, not the EPO's "simple family".** OPS's own `family-id` puts
  US5545219 (one priority) in a different family from its EP, WO and CA siblings (the same
  priority plus one more), which is exactly the case that needed folding. The cost of joining on
  any shared priority is that a continuation-in-part or a divisional, whose claims differ, folds
  into its parent. Nothing is discarded: every publication is named with its own link.
- **Which one is kept:** one held with its text, else one held, else US, then EP, then WO, then
  any other office; among those the latest, which is the grant where there is one.
- **Filters apply to publications before grouping**, so `countries=["EP"]` keeps the EP
  publication of a family whose US one was filtered out.
- **In a collection, a family is new only if none of its publications was seen before**, and all
  of them are recorded as seen. The grant of an application already seen is not reported as new.
  If grants matter to you as events, that is a different feature (legal status), not this one.

## Collections and sweeps

`patents/collections/<name>.json`: `queries`, `members` (patents someone decided belong), `seen`
(every patent any search for the collection returned, with the date first seen). `sweep()` runs
each query with `refresh=True` and returns the hits not in `seen`; that is the whole mechanism for
"what has appeared in our area". Removing a member leaves it seen. `complete` is False when any
provider was `unavailable` or `error` during the sweep, because a provider that did not answer may
be hiding new patents: run it again rather than trust it.

A sweep is only as current as its providers. With no keyed provider enabled the only search
provider is `europepmc`, which ends in 2012, so a sweep can never report anything new. **Set
`EPO_OPS_KEY` and `EPO_OPS_SECRET` before relying on sweeps.**

## Index-first, and why a stale index cannot cause a second retrieval

`fetch()` looks in the catalogue (`patents/index.jsonl`), then at the authoritative per-patent
record (`patents/docs/<ID>/record.json`), and only then asks a provider. **The catalogue is
derived**: `tests/test_library.py::test_a_held_patent_is_never_retrieved_again` deletes it and
asserts no further lookup. `patent-fetch rebuild-index` recreates it whole.

A patent held without description or claims is kept, with a `retry_after` 30 days out, and asked
for again after that: providers add full text over time.

Several processes may share one store. The catalogue is always read from the store, never from
the S3 read-through cache, and a change re-reads it and writes one row onto the latest copy
(`test_two_libraries_on_one_store_do_not_drop_each_others_rows`). Two saves in the same instant
can still race; the per-patent records repair any row lost (`rebuild-index`). Within a process one
lock covers the catalogue, collections and the pause clock, and is never held across a provider
call.

Searches are cached in the store for **7 days** (`SEARCH_TTL_DAYS`): offices publish weekly. The
cache key carries `SEARCH_CACHE_VERSION`; raise it when `Patent` gains a field a search result
depends on. This was learned the hard way on 2026-10-13: the first search after families were
added was served from a cache written before `priorities` existed, and folded nothing.

## Private, though patents are public

A patent is a public document, but the copy came from a service whose terms restrict
redistribution, and a store of searches and collections says what a project is working on. No
code path sets an ACL (asserted), every write goes through a prefix guard that refuses anything
outside `patents/`, and each stored full text is checked the way a stranger would ask (files
`0600` in directories `0700`; on S3 an unsigned GET that must not succeed). The prefix means one
bucket can hold this library beside paper-fetch's `papers/`.

## Services surveyed and not built (October 2026)

| service | account | observed | verdict |
|---|---|---|---|
| USPTO PatentsView (`search.patentsview.org`) | — | Host does not answer. Shut down 2026-03-20; data moved to the Open Data Portal as bulk downloads. | **gone** |
| USPTO Open Data Portal (`api.uspto.gov`) | free key, needs a USPTO.gov account verified by ID.me | 401 "Unauthorized" without a key. Search is over application file wrappers (prosecution history), not patent text. | add next, for US prosecution data |
| EPO linked open data (`data.epo.org/linked-data/query`) | — | A SPARQL query answered 200 with JSON results, no key. EP publications only; text search over SPARQL would be slow. | a keyless candidate; not designed yet |
| SureChEMBL (`surechembl.org/api`) | — | `/api/document/WO-2012016009-A1/contents` answered 200 with the document, no key; its search endpoint answered 500. Chemistry-annotated patents. | a keyless lookup candidate |
| Google BigQuery `patents-public-data` | Google Cloud project | not probed. Full-text SQL over 90M+ publications; 1 TB of queries a month free. | for a bulk landscape, not a search box |
| PQAI (`api.projectpq.ai`) | token | 401 "Unauthorized" without one. Natural-language prior-art search. Current terms unchecked. | maybe |
| WIPO PATENTSCOPE | paid | not probed; its web service is a paid subscription. | skip |
| Google Patents search (`/xhr/query`) | — | disallowed by `robots.txt`. | **never** |
| MCP connectors (Patlytics, Solve Intelligence, PatSnap) | each its own subscription | listed in the connector registry, none connected or tried. Community servers wrap USPTO ODP, EPO OPS and BigQuery, the sources above. | the operator's choice; nothing to build here |

## How to add a provider

1. Probe it anonymously first and write down what it answered (status, body) with the date.
2. Subclass `Provider` in `providers/`; set `name`, `can_search` / `can_lookup`, `needs`,
   `min_interval_s`, `coverage`, `terms`. Use `self._get` / `self._post` (pacing, 429, transport
   errors) and `self._answered` for query endpoints. Return `Patent` / `Document`; build ids with
   `number_or_none` so one bad row is dropped rather than raised.
3. Register it in `providers/__init__.py`. Add it to a default set only if it needs no payment.
4. Cut a fixture from a real answer and test the adapter against it in `tests/test_providers.py`.
   If you cannot run it live, set `verified = False` and say so in the table above.
5. Add its key to `config.PROVIDER_KEYS`, `tests/conftest.py::_isolated` and README § Configure.

## Honest status

- **Run live (2026-10-07):** `europepmc` search and lookup; `google-patents` lookup of three
  publications from three offices, stored with description and claims, and re-fetched with no
  network; a full CLI pass against the real services with the local store.
- **Run live (2026-10-13):** `epo-ops`, with a registered key: search (plain words and CQL),
  lookup of an EP document with description and claims, of a US document without, an unknown
  number, and an empty search. Everything written from the documentation held. **What the live
  answer showed that the documentation did not:** a cited reference can name an *application*
  (`<patcit dnum-type="application number">`, typically a US provisional), which the adapter was
  listing in `cites` as if it were a publication; those are now left out. OPS also returns US
  application publications with eleven digits in a search and accepts ten in a lookup; `ids.py`
  already read both. Cost, from the `x-registeredquotaperweek-used` header: about 42 kB for a
  search of two records, 68 kB for one description, against 4 GB a week.
- **Run through the research pipeline (2026-10-13):** see the gateway repository's run log; the
  companion there calls `patent-fetch --json search` and prints the hits.
- **Not run live:** `lens`, `serpapi` (no account), and the S3 store **in this
  repository** (it is paper-fetch's `S3Store` with the prefix changed, and its tests use a fake
  client).
- **Search filters are applied after merging.** `countries` and `since` drop hits from what the
  providers returned; they are not passed to the providers. With `limit` per provider that can
  leave few hits: the result says how many were `filtered_out`. A hit with no known year passes
  `since`.
- **Ranking is "found by more providers first", then provider order.** There is no relevance score
  across providers and no semantic search: "hearing prosthesis" does not find "cochlear implant".
- **`fulltext.txt` is the description and claims as the provider gave them**, in English where the
  provider offered it; Google's text for non-English filings is a machine translation, and the
  record does not yet say which.
- **Families are joined in search results only** (above). The library still stores each
  publication under its own number: fetching the EP and the US publication of one invention
  stores two entries, and `search_library` lists both.
- **Europe PMC's `priorityDate` is wrong and is not used** (found 2026-10-13): it repeats the
  application date on every priority. Until then this adapter reported it as the priority date,
  so a record cached from Europe PMC before that date may carry a filing date under `priority`.
  Its priority *numbers* are right and are what families are joined on.
- **The catalogue is one JSON-lines object re-read and rewritten on each change**: fine for
  hundreds of patents, slow for tens of thousands.
