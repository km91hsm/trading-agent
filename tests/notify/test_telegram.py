import logging

import httpx
import pytest
import respx

from trader.config import Secrets
from trader.notify.telegram import NullNotifier, TelegramNotifier, make_notifier

API = "https://api.telegram.org/botTOKEN-SECRET/sendMessage"


@respx.mock
def test_split_and_escape():
    route = respx.post(API).mock(return_value=httpx.Response(200, json={"ok": True}))
    n = TelegramNotifier("TOKEN-SECRET", "123")
    assert n.send("가" * 4500 + " <b>&") is True
    assert route.call_count == 2
    forms = [httpx.QueryParams(c.request.read().decode("utf-8")) for c in route.calls]
    assert forms[1]["text"].endswith("&lt;b&gt;&amp;")
    assert all(f["chat_id"] == "123" and f["parse_mode"] == "HTML" for f in forms)
    assert len(forms[0]["text"]) <= 4000


@respx.mock
def test_split_does_not_break_entities():
    """분할점이 이스케이프 엔티티(&lt; 등) 중간에 오면 안 된다 (리뷰 Important 4)."""
    import re

    route = respx.post(API).mock(return_value=httpx.Response(200, json={"ok": True}))
    assert TelegramNotifier("TOKEN-SECRET", "123").send("a" * 3998 + "<b>&<i>" + "b" * 10) is True
    for c in route.calls:
        text = httpx.QueryParams(c.request.read().decode("utf-8"))["text"]
        assert len(text) <= 4000
        assert not re.search(r"&[a-z]*$", text), text[-10:]
        assert not re.search(r"^[a-z]*;", text), text[:10]


@respx.mock
def test_html_passthrough_when_not_escaped():
    route = respx.post(API).mock(return_value=httpx.Response(200, json={"ok": True}))
    TelegramNotifier("TOKEN-SECRET", "123").send("<b>굵게</b>", escape=False)
    assert "<b>" in httpx.QueryParams(route.calls[0].request.read().decode("utf-8"))["text"]


def test_null_when_no_token():
    n = make_notifier(Secrets())
    assert isinstance(n, NullNotifier) and n.send("x") is True
    assert isinstance(make_notifier(Secrets(TELEGRAM_BOT_TOKEN="t")), NullNotifier)
    assert isinstance(
        make_notifier(Secrets(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c")), TelegramNotifier
    )


@respx.mock
def test_failure_returns_false_without_token_in_log(caplog):
    respx.post(API).mock(return_value=httpx.Response(401, json={"ok": False}))
    with caplog.at_level(logging.WARNING, logger="trader.notify"):
        assert TelegramNotifier("TOKEN-SECRET", "123").send("x") is False
    assert caplog.records and "TOKEN-SECRET" not in caplog.text


@respx.mock
def test_network_failure_returns_false(caplog):
    respx.post(API).mock(side_effect=httpx.ConnectError("x"))
    with caplog.at_level(logging.WARNING, logger="trader.notify"):
        assert TelegramNotifier("TOKEN-SECRET", "123").send("x") is False
    assert "TOKEN-SECRET" not in caplog.text


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_text_is_noop(text):
    assert TelegramNotifier("TOKEN-SECRET", "123").send(text) is True
