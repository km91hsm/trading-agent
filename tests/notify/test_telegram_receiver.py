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


@respx.mock
def test_poll_tracks_last_update_id_even_when_all_filtered():
    """다른 채팅 업데이트만 와도 update_id 를 기억해야 오프셋이 전진한다.

    안 그러면 100건이 쌓인 뒤 getUpdates 가 같은 묶음만 돌려줘 /approve·/pause 가 영원히 안 온다."""
    respx.get(API).mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "result": [_upd(10, 999, "/approve 1"), _upd(11, 999, "스팸")]},
        )
    )
    r = TelegramReceiver("TOKEN-SECRET", "123")
    assert r.poll(offset=7) == [] and r.last_update_id == 11
    respx.get(API).mock(return_value=httpx.Response(200, json={"ok": True, "result": []}))
    assert r.poll(offset=12) == [] and r.last_update_id is None


def test_group_chat_sender_trust(monkeypatch):
    """그룹 채팅: 허용 목록이 없으면 믿지 않고, 있으면 보낸 사람으로 판단."""
    import httpx

    from trader.notify.telegram import TelegramReceiver

    body = {
        "ok": True,
        "result": [
            {
                "update_id": 1,
                "message": {
                    "text": "/approve 3 실전",
                    "chat": {"id": -100, "type": "group"},
                    "from": {"id": 7},
                },
            }
        ],
    }
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    assert TelegramReceiver("T", "-100", http=http).poll()[0].trusted is False
    ok = TelegramReceiver("T", "-100", http=http, allowed_users={"7"}).poll()[0]
    assert ok.trusted and ok.sender_id == "7"
    assert TelegramReceiver("T", "-100", http=http, allowed_users={"8"}).poll()[0].trusted is False


def test_chat_without_type_is_not_trusted(monkeypatch):
    """2-T2 보완: 허용 목록이 없을 때 신뢰는 1:1 채팅(type=private)만 — 종류가 빠지면 믿지
    않는다."""
    import httpx

    from trader.notify.telegram import TelegramReceiver

    body = {
        "ok": True,
        "result": [
            {"update_id": 1, "message": {"chat": {"id": 5}, "from": {"id": 7}, "text": "/pause"}},
            {
                "update_id": 2,
                "message": {"chat": {"id": 5, "type": "private"}, "from": {"id": 7}, "text": "/x"},
            },
        ],
    }
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    got = TelegramReceiver("T", "5", http=http).poll()
    assert [u.trusted for u in got] == [False, True]
