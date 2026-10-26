"""Phase 24: 텔레그램 문구 형식 고정 (골든). 문구를 바꾸면 여기도 같이 바꾼다."""

from decimal import Decimal

from trader import names
from trader.notify import messages as M

OP = M.DecisionBrief(
    action="BUY",
    target_weight=0.15,
    confidence=0.62,
    rationale="실적이 개선된다. 외인 순매수가 이어진다. 세 번째 문장은 잘린다.",
    take_profit=80000,
    stop_loss=65000,
    horizon_days=10,
)


def test_label_is_name_paren_code():
    assert names.label("005930", "삼성전자") == "삼성전자(005930)"
    assert names.label("005930", None) == "005930"
    assert names.label("AAPL", "AAPL") == "AAPL"


def test_money_and_sentences():
    assert M.money(Decimal("71500"), "KR") == "71,500원"
    assert M.money("227.5", "US") == "$227.50"
    assert M.money(None, "KR") == "—"
    assert M.first_sentences("가. 나. 다.") == "가. 나."


def test_order_with_opinion():
    text = M.order(
        account="paper-main",
        market="KR",
        label="삼성전자(005930)",
        side="BUY",
        qty=3,
        price=Decimal("71500"),
        mode="auto",
        accepted=True,
        order_no="0001",
        message="정상 처리",
        opinion=OP,
    )
    assert text == (
        "[주문] paper-main · 삼성전자(005930)\n"
        "매수 3주 @71,500원 · 자동\n"
        "접수번호 0001 · 정상 처리\n"
        "\n"
        "최종 의견: 매수 · 목표 비중 15% · 확신 0.62\n"
        "근거: 실적이 개선된다. 외인 순매수가 이어진다.\n"
        "목표가 80,000원 · 손절가 65,000원 · 보유 10일"
    )


def test_order_failure_has_no_opinion_and_kind_tail():
    text = M.order(
        account="real-main",
        market="US",
        label="Apple Inc.(AAPL)",
        side="SELL",
        qty=1,
        price=None,
        mode="approve",
        accepted=False,
        order_no=None,
        message="40100000: 장 운영 시간이 아님",
        opinion=OP,
        kind="재평가",
    )
    assert text == (
        "[주문 실패 · 재평가] real-main · Apple Inc.(AAPL)\n"
        "매도 1주 시장가 · 승인\n"
        "접수번호 — · 40100000: 장 운영 시간이 아님"
    )


def test_approval_request():
    text = M.approval_request(
        approval_id=12,
        account="real-main",
        market="KR",
        label="삼성전자(005930)",
        side="BUY",
        qty=2,
        price=Decimal("70000"),
        reasons=["confidence<0.5 → 수량 반감"],
        opinion=M.DecisionBrief(action="BUY", target_weight=0.1, confidence=0.45),
    )
    assert text == (
        "[승인 요청 #12] real-main · 삼성전자(005930)\n"
        "매수 2주 @70,000원 = 140,000원\n"
        "리스크 판정: confidence<0.5 → 수량 반감\n"
        "\n"
        "최종 의견: 매수 · 목표 비중 10% · 확신 0.45\n"
        "\n"
        "/approve 12 · /reject 12 (30분 안)"
    )


def test_stop_loss_and_failure():
    assert M.stop_loss(
        account="paper-main",
        market="KR",
        label="삼성전자(005930)",
        qty=3,
        quote=64000,
        level=65000,
        order_no="9",
    ) == (
        "[손절] paper-main · 삼성전자(005930)\n"
        "3주 즉시 매도 · 접수번호 9\n"
        "현재가 64,000원 ≤ 손절선 65,000원"
    )
    assert M.failure("analyze 실패", "KR 005930", ValueError("x")) == (
        "[analyze 실패] KR 005930\n오류: ValueError: x"
    )
    assert M.event("스캔", "KR 2026-10-26: picks 3") == "[스캔] KR 2026-10-26: picks 3"


def test_decision_brief_from_dict_and_model():
    from trader.agents.schemas import Decision

    d = Decision(
        ticker="005930",
        action="TRIM",
        target_weight=0.05,
        order_type="LIMIT",
        horizon_days=5,
        confidence=0.7,
        rationale="r",
        invalidation_conditions=[],
    )
    assert M.DecisionBrief.of(d).action == "TRIM"
    assert M.DecisionBrief.of({"action": "SELL", "confidence": 0.8}).confidence == 0.8
    assert M.DecisionBrief.of(None) is None and M.DecisionBrief.of({}) is None


def test_approval_request_shows_env_amount_and_kst_expiry():
    """전체 검토 T1: 모의/실전·금액·만료 시각(KST)·실전 확인어."""
    text = M.approval_request(
        approval_id=12,
        account="real-main",
        market="KR",
        label="삼성전자(005930)",
        side="BUY",
        qty=10,
        price=Decimal("71500"),
        reasons=[],
        env="real",
        expires_at="2026-10-26T01:42:00+00:00",
    )
    assert text.splitlines()[0] == "[승인 요청 #12 · 실전] real-main · 삼성전자(005930)"
    assert "매수 10주 @71,500원 = 715,000원" in text and "리스크 한도 통과" in text
    assert text.endswith("/approve 12 실전 · /reject 12 — 10:42 KST 까지")
    paper = M.approval_request(
        approval_id=3,
        account="paper-main",
        market="US",
        label="AAPL",
        side="BUY",
        qty=2,
        price=None,
        quote_price="200",
        reasons=["확신 0.45 → 수량 절반"],
        env="paper",
    )
    assert "시장가 = $400.00 (현재가 기준)" in paper and "리스크 판정: 확신 0.45" in paper
    assert "/approve 3 · /reject 3 (30분 안)" in paper
