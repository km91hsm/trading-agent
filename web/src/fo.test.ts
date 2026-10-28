import { describe, expect, it } from "vitest";
import { checkDetail, contextRows, daysLeft, decisionDetail, decisionLine, legRows, modeAccountLabel, positionRows, settingsAccounts, splitAccounts, stateBadge, summaryParts, verifiedText } from "./fo";
import type { Account, FoCheck, FoDecisionRow, FoLeg, FoPosition, FoStatus } from "./types";

const pos = (over: Partial<FoPosition> = {}): FoPosition => ({
  id: 1, code: "105W11", kind: "FUTURES", contract_side: "LONG", direction: "LONG", vehicle: "FUTURES",
  status: "held", qty_held: 2, qty_target: 2, leverage_used: 0.35, stop_pt: "345", stop_fut: "345.4",
  tp_pt: null, premium_stop: null, entry_premium: null, approved_mult: null, last_trade_date: "2026-11-12",
  horizon_until: null, opened_at: null, avg: "350", current: "350.5", eval: "50000", budget_used: "120000",
  budget_planned: "500000", budget_free: "1380000",
  budget_limit: "2000000", live_legs: 0, note: null, ...over,
});

describe("파생 화면 변환 (Phase 26)", () => {
  it("계좌를 현물·파생으로 나눈다(asset 없으면 현물)", () => {
    const rows = [{ id: "a" }, { id: "b", asset: "fo" }, { id: "c", asset: "stock" }] as Account[];
    const { stock, fo } = splitAccounts(rows);
    expect(stock.map((a) => a.id)).toEqual(["a", "c"]);
    expect(fo.map((a) => a.id)).toEqual(["b"]);
  });
  it("설정 화면 계좌 모드는 현물 뒤에 파생 계좌까지, 꼬리표로 구분", () => {
    const stock = [{ id: "paper-main", env: "paper", mode: "auto" }] as Account[];
    const fo = [{ id: "paper-fo", env: "paper", mode: "auto", asset: "fo" }] as Account[];
    expect(settingsAccounts(stock, fo).map((a) => a.id)).toEqual(["paper-main", "paper-fo"]);
    expect(modeAccountLabel(stock[0])).toBe("paper-main · 모의 · 자동");
    expect(modeAccountLabel(fo[0])).toBe("paper-fo · 모의 선물옵션 · 자동");
    expect(modeAccountLabel({ id: "real-fo", env: "real", mode: "off", asset: "fo" } as Account)).toBe(
      "real-fo · 실전 선물옵션 · 끔",
    );
  });
  it("positionRows: 방향·수단·보유 방향·배수·예산 문자열", () => {
    const [r] = positionRows([pos()]);
    expect(r).toMatchObject({ what: "롱 · 선물", side: "매수 2계약", dir: "up", leverage: "0.35배", stop: "지수 345.00", budget: "12만 + 계획 50만 / 200만 · 여유 138만", status: "보유", attention: false });
    const [put] = positionRows([pos({ kind: "PUT", direction: "SHORT", vehicle: "OPTION_BUY", premium_stop: "2.5", qty_target: 1 })]);
    expect(put.what).toBe("숏 · 옵션 매수(풋)");
    expect(put.side).toBe("매수 2계약 → 1");
    expect(put.dir).toBe("dn");
    expect(put.stop).toBe("지수 345.00 · 프리미엄 2.50");
  });
  it("unknown·청산 실패·정산 대기는 주의(황) 표시", () => {
    const rows = positionRows([pos({ status: "unknown" }), pos({ status: "exit_failed" }), pos({ status: "closing" })]);
    expect(rows.map((r) => r.attention)).toEqual([true, true, false]);
    expect(rows[0].status).toBe("대조 필요");
  });
  it("legRows: 의도·진행·가격·상태", () => {
    const leg: FoLeg = { id: 3, position_id: 1, code: "105W11", intent: "RESIZE_DOWN", round: 2, attempt: 2, status: "accepted", order_no: "1", qty: 2, filled_qty: 1, price: null, order_type: "MARKET", sent_at: null, created_at: "" };
    expect(legRows([leg])[0]).toMatchObject({ intent: "감액 · 재평가 2", progress: "1/2계약", price: "시장가", status: "접수", attempt: "2차", live: true });
  });
  it("decisionLine", () => {
    expect(decisionLine(null)).toBe("오늘 결정 없음");
    const d = { intent: "OPEN", direction: "SHORT", vehicle: "OPTION_SELL", target_leverage: 1, confidence: 0.7, record_only: null } as FoDecisionRow;
    expect(decisionLine(d)).toBe("진입 숏 · 옵션 매도 · 배수 1.00 · 확신 0.70");
  });
  it("확장 포지션 행: 평단·현재가·평가손익(색)·익절·선물 손절·만기까지 일수", () => {
    const [r] = positionRows([pos({ tp_pt: "360", stop_fut: "345.4", eval: "-150000" })], "2026-10-28");
    expect(r).toMatchObject({ avg: "350.00", current: "350.50", pnl: "-15만", pnlDir: "dn", tp: "지수 360.00", stopFut: "345.40", daysLeft: "D-15" });
    expect(daysLeft("2026-10-28", "2026-10-28")).toBe("D-day");
    expect(daysLeft(null, "2026-10-28")).toBe("—");
  });
  it("legRows: 주문번호·전송 시각", () => {
    const leg: FoLeg = { id: 3, position_id: 1, code: "105W11", intent: "OPEN", round: 0, attempt: 1, status: "filled", order_no: "0001234", qty: 2, filled_qty: 2, price: "350.5", order_type: "LIMIT", sent_at: "2026-10-28T00:01:02+00:00", created_at: "" };
    expect(legRows([leg])[0]).toMatchObject({ orderNo: "0001234", sentAt: "09:01" });
  });
});

const status = (over: Partial<FoStatus> = {}): FoStatus => ({
  id: "paper-fo", env: "paper", mode: "auto", mode_note: null, paused: false, state: "active", enabled: true,
  vehicles: ["FUTURES", "OPTION_BUY", "OPTION_SELL"], verified: { FUTURES: true, OPTION_BUY: false, OPTION_SELL: false },
  halted: false, pending_entry: false, nav_date: "2026-10-28", nav: "50000000", cash: "40000000", margin_total: "10000000",
  add_margin: "0", margin_pct: 0.2, positions: [], decision: null, ...over,
});

describe("파생 요약·상태 (대시보드·파생 화면)", () => {
  it("stateBadge: 상태마다 라벨·색", () => {
    expect(stateBadge("active")).toEqual({ label: "자동", kind: "ok" });
    expect(stateBadge("disabled")).toEqual({ label: "레인 꺼짐", kind: "neutral" });
    expect(stateBadge("unverified")).toEqual({ label: "검증 전", kind: "pending" });
    expect(stateBadge("halted")).toEqual({ label: "신규 금지(손실 한도)", kind: "danger" });
    expect(stateBadge("off")).toEqual({ label: "실전 미지원", kind: "neutral" });
  });
  it("summaryParts: 순자산·증거금(추가증거금이면 경고)·포지션·오늘 결정", () => {
    expect(summaryParts(status())).toEqual([
      { text: "순자산 5,000만" }, { text: "증거금 20%" }, { text: "포지션 없음" }, { text: "오늘 결정 없음" },
    ]);
    const p = summaryParts(status({ add_margin: "300000", positions: [
      { code: "A05611", kind: "FUTURES", contract_side: "LONG", direction: "LONG", qty: 2, status: "held" },
      { code: "B05611945", kind: "CALL", contract_side: "SHORT", direction: "SHORT", qty: 1, status: "held" },
    ] }));
    expect(p[1]).toEqual({ text: "증거금 20% · 추가증거금 30만", tone: "danger" });
    expect(p[2]).toEqual({ text: "A05611 매수 2계약 외 1건", tone: "up" });
    expect(summaryParts(status({ nav: null, margin_pct: null }))[0]).toEqual({ text: "순자산 미확인" });
  });
  it("verifiedText: 허용 수단의 검증 여부", () => {
    expect(verifiedText(status())).toBe("선물 ✓ · 옵션 매수 – · 옵션 매도 –");
  });
  it("checkDetail: 게이트 판정 상세 한 줄들", () => {
    const c: FoCheck = { approved: true, reasons: [], note: null, status: null, record_only: null, error: null,
      leverage_used: 0.4, exposure_after: "20000000", stress_loss_after: "900000", cross_factor: 0.5, n_target: 2,
      lifetime_budget: { used: "0", planned: "300000", limit: "1000000", free: "700000" } };
    expect(checkDetail(c)).toEqual(["배수 0.40", "노출 2,000만", "스트레스 손실 90만", "수명 예산 0 + 계획 30만 / 100만", "대조 배수 0.5", "목표 2계약"]);
    expect(checkDetail({ approved: false, reasons: ["x"], note: null, status: null, record_only: null, error: null })).toEqual([]);
  });
  it("contextRows: 지수·국면·전략가·한도", () => {
    const rows = contextRows({ source: "settings", index: null, regime: { state: "bear", count: 2 }, market_view: { stance: "RISK_OFF", confidence: 0.8 },
      limits: { max_leverage: 2, regime_cap: 0, max_contracts: 4, risk_per_trade_pct: 0.02, entry_window: { start: "09:00", cutoff: "15:30" } }, products: null });
    expect(rows).toEqual([
      ["코스피200", "—"], ["국면", "하락 (2/3)"], ["전략가 제안", "위험 회피 · 확신 0.80"],
      ["최대 배수", "2.00배 · 국면 상한 신규 금지"], ["최대 계약", "4계약"], ["1회 위험", "2.0%"], ["진입 창", "09:00~15:30"],
    ]);
  });
  it("decisionDetail: 손절·익절·기간·결정 때 지수·현물 대비", () => {
    const d = { stop_pt: 345, take_profit_pt: 360, horizon_days: 3, index_at_decision: "350.2", vs_spot: "현물 헤지와 같은 방향" } as FoDecisionRow;
    expect(decisionDetail(d)).toEqual(["손절선 지수 345.00", "익절선 지수 360.00", "보유 3일", "결정 때 지수 350.20", "현물 대비: 현물 헤지와 같은 방향"]);
    expect(decisionDetail(null)).toEqual([]);
  });
});
