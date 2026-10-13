from __future__ import annotations

import json

import pytest

from conftest import CLAIMS, FakeProvider, make_library, patent
from patent_fetch import cli, problems
from patent_fetch.library import Library
from patent_fetch.providers import ProviderBroken, ProviderUnavailable

EP = "EP2598092"


def run(
    argv: list[str], capsys: pytest.CaptureFixture[str], lib: Library | None = None
) -> tuple[int, str, str]:
    code = cli.main(argv, library=lib)
    out, err = capsys.readouterr()
    return code, out, err


def test_fetch_then_read(capsys: pytest.CaptureFixture[str]) -> None:
    lib = make_library()
    code, out, _ = run(["fetch", "EP 2598092 B1"], capsys, lib)
    assert code == 0
    assert out.startswith("✓ EP2598092B1  [retrieved]  2018  Fitting a bilateral cochlear implant")
    assert "source=fake  full text" in out
    assert "[library]" in run(["fetch", EP], capsys, lib)[1]
    assert run(["claims", EP], capsys, lib)[1] == CLAIMS + "\n"
    assert run(["text", EP], capsys, lib)[1].endswith(CLAIMS + "\n")
    code, out, _ = run(["record", EP], capsys, lib)
    assert json.loads(out)["provenance"]["source"] == "fake"
    code, out, _ = run(["library", "cochlear"], capsys, lib)
    assert f" ■ {EP}" in out
    assert "1 held" in out
    assert json.loads(run(["--json", "library"], capsys, lib)[1])[0]["id"] == EP
    assert json.loads(run(["status"], capsys, lib)[1])["patents"] == 1
    assert json.loads(run(["rebuild-index"], capsys, lib)[1]) == {"rebuilt": 1}


def test_fetch_reports_each_failure_and_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    lib = make_library(providers=[FakeProvider()])
    code, out, _ = run(["fetch", "US99999999999", "US 10/962,874"], capsys, lib)
    assert code == 1
    assert "✗ US99999999999: US99999999999: no provider returned it" in out
    assert "is a US application number" in out
    code, out, _ = run(["--json", "fetch", "7861317"], capsys, lib)
    assert code == 1
    assert "no country code" in json.loads(out)[0]["error"]
    code, _, err = run(["claims", EP], capsys, lib)
    assert (code, err) == (1, f"patent-fetch: {EP} is not in the library; fetch it first\n")


def test_a_record_without_claims_is_marked(capsys: pytest.CaptureFixture[str]) -> None:
    from patent_fetch.providers import Document  # noqa: PLC0415

    lib = make_library(providers=[FakeProvider(doc=Document(patent(applicants=["ACME"])))])
    _, out, _ = run(["fetch", EP], capsys, lib)
    assert out.startswith(f"~ {EP}  [retrieved]")
    assert "record only, no claims" in out
    assert f" □ {EP}" not in run(["library"], capsys, lib)[1]  # held, but the row has no flag key
    assert "(ACME)" in run(["library"], capsys, lib)[1]


def test_search_prints_statuses_hits_and_the_caveat(capsys: pytest.CaptureFixture[str]) -> None:
    lib = make_library(
        search_providers=[
            FakeProvider(
                hits=[
                    patent(year=2018, inventors=["CHAPA FERNANDO"]),
                    patent("US7000001", year=2001),
                ]
            ),
            FakeProvider("down", raise_=ProviderUnavailable("HTTP 503")),
            FakeProvider("buggy", raise_=ProviderBroken("answer changed shape")),
        ]
    )
    lib.fetch(EP)
    code, out, err = run(
        ["search", "cochlear implant", "--since", "2015", "--country", "EP,US"], capsys, lib
    )
    assert code == 0
    assert "  fake             ok             2 hits" in out
    assert "  down             unavailable    0 hits  HTTP 503" in out
    assert "1 distinct patents (1 filtered out)" in out
    assert "also published as" not in out
    assert f" ■  {EP}" in out
    assert "(CHAPA FERNANDO) [fake]" in out
    assert "No hits is not evidence" in out
    assert err.startswith("! BROKEN: buggy: ProviderBroken: answer changed shape")
    res = json.loads(
        run(["--json", "search", "cochlear implant", "-n", "3", "--refresh"], capsys, lib)[1]
    )
    assert len(res["hits"]) == 2
    code, _, err = run(["search", " "], capsys, lib)
    assert (code, "empty query" in err) == (1, True)
    _, out, _ = run(["problems"], capsys)
    assert "buggy  search  ProviderBroken: answer changed shape" in out
    assert "Traceback" in run(["problems", "--trace", "-n", "1"], capsys)[1]
    assert json.loads(run(["--json", "problems"], capsys)[1])[0]["provider"] == "buggy"
    assert str(problems.log_path()) in out


def test_search_folds_publications_of_one_invention(capsys: pytest.CaptureFixture[str]) -> None:
    hits = [
        patent("US5545219", year=1996, priorities=["US19950414656"]),
        patent("EP0818123", year=1998, priorities=["US19950414656", "US19960616299"]),
    ]
    lib = make_library(search_providers=[FakeProvider(hits=hits)])
    _, out, _ = run(["search", "electrode"], capsys, lib)
    assert "1 distinct patents (1 further publications of the same inventions folded in)" in out
    assert "    US5545219" in out
    assert "      also published as EP0818123\n" in out
    _, out, _ = run(["search", "electrode", "--all-publications"], capsys, lib)
    assert "2 distinct patents\n" in out
    assert "also published as" not in out


def test_collections_and_sweep(capsys: pytest.CaptureFixture[str]) -> None:
    provider = FakeProvider(hits=[patent()])
    lib = make_library(search_providers=[provider])
    assert run(["collection"], capsys, lib)[1] == ""
    code, out, _ = run(
        [
            "collection",
            "hearing",
            "--create",
            "--description",
            "Cochlear work",
            "--query",
            "cochlear implant",
        ],
        capsys,
        lib,
    )
    assert (
        out
        == "hearing  0 members, 0 seen, swept never\n  Cochlear work\n  query: cochlear implant\n"
    )
    _, out, _ = run(["sweep", "hearing"], capsys, lib)
    assert "cochlear implant: 1 hits, 1 new" in out
    assert "1 patents not shown to 'hearing' before" in out
    assert f"  + {EP}" in out
    assert "may be missing" not in out
    assert "0 patents not shown" in run(["sweep", "hearing"], capsys, lib)[1]
    assert run(["collect", "hearing", EP], capsys, lib)[1] == "hearing: 1 members\n"
    lib.fetch(EP)
    assert f" ■ {EP}" in run(["collection", "hearing"], capsys, lib)[1]
    assert (
        "hearing                   1 queries     1 members" in run(["collection"], capsys, lib)[1]
    )
    assert json.loads(run(["--json", "collection"], capsys, lib)[1])[0]["seen"] == 1
    assert (
        json.loads(run(["--json", "collect", "hearing", EP, "--remove"], capsys, lib)[1])["members"]
        == []
    )
    assert json.loads(run(["--json", "collection", "hearing"], capsys, lib)[1])["name"] == "hearing"
    provider._raise = ProviderUnavailable("HTTP 503")
    _, out, _ = run(["sweep", "hearing"], capsys, lib)
    assert "! a provider did not answer" in out
    assert json.loads(run(["--json", "sweep", "hearing"], capsys, lib)[1])["complete"] is False
    code, _, err = run(["sweep", "absent"], capsys, lib)
    assert (code, "no collection named" in err) == (1, True)


def test_providers_lists_what_is_off_paid_and_untested(capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = run(["providers"], capsys)
    assert "  europepmc        search+lookup  ready" in out
    assert "  google-patents   lookup         ready" in out
    assert "  epo-ops          search+lookup  off (needs EPO_OPS_KEY, EPO_OPS_SECRET)\n" in out
    assert "off (needs LENS_API_TOKEN)  NOT YET RUN LIVE" in out
    assert "  serpapi          search         off (needs SERPAPI_API_KEY)  PAID, opt-in\n" in out
    assert len(json.loads(run(["--json", "providers"], capsys)[1])) == 5


def test_the_default_library_comes_from_the_environment(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATENT_FETCH_STORE", "memory")
    assert json.loads(run(["status"], capsys)[1])["store"] == "MemoryStore"


def test_python_m_runs_the_same_program(monkeypatch: pytest.MonkeyPatch) -> None:
    import runpy  # noqa: PLC0415

    monkeypatch.setattr("sys.argv", ["patent-fetch", "providers"])
    with pytest.raises(SystemExit) as e:
        runpy.run_module("patent_fetch", run_name="__main__")
    assert e.value.code == 0
