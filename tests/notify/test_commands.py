"""텔레그램 제어 명령 (/pause /resume /mode /watch): 실전 auto 는 거부, 모드는 control 테이블."""

from conftest import make_account

from trader.config import Settings
from trader.control import control
from trader.notify import commands
from trader.notify.telegram import Update, parse_command
from trader.orchestrator.executor import ExecContext


class Sent:
    def __init__(self):
        self.msgs: list[str] = []

    def send(self, text, *, escape=True):
        self.msgs.append(text)
        return True


class Recv:
    def __init__(self, texts):
        self.texts = texts

    def poll(self, offset=None):
        return [Update(update_id=i + 1, text=t, chat_id="1") for i, t in enumerate(self.texts)]


def _ctx(conn, sent):
    accounts = [
        make_account("paper-main", "paper", "auto"),
        make_account("real-main", "real", "off"),
    ]
    return ExecContext(settings=Settings(), accounts=accounts, conn=conn, notifier=sent)


def test_parse_multiword_commands():
    assert parse_command("/mode paper-main approve") == ("mode", "paper-main approve")
    assert parse_command("/watch add 000660") == ("watch", "add 000660")
    assert parse_command("/pause") == ("pause", None)
    assert parse_command("/resume@bot") == ("resume", None)
    assert parse_command("hello") is None


def test_telegram_pause_resume_mode_watch(conn):
    sent = Sent()
    ctx = _ctx(conn, sent)
    r = commands.poll_once(ctx, Recv(["/pause"]))
    assert r["handled"] == 1 and control.is_paused(conn)
    commands.poll_once(ctx, Recv(["/resume"]))
    assert not control.is_paused(conn)
    commands.poll_once(ctx, Recv(["/mode paper-main approve"]))
    assert control.get_account_mode(conn, ctx.accounts[0]) == "approve"
    commands.poll_once(ctx, Recv(["/mode nope auto", "/mode paper-main fast"]))
    assert any("사용법" in m or "알 수 없는" in m for m in sent.msgs[-2:])
    commands.poll_once(ctx, Recv(["/watch add 000660", "/watch add AAPL", "/watch add 000660"]))
    assert control.get_watchlist(conn) == ["000660", "AAPL"]
    commands.poll_once(ctx, Recv(["/watch remove 000660", "/watch list"]))
    assert control.get_watchlist(conn) == ["AAPL"]
    assert "AAPL" in sent.msgs[-1]
    commands.poll_once(ctx, Recv(["/watch add bad/ticker"]))
    assert control.get_watchlist(conn) == ["AAPL"]


def test_real_auto_via_telegram_rejected(conn):
    sent = Sent()
    ctx = _ctx(conn, sent)
    commands.poll_once(ctx, Recv(["/mode real-main auto"]))
    assert control.get_account_mode(conn, ctx.accounts[1]) == "off"
    assert "거부" in sent.msgs[-1] and "control mode" in sent.msgs[-1]
    commands.poll_once(ctx, Recv(["/mode real-main approve"]))
    assert control.get_account_mode(conn, ctx.accounts[1]) == "off"
    assert "거부" in sent.msgs[-1]
    control.set_account_mode(conn, ctx.accounts[1], "approve")
    commands.poll_once(ctx, Recv(["/mode real-main off"]))  # 비상 정지(하향)만 허용
    assert control.get_account_mode(conn, ctx.accounts[1]) == "off"


def test_poll_once_advances_offset_past_filtered_updates(conn):
    class FilteredRecv(Recv):
        last_update_id = 50  # 원본 결과엔 50 까지 있었지만 전부 다른 채팅이라 걸러짐

    sent = Sent()
    ctx = _ctx(conn, sent)
    r = commands.poll_once(ctx, FilteredRecv([]))
    assert r["received"] == 0
    assert control.get_flag(conn, "telegram_offset") == 51
    # 유지된 업데이트가 더 뒤면 그것이 우선
    class MixedRecv(Recv):
        last_update_id = 1

    commands.poll_once(ctx, MixedRecv(["/status", "/status"]))  # update_id 1, 2
    assert control.get_flag(conn, "telegram_offset") == 3
