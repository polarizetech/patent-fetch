from __future__ import annotations

import json

import pytest

from conftest import (
    CLAIMS,
    DESCRIPTION,
    FakeHttp,
    FakeProvider,
    J,
    fixture,
    full_doc,
    make_library,
    patent,
)
from patent_fetch import Library, LocalStore, MemoryStore, NotFound
from patent_fetch.library import INDEX, _now
from patent_fetch.providers import Document, Patent, ProviderBroken, ProviderUnavailable

EP = "EP2598092"

# --------------------------------------------------------------------------------- fetch


def test_a_held_patent_is_never_retrieved_again() -> None:
    provider = FakeProvider(doc=full_doc())
    lib = make_library(providers=[provider])
    first = lib.fetch("EP 2598092 B1")
    assert (first["from"], first["id"], first["has_full_text"]) == ("retrieved", EP, True)
    assert lib.fetch(EP)["from"] == "library"
    assert lib.fetch("https://patents.google.com/patent/EP2598092B1/en")["from"] == "library"
    lib.store.delete(INDEX)  # the catalogue is derived: losing it costs a repair, not a lookup
    fresh = Library(lib.store, providers=[provider], search_providers=[])
    assert fresh.fetch(EP)["from"] == "library"
    assert EP in fresh.index()
    assert provider.lookups == 1
    assert lib.fetch(EP, force=True)["from"] == "retrieved"
    assert provider.lookups == 2


def test_what_is_stored_and_how_it_is_read() -> None:
    lib = make_library()
    lib.fetch(EP)
    text = lib.text(EP)
    assert text.startswith("Fitting a bilateral cochlear implant\n\nDESCRIPTION\n\n" + DESCRIPTION)
    assert text.endswith("CLAIMS\n\n" + CLAIMS + "\n")
    assert lib.claims("EP2598092B1") == CLAIMS + "\n"
    stored = lib.record(EP)
    assert stored["patent"]["kind"] == "B1"
    prov = stored["provenance"]
    assert (prov["source"], prov["asked_for"], prov["has_full_text"]) == ("fake", EP, True)
    assert prov["routes"] == [{"provider": "fake", "outcome": "full-text"}]
    assert len(prov["text_sha256"]) == len(prov["claims_sha256"]) == 64
    assert sorted(lib.store.keys("patents/docs/")) == [
        f"patents/docs/{EP}/claims.txt",
        f"patents/docs/{EP}/fulltext.txt",
        f"patents/docs/{EP}/record.json",
    ]


def test_the_first_provider_with_claims_wins_and_earlier_records_fill_gaps() -> None:
    meta = FakeProvider(
        "meta",
        doc=Document(
            patent(EP, "meta", title=None, status="ACTIVE", dates={"grant": "2018-12-05"})
        ),
    )
    text = FakeProvider(
        "text", doc=Document(patent(EP, "text", dates={"filing": "2011-07-28"}), None, CLAIMS)
    )
    never = FakeProvider("never", doc=full_doc())
    lib = make_library(
        providers=[
            FakeProvider("nokey", needs=("EPO_OPS_KEY",)),
            FakeProvider("down", raise_=ProviderUnavailable("down for maintenance")),
            FakeProvider("buggy", raise_=KeyError("exchange-document")),
            FakeProvider("unknown"),
            meta,
            text,
            never,
        ]
    )
    row = lib.fetch(EP)
    assert (row["source"], row["status"], row["title"]) == (
        "text",
        "ACTIVE",
        "Fitting a bilateral cochlear implant",
    )
    assert row["dates"] == {"grant": "2018-12-05", "filing": "2011-07-28"}
    routes = lib.record(EP)["provenance"]["routes"]
    assert [(r["provider"], r["outcome"]) for r in routes] == [
        ("nokey", "skipped"),
        ("down", "unavailable"),
        ("buggy", "error"),
        ("unknown", "not-known"),
        ("meta", "record-only"),
        ("text", "full-text"),
    ]
    assert never.lookups == 0
    assert "down" in lib.paused()


def test_a_record_without_claims_is_kept_and_asked_for_again_later() -> None:
    provider = FakeProvider(doc=Document(patent(EP)))
    lib = make_library(providers=[provider])
    row = lib.fetch(EP)
    assert row["has_full_text"] is False
    assert row["retry_after"] > _now()
    assert lib.fetch(EP)["from"] == "library"
    assert provider.lookups == 1
    with pytest.raises(NotFound, match="held without its claims"):
        lib.claims(EP)
    with pytest.raises(NotFound, match="held without its full text"):
        lib.text(EP)
    # Once the date has passed, the providers are asked again, and now one has the text.
    stored = lib.record(EP)
    stored["provenance"]["retry_after"] = "2020-01-01T00:00:00Z"
    lib.store.put(f"patents/docs/{EP}/record.json", json.dumps(stored).encode())
    lib.rebuild_index()
    provider.doc = full_doc()
    again = lib.fetch(EP)
    assert (again["from"], again["has_full_text"]) == ("retrieved", True)
    assert "retry_after" not in again


def test_nothing_found_says_what_was_tried_and_keeps_what_is_held() -> None:
    provider = FakeProvider()
    lib = make_library(providers=[provider, FakeProvider("nokey", needs=("LENS_API_TOKEN",))])
    with pytest.raises(NotFound, match=r"fake: not-known; nokey: skipped \(needs LENS_API_TOKEN\)"):
        lib.fetch("US99999999999")
    with pytest.raises(NotFound, match="no providers enabled"):
        make_library(providers=[]).fetch(EP)
    with pytest.raises(NotFound, match="not in the library"):
        lib.record(EP)
    provider.doc = full_doc()
    lib.fetch(EP)
    provider.doc = None
    assert lib.fetch(EP, force=True)["has_full_text"] is True  # a miss does not erase a hit


def test_a_paused_provider_is_not_asked_to_look_up() -> None:
    down = FakeProvider("down", raise_=ProviderUnavailable("429", pause_s=300))
    lib = make_library(providers=[down, FakeProvider(doc=full_doc())])
    lib.fetch(EP)
    lib.fetch("EP2598092", force=True)
    assert down.lookups == 1
    route = lib.record(EP)["provenance"]["routes"][0]
    assert route["outcome"] == "unavailable"
    assert route["detail"].startswith("not asked for")


def test_the_local_store_keeps_documents_private(tmp_path: object) -> None:
    lib = make_library(store=LocalStore(tmp_path))  # type: ignore[arg-type]
    lib.fetch(EP)
    path = lib.store.root / "patents" / "docs" / EP / "fulltext.txt"  # type: ignore[attr-defined]
    assert path.stat().st_mode & 0o777 == 0o600


def test_library_search_and_status() -> None:
    lib = make_library()
    lib.fetch(EP)
    assert [r["id"] for r in lib.search_library("bilateral COCHLEAR")] == [EP]
    assert lib.search_library("hair cells") == []
    assert [r["id"] for r in lib.search_library("hair cells", full_text=True)] == [EP]
    assert len(lib.search_library()) == 1
    status = lib.status()
    assert (status["patents"], status["with_full_text"], status["store"]) == (1, 1, "MemoryStore")
    assert status["lookup_providers"] == ["fake"]
    assert status["paused"] == {}


def test_two_libraries_on_one_store_do_not_drop_each_others_rows() -> None:
    store = MemoryStore()
    a = make_library(store=store, providers=[FakeProvider(doc=full_doc("EP1000001"))])
    b = make_library(store=store, providers=[FakeProvider(doc=full_doc("EP1000002"))])
    a.index()
    b.index()  # both have read an empty catalogue
    a.fetch("EP1000001")
    b.fetch("EP1000002")
    assert sorted(Library(store, providers=[], search_providers=[]).index()) == [
        "EP1000001",
        "EP1000002",
    ]
    assert a.lookup("EP1000002") is not None  # found through the record, then catalogued


def test_default_builds_providers_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATENT_FETCH_STORE", "memory")
    lib = Library.default()
    assert [p.name for p in lib.providers] == ["epo-ops", "lens", "google-patents", "europepmc"]
    assert [p.name for p in lib.search_providers] == ["europepmc", "epo-ops", "lens"]


# -------------------------------------------------------------------------------- search


def test_search_merges_hits_and_reports_every_provider() -> None:
    a = FakeProvider(
        "a", hits=[patent("EP1000001", "a", abstract=None), patent("US7000001", "a", year=2001)]
    )
    b = FakeProvider("b", hits=[patent("EP1000001", "b", abstract="From b", title="B's title")])
    lib = make_library(
        search_providers=[
            a,
            b,
            FakeProvider("nokey", needs=("EPO_OPS_KEY",)),
            FakeProvider("down", raise_=ProviderUnavailable("HTTP 503")),
            FakeProvider("buggy", raise_=ProviderBroken("answer changed shape")),
        ]
    )
    res = lib.search("cochlear implant")
    assert [(r["provider"], r["status"], r["hits"]) for r in res["providers"]] == [
        ("a", "ok", 2),
        ("b", "ok", 1),
        ("nokey", "skipped", 0),
        ("down", "unavailable", 0),
        ("buggy", "error", 0),
    ]
    assert [b["provider"] for b in res["broken"]] == ["buggy"]
    assert "answer changed shape" in res["broken"][0]["error"]
    first, second = res["hits"]
    assert (first["id"], first["found_by"]) == ("EP1000001", ["a", "b"])  # found twice: first
    assert (first["title"], first["abstract"]) == ("Fitting a bilateral cochlear implant", "From b")
    assert "provider" not in first
    assert (second["id"], second["in_library"]) == ("US7000001", False)
    assert res["filtered_out"] == 0


def test_search_filters_and_says_how_many_it_dropped() -> None:
    hits = [
        patent("EP1000001", year=2018),
        patent("US7000001", year=2001),
        patent("WO2012016009", year=None),
    ]
    lib = make_library(search_providers=[FakeProvider(hits=hits)])
    res = lib.search("x", since=2015)
    assert [h["id"] for h in res["hits"]] == ["EP1000001", "WO2012016009"]  # unknown year is kept
    assert res["filtered_out"] == 1
    assert [h["id"] for h in lib.search("x", countries=["us", "WO"])["hits"]] == [
        "US7000001",
        "WO2012016009",
    ]
    with pytest.raises(ValueError, match="empty query"):
        lib.search("  ")


def test_a_repeat_search_is_served_from_the_store_for_a_week() -> None:
    provider = FakeProvider(hits=[patent()])
    lib = make_library(search_providers=[provider])
    assert lib.search("x")["providers"][0]["status"] == "ok"
    again = lib.search("x")
    assert (again["providers"][0]["status"], again["hits"][0]["id"]) == ("cache", EP)
    lib.search("x", limit=20)  # a different question
    lib.search("x", refresh=True)
    assert len(provider.searches) == 3
    (key,) = list(lib.store.keys("patents/searches/fake/"))[:1]
    cached = json.loads(lib.store.get(key))
    cached["at"] = "2020-01-01T00:00:00Z"
    for k in lib.store.keys("patents/searches/fake/"):
        lib.store.put(k, json.dumps(cached).encode())
    assert lib.search("x")["providers"][0]["status"] == "ok"  # too old: asked again


def test_a_search_cached_under_an_older_record_shape_is_asked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider(hits=[patent()])
    lib = make_library(search_providers=[provider])
    lib.search("x")
    monkeypatch.setattr("patent_fetch.library.SEARCH_CACHE_VERSION", 99)
    assert lib.search("x")["providers"][0]["status"] == "ok"
    assert len(provider.searches) == 2


def test_an_unavailable_provider_is_left_alone_then_asked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    down = FakeProvider("down", raise_=ProviderUnavailable("HTTP 503"))
    lib = make_library(search_providers=[down])
    lib.search("x")
    second = lib.search("y")["providers"][0]
    assert (second["status"], second["paused"]) == ("unavailable", True)
    assert second["detail"].startswith("not asked for 60 s, after: HTTP 503")
    assert len(down.searches) == 1
    lib.search("z", refresh=True)  # refresh asks anyway
    assert len(down.searches) == 2
    lib._pause("down", -1, "over")
    assert lib.paused() == {}
    lib._pause("down", float("inf"), "allowance spent")
    assert lib.paused() == {"down": "not asked for this process, after: allowance spent"}


def test_a_metered_provider_is_asked_only_when_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERPAPI_API_KEY", "k")
    http = FakeHttp({"serpapi.com/search": J(fixture("serpapi_search.json"))})
    lib = Library(
        MemoryStore(), http=http, providers=[], search_providers=[FakeProvider(hits=[patent()])]
    )
    assert [r["provider"] for r in lib.search("x")["providers"]] == ["fake"]
    assert http.calls == 0
    res = lib.search("y", also=["serpapi", "fake"])
    assert [(r["provider"], r["status"]) for r in res["providers"]] == [
        ("fake", "ok"),
        ("serpapi", "ok"),
    ]
    assert res["providers"][1]["note"] == "adapter not yet verified against the live service"
    lib.search("z", also=["serpapi"])
    assert len(lib._opted) == 1  # built once, so its pacing and any session are reused
    # The key travels in the URL, so nothing kept from the search may contain it.
    kept = b"".join(lib.store.get(k) for k in lib.store.keys("patents/"))
    assert b"EP2264377" in kept
    assert b"api_key" not in kept
    named = Library(MemoryStore(), http=http).search("q", providers=["serpapi"])
    assert [r["provider"] for r in named["providers"]] == ["serpapi"]


# ------------------------------------------------------------------------------ families


def family() -> list[Patent]:
    """One invention published four times, as two services spell it, and one unrelated patent."""
    return [
        patent(
            "WO9631087",
            year=1996,
            application="WO1996AU00179",
            priorities=["US19950414656", "US19960616299"],
        ),
        patent(
            "EP0818123",
            year=1998,
            application="EP19960907216",
            priorities=["US19950414656", "US19960616299"],
        ),
        patent("US5545219", year=1996, application="US19950414656", priorities=["US19950414656"]),
        patent("CA2216703", year=1996, priorities=["US 1995/0414656", "US19960616299P"]),
        patent("WO0112115", year=2001, application="WO2000CA00942", priorities=["US19990148809"]),
    ]


def test_publications_of_one_invention_are_one_hit() -> None:
    lib = make_library(search_providers=[FakeProvider(hits=family())])
    res = lib.search("modiolus hugging electrode")
    assert [h["id"] for h in res["hits"]] == ["US5545219", "WO0112115"]
    assert (res["collapsed"], res["filtered_out"]) == (3, 0)
    first, second = res["hits"]
    # Joined through any shared priority, read past punctuation and a trailing kind letter.
    assert [m["id"] for m in first["also_published_as"]] == ["WO9631087", "EP0818123", "CA2216703"]
    assert first["also_published_as"][0] == {
        "id": "WO9631087",
        "kind": None,
        "year": 1996,
        "url": None,
        "found_by": ["fake"],
        "in_library": False,
    }
    assert second["also_published_as"] == []
    every = lib.search("modiolus hugging electrode", families=False)
    assert (len(every["hits"]), every["collapsed"]) == (5, 0)
    assert "also_published_as" not in every["hits"][0]


def test_the_publication_kept_is_one_already_held_then_by_office_then_the_latest() -> None:
    hits = family()[:3]
    lib = make_library(search_providers=[FakeProvider(hits=hits)])
    assert lib.search("x")["hits"][0]["id"] == "US5545219"  # US before EP before WO
    two_us = [*hits, patent("US6000000", year=1999, priorities=["US19950414656"])]
    lib = make_library(search_providers=[FakeProvider(hits=two_us)])
    assert lib.search("x")["hits"][0]["id"] == "US6000000"  # the later one: the grant
    lib = make_library(
        search_providers=[FakeProvider(hits=hits)],
        providers=[FakeProvider(doc=full_doc("WO9631087"))],
    )
    lib.fetch("WO9631087")
    kept = lib.search("x")["hits"][0]
    assert (kept["id"], kept["full_text_in_library"]) == ("WO9631087", True)  # the one we can read


def test_a_filter_applies_to_publications_before_they_are_grouped() -> None:
    lib = make_library(search_providers=[FakeProvider(hits=family())])
    res = lib.search("x", countries=["EP", "WO"])
    assert [h["id"] for h in res["hits"]] == ["EP0818123", "WO0112115"]
    assert [m["id"] for m in res["hits"][0]["also_published_as"]] == ["WO9631087"]
    assert (res["filtered_out"], res["collapsed"]) == (2, 1)


def test_hits_with_nothing_to_join_on_stay_apart() -> None:
    hits = [patent("EP1000001"), patent("EP1000002"), patent("EP1000003", priorities=["12"])]
    lib = make_library(search_providers=[FakeProvider(hits=hits)])
    assert lib.search("x")["collapsed"] == 0


def test_a_new_publication_of_a_seen_invention_is_not_new_to_a_collection() -> None:
    provider = FakeProvider(hits=family()[:1])  # only the WO application so far
    lib = make_library(search_providers=[provider])
    lib.create_collection("hearing", queries=["modiolus hugging electrode"])
    assert [h["id"] for h in lib.sweep("hearing")["new"]] == ["WO9631087"]
    lib.collect("hearing", ["WO9631087"])
    provider.hits = family()  # later the US grant appears, and an unrelated patent
    second = lib.sweep("hearing")
    assert [h["id"] for h in second["new"]] == ["WO0112115"]
    us = lib.search("modiolus hugging electrode", collection="hearing")["hits"][0]
    assert (us["id"], us["new"], us["member"]) == ("US5545219", False, True)
    assert lib.collection("hearing")["seen"] == 5  # every publication is recorded


# --------------------------------------------------------------------------- collections


def test_a_collection_is_queries_members_and_everything_seen() -> None:
    hits = [patent("EP1000001"), patent("US7000001")]
    provider = FakeProvider(hits=hits)
    lib = make_library(
        search_providers=[provider], providers=[FakeProvider(doc=full_doc("EP1000001"))]
    )
    c = lib.create_collection("hearing", description="Cochlear work", queries=["cochlear implant"])
    assert (c["queries"], c["members"], c["seen"], c["swept"]) == (
        ["cochlear implant"],
        [],
        0,
        None,
    )
    c = lib.create_collection("hearing", queries=["cochlear implant", "auditory prosthesis"])
    assert c["queries"] == ["cochlear implant", "auditory prosthesis"]
    assert c["description"] == "Cochlear work"

    first = lib.sweep("hearing")
    assert [h["id"] for h in first["new"]] == ["EP1000001", "US7000001"]  # everything, once
    assert first["new"][0]["query"] == "cochlear implant"
    assert [(q["hits"], q["new"]) for q in first["queries"]] == [(2, 2), (2, 0)]
    assert first["complete"] is True

    provider.hits = [*hits, patent("WO2026000001", year=2026)]
    second = lib.sweep("hearing")
    assert [h["id"] for h in second["new"]] == ["WO2026000001"]  # only what appeared since
    assert len(provider.searches) == 4  # a sweep asks fresh, not from the week's cache

    lib.fetch("EP 1000001", collection="hearing")
    c = lib.collect("hearing", ["US 7,000,001"], via="reading")
    assert [(m["id"], m["in_library"], m["via"]) for m in c["members"]] == [
        ("EP1000001", True, "fetch"),
        ("US7000001", False, "reading"),
    ]
    assert c["members"][0]["full_text_in_library"] is True
    hit = lib.search("cochlear implant", collection="hearing")["hits"][0]
    assert (hit["id"], hit["new"], hit["member"], hit["in_library"]) == (
        "EP1000001",
        False,
        True,
        True,
    )
    c = lib.uncollect("hearing", ["US7000001"])
    assert [m["id"] for m in c["members"]] == ["EP1000001"]
    assert c["seen"] == 3  # removed from the members, still seen: not reported as new again
    (listed,) = lib.list_collections()
    assert (listed["name"], listed["queries"], listed["members"], listed["seen"]) == (
        "hearing",
        2,
        1,
        3,
    )
    assert listed["swept"] is not None
    assert lib.status()["collections"] == 1


def test_a_sweep_says_when_it_may_be_missing_patents() -> None:
    lib = make_library(
        search_providers=[
            FakeProvider(hits=[patent()]),
            FakeProvider("down", raise_=ProviderUnavailable("HTTP 503")),
            FakeProvider("buggy", raise_=ProviderBroken("shape")),
        ]
    )
    lib.create_collection("area", queries=["q"])
    res = lib.sweep("area")
    assert (res["complete"], len(res["new"]), len(res["broken"])) == (False, 1, 1)


def test_collection_errors() -> None:
    lib = make_library()
    with pytest.raises(ValueError, match="lower-case letters"):
        lib.create_collection("My Area")
    with pytest.raises(NotFound, match="no collection named 'absent'"):
        lib.collection("absent")
    with pytest.raises(NotFound):
        lib.collect("absent", [EP])
    lib.create_collection("empty")
    with pytest.raises(ValueError, match="has no queries"):
        lib.sweep("empty")
    assert lib.search("x", collection="empty")["hits"] == []
