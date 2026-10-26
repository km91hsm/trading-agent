"""Phase 25 단계 5: 분 단위 LLM 판정자 — 비동기 판정·다음 틱 회수·가격 이탈·재시작·클램프."""

from datetime import timedelta
from decimal import Decimal

from scalp.test_engine import NOW, D, _bars, _ctx, _scan
from trader.agents.schemas import ScalpAdvice, ScalpVerdict
from trader.config import ScalpCfg
from trader.scalp import engine


class SyncJudge(engine.ScalpJudge):
    """테스트용: 제출 즉시 결과(스레드 없음)."""

    def __init__(self, answer):
        self.answer = answer
        self.seen = []
        self._futs = {}

    def submit(self, key, payload, schema):
        self.seen.append((key, payload, schema))
        from concurrent.futures import Future

        f = Future()
        ans = self.answer(key, payload, schema) if callable(self.answer) else self.answer
        if isinstance(ans, BaseException):
            f.set_exception(ans)
        else:
            f.set_result(ans)
        self._futs[key] = f


GO = ScalpVerdict(
    go=True,
    size="half",
    take_profit_pct=0.02,
    stop_pct=0.02,
    max_hold_min=120,
    reason="돌파 확인",
    confidence=0.7,
)
NOGO = GO.model_copy(update={"go": False, "reason": "추격"})


def _judged_ctx(conn, answer, **kw):
    ctx = _ctx(conn, minutes={"005930": _bars(True)}, prices={"005930": Decimal("10050")}, **kw)
    ctx.settings = ctx.settings.model_copy(update={"scalp": ScalpCfg(enabled=True, judge=True)})
    ctx.judge = SyncJudge(answer)
    return ctx


def test_judge_go_enters_next_tick(conn, project):
    _scan(["005930"])
    ctx = _judged_ctx(conn, GO)
    out = engine.tick(ctx, market="KR", today=D)
    assert out["entries"] == []  # 이번 틱은 제출만
    key, payload, schema = ctx.judge.seen[0]
    assert key == "entry:005930" and schema is ScalpVerdict and len(payload["bars"]) == 20
    out = engine.tick(ctx, market="KR", today=D)
    assert [e["ticker"] for e in out["entries"]] == ["005930"]
    st = engine.load_state(None, D, "KR")
    pos = st["open"][0]
    assert pos["judged"] is True and pos["qty"] == {"paper-main": 24}  # half → 비중 2.5%


def test_verdict_cannot_widen_stop(conn, project):
    _scan(["005930"])
    ctx = _judged_ctx(conn, GO)
    engine.tick(ctx, market="KR", today=D)
    engine.tick(ctx, market="KR", today=D)
    pos = engine.load_state(None, D, "KR")["open"][0]
    cfg = ctx.settings.scalp
    assert pos["stop_pct"] == cfg.stop_pct and pos["max_hold_min"] == cfg.max_hold_min
    assert pos["take_profit_pct"] == 0.02  # 0.5~2배 안이면 판정값


def test_judge_nogo_no_entry(conn, project):
    _scan(["005930"])
    ctx = _judged_ctx(conn, NOGO)
    engine.tick(ctx, market="KR", today=D)
    out = engine.tick(ctx, market="KR", today=D)
    st = engine.load_state(None, D, "KR")
    assert out["entries"] == [] and st["open"] == [] and "005930" in st["done"]


def test_judge_drift_discards(conn, project):
    _scan(["005930"])
    ctx = _judged_ctx(conn, GO)
    engine.tick(ctx, market="KR", today=D)
    ctx.market_client.prices["005930"] = Decimal("10200")  # 신호가 10050 대비 +1.5%
    out = engine.tick(ctx, market="KR", today=D)
    assert out["entries"] == [] and "005930" in engine.load_state(None, D, "KR")["done"]


def test_pending_lost_after_restart_no_entry(conn, project):
    _scan(["005930"])
    ctx = _judged_ctx(conn, GO)
    engine.tick(ctx, market="KR", today=D)
    ctx.judge = SyncJudge(GO)  # 재시작: future 가 없다
    out = engine.tick(ctx, market="KR", today=D)
    assert out["entries"] == [] and engine.load_state(None, D, "KR")["pending"] == {}


def test_judge_timeout_no_entry(conn, project):
    _scan(["005930"])
    ctx = _judged_ctx(conn, GO)
    engine.tick(ctx, market="KR", today=D)

    class Never(SyncJudge):
        def poll(self, key):
            return "pending"

    ctx.judge = Never(GO)
    ctx.now = lambda: NOW + timedelta(seconds=10)
    assert engine.tick(ctx, market="KR", today=D)["entries"] == []
    assert "005930" in engine.load_state(None, D, "KR")["pending"]  # 아직 기다린다
    ctx.now = lambda: NOW + timedelta(seconds=120)
    engine.tick(ctx, market="KR", today=D)
    st = engine.load_state(None, D, "KR")
    assert st["pending"] == {} and st["open"] == []


def test_stop_loss_exits_immediately_while_advice_pending(conn, project):
    _scan(["005930"])
    ctx = _judged_ctx(
        conn,
        lambda k, p, s: (
            GO if s is ScalpVerdict else ScalpAdvice(action="hold", reason="r", confidence=0.5)
        ),
    )
    engine.tick(ctx, market="KR", today=D)
    engine.tick(ctx, market="KR", today=D)

    class Pending(SyncJudge):
        def poll(self, key):
            return "pending"

    ctx.judge = Pending(GO)
    ctx.market_client.prices["005930"] = Decimal("9800")  # 손절선 −1% 아래
    out = engine.tick(ctx, market="KR", today=D)
    assert out["exits"] and out["exits"][0]["reason"] == "stop"


def test_advice_exit_closes_early(conn, project):
    _scan(["005930"])
    advice = ScalpAdvice(action="exit", reason="VWAP 이탈", confidence=0.8)
    ctx = _judged_ctx(conn, lambda k, p, s: GO if s is ScalpVerdict else advice)
    engine.tick(ctx, market="KR", today=D)
    engine.tick(ctx, market="KR", today=D)  # 진입
    ctx.now = lambda: NOW + timedelta(minutes=6)
    engine.tick(ctx, market="KR", today=D)  # 조언 제출(동기 판정)
    out = engine.tick(ctx, market="KR", today=D)  # 회수 → 조기 청산
    assert out["exits"] and out["exits"][0]["reason"] == "judge_exit"


def test_report_go_vs_nogo_summary(conn, project):
    from scalp.test_engine import _bars as bars

    _scan(["005930", "000660"])
    ctx = _ctx(
        conn,
        minutes={"005930": bars(True), "000660": bars(True)},
        prices={"005930": Decimal("10050"), "000660": Decimal("10050")},
    )
    ctx.settings = ctx.settings.model_copy(
        update={"scalp": ScalpCfg(enabled=True, judge=True, max_positions=2)}
    )
    ctx.judge = SyncJudge(lambda k, p, s: GO if "005930" in k else NOGO)
    engine.tick(ctx, market="KR", today=D)
    engine.tick(ctx, market="KR", today=D)  # 005930 GO 진입, 000660 NO_GO
    ctx.market_client.prices["005930"] = Decimal("10300")
    ctx.judge = SyncJudge(ScalpAdvice(action="hold", reason="r", confidence=0.5))
    engine.tick(ctx, market="KR", today=D)  # 익절(+2.5% ≥ 판정 2%)
    after = bars(True, price="10200")  # 신호 뒤 봉: +1.5% 익절
    r = engine.report(
        None,
        market="KR",
        days=1,
        today=D,
        judge={"cfg": ScalpCfg(), "close_hm": "15:30", "minutes_fn": lambda t: after},
    )
    j = r["judge"]
    assert j["counts"] == {"go": 1, "no_go": 1, "none": 0}
    assert j["go_trades"] == 1 and j["go_avg_ret"] > 0
    assert j["no_go_measured"] in (0, 1)
