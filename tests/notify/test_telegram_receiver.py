import httpx
import respx

from trader.config import Secrets
from trader.notify.telegram import TelegramReceiver, make_receiver, parse_command

API = "https://api.telegram.org/botTOKEN-SECRET/getUpdates"


def _upd(uid, chat, text):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


@respx.mock
def test_poll_filters_chat_and_parses():
    route = respx.get(API).mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    _upd(10, 999, "/approve 1"),  # 다른 chat → 무시
                    _upd(11, 123, "/approve 5"),
                    _upd(12, 123, "안녕"),
                    {"update_id": 13, "edited_message": {"chat": {"id": 123}, "text": "x"}},
                ],
            },
        )
    )
    r = TelegramReceiver("TOKEN-SECRET", "123")
    ups = r.poll(offset=7)
    assert route.calls[0].request.url.params["offset"] == "7"
    assert [(u.update_id, u.text) for u in ups] == [(11, "/approve 5"), (12, "안녕")]
    assert parse_command("/approve 5") == ("approve", "5")
    assert parse_command("/reject@my_bot 7") == ("reject", "7")
    assert parse_command("/status") == ("status", None)
    assert parse_command("안녕") is None and parse_command("/foo") is None


@respx.mock
def test_poll_failure_returns_empty():
    respx.get(API).mock(return_value=httpx.Response(500))
    assert TelegramReceiver("TOKEN-SECRET", "123").poll(offset=None) == []


def test_make_receiver_requires_secrets():
    assert make_receiver(Secrets()) is None
    assert make_receiver(Secrets(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="1")) is not None
