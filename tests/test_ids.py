from __future__ import annotations

import pytest

from patent_fetch.ids import PatentId, normalize


@pytest.mark.parametrize(
    ("text", "key", "kind"),
    [
        ("US7861317B2", "US7861317", "B2"),
        ("US 7,861,317 B2", "US7861317", "B2"),
        ("us7861317", "US7861317", None),
        ("US:7861317", "US7861317", None),
        ("EP 2264377 A2", "EP2264377", "A2"),
        ("WO 2012/016009", "WO2012016009", None),
        ("WO2012016009A1", "WO2012016009", "A1"),
        ("US 2012/0029596 A1", "US20120029596", "A1"),
        ("US2012029596", "US20120029596", None),  # the EPO's ten-digit spelling
        ("USD536139S1", "USD536139", "S1"),
        ("US RE45,123 E", "USRE45123", "E"),
        ("https://patents.google.com/patent/EP2264377A2/en", "EP2264377", "A2"),
        (
            "https://worldwide.espacenet.com/publicationDetails/biblio?CC=EP&NR=2264377A2",
            "EP2264377",
            "A2",
        ),
    ],
)
def test_spellings_of_one_number(text: str, key: str, kind: str | None) -> None:
    pid = normalize(text)
    assert (pid.key, pid.kind) == (key, kind)
    assert pid.full == key + (kind or "")
    assert str(pid) == pid.full
    assert normalize(pid) is pid


def test_docdb_spelling_drops_the_zero_only_for_us_applications() -> None:
    assert normalize("US20120029596A1").docdb == "US2012029596"
    assert normalize("US7861317").docdb == "US7861317"
    assert normalize("WO2012016009").docdb == "WO2012016009"
    assert PatentId("EP", "2264377").docdb == "EP2264377"


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("US 10/962,874", "application number"),
        ("7861317", "no country code"),
        ("cochlear implant", "not a patent publication number"),
        ("https://example.org/nothing", "no patent number in the URL"),
    ],
)
def test_what_is_refused_says_why(text: str, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        normalize(text)
