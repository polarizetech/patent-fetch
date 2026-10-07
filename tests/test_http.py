from __future__ import annotations

import io
import urllib.error
from email.message import Message
from typing import Any

import pytest

from patent_fetch.http import Http, user_agent


class _Resp(io.BytesIO):
    status = 200
    headers = Message()

    def geturl(self) -> str:
        return "https://final.example/x"

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)


def test_success_is_counted_and_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_urlopen(req: Any, timeout: float) -> _Resp:
        seen["ua"] = req.get_header("User-agent")
        return _Resp(b"hello")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    h = Http()
    r = h.get("https://x.example/a")
    assert (r.status, r.body, r.url) == (200, b"hello", "https://final.example/x")
    assert (h.calls, h.bytes_in, h.log) == (1, 5, [("https://x.example/a", 200, 5)])
    assert seen["ua"].startswith("patent-fetch/")


def test_a_429_is_returned_on_the_first_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retrying a 429 into a generic error would hide the rate limit from every caller."""
    calls = {"n": 0}

    def raise_429(*_a: Any, **_k: Any) -> None:
        calls["n"] += 1
        raise urllib.error.HTTPError("https://x", 429, "Too Many", Message(), None)

    monkeypatch.setattr("urllib.request.urlopen", raise_429)
    r = Http(retries=3).get("https://x")
    assert r.status == 429
    assert calls["n"] == 1


def test_5xx_and_transport_errors_are_retried_then_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    errors = iter(
        [
            urllib.error.HTTPError("https://x", 503, "Unavailable", Message(), io.BytesIO(b"x")),
            OSError("reset"),
        ]
    )

    def flaky(*_a: Any, **_k: Any) -> None:
        raise next(errors)

    monkeypatch.setattr("urllib.request.urlopen", flaky)
    h = Http(retries=1)
    with pytest.raises(ConnectionError, match="after 2 attempts"):
        h.get("https://x")
    assert h.calls == 2


def test_user_agent_carries_contact_only_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "mailto" not in user_agent()
    monkeypatch.setenv("PATENT_FETCH_EMAIL", "someone@example.org")
    assert user_agent().endswith("(mailto:someone@example.org)")


def test_post_returns_an_answer_and_a_refusal_but_raises_on_an_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def ok(req: Any, timeout: float) -> _Resp:
        seen["body"], seen["type"] = req.data, req.get_header("Content-type")
        return _Resp(b"{}")

    monkeypatch.setattr("urllib.request.urlopen", ok)
    h = Http()
    r = h.post("https://x.example/token", b"grant", {"Content-Type": "application/json"})
    assert (r.status, r.body, seen) == (200, b"{}", {"body": b"grant", "type": "application/json"})

    def refuse(code: int) -> Any:
        def raiser(*_a: Any, **_k: Any) -> None:
            raise urllib.error.HTTPError("https://x", code, "no", Message(), io.BytesIO(b"why"))

        return raiser

    monkeypatch.setattr("urllib.request.urlopen", refuse(401))
    r = h.post("https://x.example/token", b"")
    assert (r.status, r.body) == (401, b"why")  # a 4xx is an answer: the caller decides
    monkeypatch.setattr("urllib.request.urlopen", refuse(502))
    with pytest.raises(ConnectionError, match="HTTP 502"):
        h.post("https://x.example/token", b"")

    def reset(*_a: Any, **_k: Any) -> None:
        raise OSError("reset")

    monkeypatch.setattr("urllib.request.urlopen", reset)
    with pytest.raises(ConnectionError, match="reset"):
        h.post("https://x.example/token", b"")
    assert h.calls == 4  # one attempt each: a POST is not retried blindly
