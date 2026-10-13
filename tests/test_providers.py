"""Each adapter against a recorded (or, for the keyed three, documented) answer."""

from __future__ import annotations

import json

import pytest

from conftest import DeadHttp, FakeHttp, J, R, fixture, raw
from patent_fetch.ids import normalize
from patent_fetch.providers import (
    REGISTRY,
    EpoOps,
    EuropePMC,
    GooglePatents,
    Lens,
    Provider,
    ProviderBroken,
    ProviderUnavailable,
    SerpApi,
    build,
    describe,
)

# ------------------------------------------------------------------------------ registry


def test_defaults_and_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHttp()
    assert [p.name for p in build(http)] == ["epo-ops", "lens", "google-patents", "europepmc"]
    assert [p.name for p in build(http, purpose="search")] == ["europepmc", "epo-ops", "lens"]
    monkeypatch.setenv("PATENT_FETCH_SEARCH_PROVIDERS", "lens, serpapi")
    assert [p.name for p in build(http, purpose="search")] == ["lens", "serpapi"]


def test_a_typo_raises_instead_of_shrinking_the_search() -> None:
    with pytest.raises(ValueError, match=r"unknown provider.*known: europepmc"):
        build(FakeHttp(), names=["europepmc", "goggle"])
    with pytest.raises(ValueError, match="'google-patents' cannot search"):
        build(FakeHttp(), names=["google-patents"], purpose="search")
    with pytest.raises(ValueError, match="'serpapi' cannot lookup"):
        build(FakeHttp(), names=["serpapi"])
    with pytest.raises(ValueError, match="purpose"):
        build(FakeHttp(), purpose="download")


def test_describe_says_what_is_untested_and_what_is_paid(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = {d["name"]: d for d in describe(FakeHttp())}
    assert set(rows) == set(REGISTRY)
    assert [n for n, d in rows.items() if not d["verified"]] == ["lens", "serpapi"]
    assert [n for n, d in rows.items() if d["metered"]] == ["serpapi"]
    assert rows["epo-ops"]["available"] is False
    assert rows["epo-ops"]["why"] == "needs EPO_OPS_KEY, EPO_OPS_SECRET"
    assert rows["europepmc"]["available"] is True
    monkeypatch.setenv("LENS_API_TOKEN", "t")
    assert Lens(FakeHttp()).available() == (True, "")


def test_the_base_provider_does_neither() -> None:
    p = Provider(FakeHttp())
    with pytest.raises(NotImplementedError):
        p.search("x")
    with pytest.raises(NotImplementedError):
        p.lookup(normalize("US1234567"))


# ---------------------------------------------------------------------------- Europe PMC


def test_europepmc_search_reads_a_recorded_answer() -> None:
    http = FakeHttp({"europepmc/webservices/rest/search": J(fixture("europepmc_search.json"))})
    hits = EuropePMC(http).search('"cochlear implant"', limit=2)
    assert "query=SRC%3APAT%20AND%20%28%22cochlear%20implant%22%29" in http.log[0]
    assert "pageSize=2" in http.log[0]
    first = hits[0]
    assert (first.id, first.kind, first.year) == ("WO2012016009", "A1", 2012)
    assert first.title
    assert first.title.startswith("METHODS AND SYSTEMS FOR FITTING")
    assert first.applicants == ["ADVANCED BIONICS AG"]
    assert first.inventors[0] == "CHAPA FERNANDO"
    # Europe PMC's priorityDate repeats the filing date, so no priority date is taken from it.
    assert first.dates == {"filing": "2011-07-28"}
    assert (first.application, first.priorities) == ("WO2011US45674", ["US20100847099"])
    assert "A61N1/36" in first.classifications
    assert first.url == "https://europepmc.org/article/PAT/WO2012016009"


def test_europepmc_lookup_uses_the_docdb_spelling_and_has_no_claims() -> None:
    http = FakeHttp({"/article/PAT/WO2012016009": J(fixture("europepmc_article.json"))})
    doc = EuropePMC(http).lookup(normalize("WO 2012/016009 A1"))
    assert doc is not None
    assert doc.patent.id == "WO2012016009"
    assert not doc.has_text
    assert EuropePMC(FakeHttp()).lookup(normalize("US7861317")) is None  # 404: not known
    http = FakeHttp({"/article/PAT/US2012029596?": J({"hitCount": 0})})
    assert EuropePMC(http).lookup(normalize("US20120029596")) is None
    assert http.calls == 1


def test_a_failed_query_is_never_an_empty_answer() -> None:
    with pytest.raises(ProviderUnavailable, match="HTTP 503"):
        EuropePMC(FakeHttp({"search": R(status=503)})).search("x")
    with pytest.raises(ProviderBroken, match=r"rejected our request \(HTTP 400\): bad query"):
        EuropePMC(FakeHttp({"search": R("bad query", 400)})).search("x")
    with pytest.raises(ProviderUnavailable, match="rate-limited") as e:
        EuropePMC(FakeHttp({"search": R(status=429)})).search("x")
    assert e.value.pause_s == 300.0
    with pytest.raises(ProviderUnavailable, match="unreachable: timed out") as e:
        EuropePMC(DeadHttp()).search("x")
    assert "api_key" not in str(e.value)  # a URL can carry a key: it is never repeated


def test_a_record_without_a_number_is_dropped() -> None:
    rows = {"resultList": {"result": [{"id": "not a number", "title": "x"}, {"title": "y"}]}}
    assert EuropePMC(FakeHttp({"search": J(rows)})).search("x") == []


# ------------------------------------------------------------------------ Google Patents


def test_google_patents_reads_a_recorded_page() -> None:
    http = FakeHttp({"patents.google.com/patent/US7861317B2/en": R(raw("google_patent.html"))})
    doc = GooglePatents(http).lookup(normalize("US 7,861,317 B2"))
    assert doc is not None
    p = doc.patent
    assert (p.id, p.kind, p.title, p.year) == ("US7861317", "B2", "Nose cover", 2011)
    assert p.inventors == ["Robert Beliveau"]
    assert p.applicants == ["Individual"]
    assert p.dates == {
        "priority": "2004-10-08",
        "filing": "2004-10-08",
        "publication": "2011-01-04",
    }
    assert p.status == "Expired - Fee Related"
    assert "A41D13/11" in p.classifications
    assert "A62B" not in p.classifications  # ancestors of a symbol are not symbols
    assert p.cites[:2] == ["US3835848A", "US4274402A"]
    assert p.cited_by[0] == "US20100042139A1"
    assert p.extra["pdf"].endswith("US7861317.pdf")
    assert (p.application, p.priorities) == ("US10/962,874", [])
    assert p.abstract
    assert p.abstract.startswith("A nose cover or nose protector")
    assert doc.description
    assert "CROSS-REFERENCE TO RELATED APPLICATION(S)" in doc.description
    assert "<" not in doc.description
    assert doc.claims
    assert doc.claims.startswith("I claim:\n1. A flexible nose cover")
    assert "Claims (9)" not in doc.claims


def test_google_patents_requests_only_document_pages() -> None:
    http = FakeHttp()
    assert GooglePatents(http).lookup(normalize("US99999999999")) is None
    assert http.log == ["https://patents.google.com/patent/US99999999999/en"]
    assert GooglePatents.can_search is False


def test_google_patents_failures_are_told_apart() -> None:
    pid = normalize("US7861317")
    with pytest.raises(ProviderUnavailable, match="HTTP 503"):
        GooglePatents(FakeHttp({"/patent/": R(status=503)})).lookup(pid)
    with pytest.raises(ProviderBroken, match="no publication number on the page"):
        GooglePatents(FakeHttp({"/patent/": R("<html>a different page</html>")})).lookup(pid)
    bare = '<dd itemprop="publicationNumber">US7861317B2</dd>'
    doc = GooglePatents(FakeHttp({"/patent/": R(bare)})).lookup(pid)
    assert doc is not None
    assert (doc.patent.kind, doc.has_text, doc.patent.abstract) == ("B2", False, None)


# ------------------------------------------------------------------------------- EPO OPS

TOKEN = J({"access_token": "tok-1", "expires_in": "1199"})


def ops(routes: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> tuple[EpoOps, FakeHttp]:
    monkeypatch.setenv("EPO_OPS_KEY", "key")
    monkeypatch.setenv("EPO_OPS_SECRET", "secret")
    http = FakeHttp({"auth/accesstoken": TOKEN, **routes})  # type: ignore[dict-item]
    return EpoOps(http), http


def test_epo_ops_search_reads_a_recorded_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    p, http = ops({"search/biblio": R(raw("epo_ops_search.xml"))}, monkeypatch)
    hits = p.search("cochlear implant electrode array", limit=5)
    assert http.posts == [
        ("https://ops.epo.org/3.2/auth/accesstoken", b"grant_type=client_credentials")
    ]
    assert http.headers[0]["Authorization"] == "Basic a2V5OnNlY3JldA=="
    assert http.headers[1] == {"Authorization": "Bearer tok-1"}
    assert "q=ta%20all%20%22cochlear%20implant%20electrode%20array%22&Range=1-5" in http.log[1]
    assert [h.id for h in hits] == ["US20250256099", "WO2025137595"]
    first, second = hits
    assert (first.kind, first.year) == ("A1", 2025)
    assert first.title == "METHOD AND DEVICE FOR PROVIDING STIMULATION TO OBSTRUCTED COCHLEA"
    assert first.applicants == ["UNIV NEW YORK [US]"]
    assert first.inventors == ["LANDSBERGER DAVID M [US]", "ROLAND JR J THOMAS [US]"]
    assert first.dates == {
        "priority": "2024-02-08",
        "filing": "2025-02-06",
        "publication": "2025-08-14",
    }
    assert first.classifications == ["A61N1/0541", "A61N1/36039"]
    assert first.extra == {"family": "96661441"}
    assert first.application == "US202519047123"
    assert first.priorities == ["US202463551349P", "US202519047123"]  # itself among them
    assert first.abstract
    assert first.abstract.startswith("A cochlear implant includes first and second ground")
    # Its four citations name applications (provisionals), which are not publications.
    assert first.cites == []
    assert second.cites[:2] == ["US20230320751", "US20180263705"]
    assert first.url == "https://worldwide.espacenet.com/patent/search?q=pn%3DUS2025256099"
    p.search("pa=bionics and cpc=A61N", limit=200)  # CQL is sent as written; one token is reused
    assert "q=pa%3Dbionics%20and%20cpc%3DA61N&Range=1-100" in http.log[2]
    assert len(http.posts) == 1


def test_epo_ops_answers_an_empty_search_with_404(monkeypatch: pytest.MonkeyPatch) -> None:
    fault = "<fault><code>SERVER.EntityNotFound</code><message>No results found</message></fault>"
    p, _ = ops({"search/biblio": R(fault, 404)}, monkeypatch)
    assert p.search("nothing matches this") == []


def test_epo_ops_lookup_joins_biblio_description_and_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p, _http = ops(
        {
            "epodoc/EP4483941/biblio": R(raw("epo_ops_biblio.xml")),
            "epodoc/EP4483941/description": R(raw("epo_ops_description.xml")),
            "epodoc/EP4483941/claims": R(raw("epo_ops_claims.xml")),
        },
        monkeypatch,
    )
    doc = p.lookup(normalize("EP4483941A1"))
    assert doc is not None
    assert (doc.patent.id, doc.patent.kind) == ("EP4483941", "A1")
    assert doc.patent.applicants == ["ADVANCED BIONICS LLC [US]"]
    assert doc.patent.classifications == ["A61N1/0541", "A61N1/36038", "A61N1/375"]
    assert doc.description
    assert doc.description.startswith("BACKGROUND\n1. Field\n[0001] The present disclosure")
    assert doc.claims
    assert doc.claims.startswith("1. An apparatus, comprising:\na cochlear implant (100)")
    assert p.lookup(normalize("EP9999999")) is None


def test_epo_ops_without_full_text_is_a_record_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refusal = "<fault><code>CLIENT.InvalidCountryCode</code></fault>"  # what OPS says of US text
    p, _http = ops(
        {
            "epodoc/US2025256099/biblio": R(raw("epo_ops_search.xml")),
            "epodoc/US2025256099/": R(refusal, 404),
        },
        monkeypatch,
    )
    doc = p.lookup(normalize("US 2025/0256099 A1"))  # asked in the EPO's ten-digit spelling
    assert doc is not None
    assert not doc.has_text
    assert doc.patent.id == "US20250256099"  # of the two records listed, the kind asked for


def test_epo_ops_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    p, _ = ops({"auth/accesstoken": R(status=401)}, monkeypatch)
    with pytest.raises(ProviderBroken, match="refused EPO_OPS_KEY"):
        p.search("x")
    p, _ = ops({"search/biblio": R(status=403)}, monkeypatch)
    with pytest.raises(ProviderUnavailable, match="fair use or quota"):
        p.search("x")
    p, _ = ops({"search/biblio": R("<html>not xml")}, monkeypatch)
    with pytest.raises(ProviderBroken, match="did not answer in XML"):
        p.search("x")
    p, _ = ops({"search/biblio": R(b"<empty/>")}, monkeypatch)
    assert p.search("x") == []
    p, _ = ops({"epodoc/EP1234567/biblio": R(b"<empty/>")}, monkeypatch)
    assert p.lookup(normalize("EP1234567")) is None
    p, _ = ops(
        {
            "epodoc/EP4483941/biblio": R(raw("epo_ops_biblio.xml")),
            "epodoc/EP4483941/claims": R(b"<empty/>"),
        },
        monkeypatch,
    )
    doc = p.lookup(normalize("EP4483941"))
    assert doc is not None
    assert doc.claims is None


def test_epo_ops_renews_a_token_the_service_calls_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([R("invalid_access_token", 400), R(raw("epo_ops_search.xml"))])
    p, http = ops({"search/biblio": lambda _url: next(answers)}, monkeypatch)
    assert len(p.search("x")) == 2
    assert len(http.posts) == 2


# ---------------------------------------------------------------------------------- Lens


def test_lens_search_and_lookup_from_the_documented_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LENS_API_TOKEN", "lens-token")
    http = FakeHttp({"api.lens.org/patent/search": J(fixture("lens_search.json"))})
    hits = Lens(http).search("cochlear implant", limit=500)
    body = json.loads(http.posts[0][1])
    assert body["query"]["query_string"]["query"] == "cochlear implant"
    assert body["size"] == 100
    assert "description" not in body["include"]
    assert http.headers[0]["Authorization"] == "Bearer lens-token"
    (hit,) = hits
    assert (hit.id, hit.kind, hit.year, hit.status) == ("US20120029596", "A1", 2012, "ACTIVE")
    assert hit.title == "Methods and systems for fitting a bilateral cochlear implant"
    assert hit.applicants == ["ADVANCED BIONICS AG"]
    assert hit.inventors == ["CHAPA FERNANDO", "CALLE GUILLERMO A"]
    assert hit.dates == {
        "priority": "2010-07-30",
        "filing": "2011-07-28",
        "publication": "2012-02-02",
        "grant": "2015-08-11",
    }
    assert hit.classifications == ["A61N1/36038", "H04R25/70"]
    assert hit.cites == ["US7861317"]
    assert hit.url == "https://www.lens.org/lens/patent/031-156-664-106-437"
    assert (hit.application, hit.priorities) == ("US201113192539", ["US36937010"])

    doc = Lens(http).lookup(normalize("US20120029596A1"))
    terms = json.loads(http.posts[1][1])["query"]["bool"]["must"]
    assert terms == [{"term": {"jurisdiction": "US"}}, {"term": {"doc_number": "20120029596"}}]
    assert doc is not None
    assert doc.description
    assert doc.description.startswith("BACKGROUND")
    assert (
        doc.claims
        == "1. A method comprising: using a first sound processor.\n2. The method of claim 1."
    )


def test_lens_unknown_number_and_refused_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LENS_API_TOKEN", "t")
    assert (
        Lens(FakeHttp({"lens.org": J({"total": 0, "data": []})})).lookup(normalize("EP1234567"))
        is None
    )
    unusable = {"data": [{"jurisdiction": None, "doc_number": None}]}
    assert Lens(FakeHttp({"lens.org": J(unusable)})).lookup(normalize("EP1234567")) is None
    with pytest.raises(ProviderBroken, match="HTTP 401"):
        Lens(FakeHttp({"lens.org": R("Missing/Incorrect Authorization Header", 401)})).search("x")
    with pytest.raises(ProviderUnavailable, match="unreachable"):
        Lens(DeadHttp()).search("x")


# ------------------------------------------------------------------------------- SerpApi


def test_serpapi_search_from_the_documented_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERPAPI_API_KEY", "paid-key")
    http = FakeHttp({"serpapi.com/search": J(fixture("serpapi_search.json"))})
    (hit,) = SerpApi(http).search("solar tracking", limit=3)
    assert "engine=google_patents&q=solar+tracking&num=10&api_key=paid-key" in http.log[0]
    assert (hit.id, hit.kind, hit.year) == ("EP2264377", "A2", 2010)
    assert hit.applicants == ["Green Plus Co. Ltd."]
    assert hit.inventors == ["Young Hwan Park"]
    assert hit.dates["priority"] == "2009-06-03"
    assert hit.url == "https://patents.google.com/patent/EP2264377A2/en"
    assert hit.extra["snippet"].startswith("a plurality")
    assert "paid-key" not in json.dumps(hit.to_json())


def test_serpapi_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERPAPI_API_KEY", "paid-key")

    def ask(answer: object, status: int = 200) -> list[object]:
        return list(SerpApi(FakeHttp({"serpapi.com": J(answer, status)})).search("x"))

    assert ask({"error": "Google Patents hasn't returned any results for this query."}) == []
    with pytest.raises(ProviderUnavailable, match="run out of searches") as e:
        ask({"error": "Your account has run out of searches."})
    assert e.value.pause_s == float("inf")
    with pytest.raises(ProviderBroken, match="Unsupported"):
        ask({"error": "Unsupported `num` parameter."})
    with pytest.raises(ProviderBroken, match="refused SERPAPI_API_KEY"):
        ask({"error": "Invalid API key."}, 401)
    with pytest.raises(ProviderUnavailable) as e:
        SerpApi(DeadHttp()).search("x")
    assert "paid-key" not in str(e.value)
    assert "api_key" not in str(e.value)
