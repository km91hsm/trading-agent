import { describe, expect, it } from "vitest";
import { buildTape, marketPhase } from "./tape";
import type { Approval, Decision, Order, Position, RiskCheck } from "./types";

describe("marketPhase", () => {
  it("KR 장중(13:41 KST) · US 개장 전(00:41 ET)", () => {
    const p = marketPhase(new Date("2026-10-23T13:41:00+09:00"));
    expect(p.kr).toBe("open");
    expect(p.us).toBe("pre");
    expect(p.label).toBe("KR 정규장 · US 개장 전");
  });
  it("KR 마감(22:41 KST) · US 장중(09:41 ET)", () => {
    const p = marketPhase(new Date("2026-10-23T22:41:00+09:00"));
    expect(p.kr).toBe("closed");
    expect(p.us).toBe("open");
    expect(p.label).toBe("KR 마감 · US 정규장");
  });
  it("주말은 둘 다 휴장", () => {
    const p = marketPhase(new Date("2026-10-24T13:00:00+09:00"));
    expect(p.kr).toBe("weekend");
    expect(p.us).toBe("weekend");
    expect(p.label).toBe("KR 휴장 · US 휴장");
  });
});


const pos = (ticker: string, market: string, qty: number, ev: string): Position => ({
  ticker,
  market,
  qty,
  avg_price: "1",
  current_price: "2",
  eval_amount: ev,
});
const dec = (id: number, ticker: string, market: string, action: string, conf = 0.6): Decision => ({
  id,
  date: "2026-10-23",
  market,
  ticker,
  action,
  target_weight: 0.15,
  confidence: conf,
  session_id: null,
  created_at: "2026-10-23T00:00:00+00:00",
});
const rc = (decision_id: number, approved: boolean, qty: number | null, reasons: string[] = []): RiskCheck => ({
  id: decision_id * 10,
  decision_id,
  account_id: "paper-main",
  approved,
  adjusted_qty: qty,
  reasons,
  created_at: "2026-10-23T00:00:00+00:00",
});
const ord = (decision_id: number, status: string, amount: string): Order => ({
  id: decision_id * 100,
  decision_id,
  account_id: "paper-main",
  ticker: "x",
  market: "KR",
  side: "BUY",
  qty: 12,
  order_type: "limit",
  price: "215000",
  amount,
  status,
  order_no: "1",
  message: "",
  mode: "auto",
  created_at: "2026-10-23T00:31:00+00:00",
});

describe("buildTape", () => {
  const dates = { KR: "2026-10-23", US: "2026-10-23" };
  it("빈 입력은 빈 배열", () => {
    expect(buildTape({ positions: [], decisions: [], riskChecks: [], orders: [], approvals: [], watch: [], dates })).toEqual([]);
  });
  it("보유 → 선정 → 관심 순, 묶음 안은 종목 순", () => {
    const rows = buildTape({
      positions: [pos("005930", "KR", 30, "2682000")],
      decisions: [dec(1, "005380", "KR", "HOLD"), dec(2, "000660", "KR", "ADD")],
      riskChecks: [],
      orders: [],
      approvals: [],
      watch: ["KR:035420"],
      dates,
    });
    expect(rows.map((r) => [r.ticker, r.mark])).toEqual([
      ["005930", "held"],
      ["000660", "pick"],
      ["005380", "pick"],
      ["035420", "watch"],
    ]);
  });
  it("게이트 문구: 승인+체결, 거부, HOLD 는 주문 없음, 분석 없음", () => {
    const rows = buildTape({
      positions: [pos("000660", "KR", 20, "4650000")],
      decisions: [dec(1, "000660", "KR", "ADD", 0.72), dec(2, "AAPL", "US", "BUY"), dec(3, "005930", "KR", "HOLD")],
      riskChecks: [rc(1, true, 12), rc(2, false, 0, ["주 2회 상한", "비중 초과"]), rc(3, false, 0, ["HOLD: 주문 없음"])],
      orders: [ord(1, "filled", "2580000")],
      approvals: [],
      watch: ["035420"],
      dates,
      nav: { KR: "10132400" },
    });
    const by = Object.fromEntries(rows.map((r) => [r.ticker, r]));
    expect(by["000660"].gate).toEqual({ approved: true, text: "12주 체결 · 2,580,000원" });
    expect(by["000660"].weight).toBeCloseTo(0.459, 3);
    expect(by["000660"].action).toBe("ADD");
    expect(by["AAPL"].gate).toEqual({ approved: false, text: "거부 · 주 2회 상한" });
    expect(by["005930"].gate).toEqual({ approved: null, text: "관망 · 주문 없음" });
    expect(by["035420"].gate).toEqual({ approved: null, text: "분석 없음" });
    expect(by["035420"].action).toBeNull();
  });
  it("승인 대기 주문은 만료까지 분을 보여 준다", () => {
    const ap: Approval = {
      id: 41,
      decision_id: 1,
      account_id: "paper-main",
      status: "pending",
      requested_at: "2026-10-23T04:52:00+00:00",
      expires_at: "2026-10-23T05:22:00+00:00",
      decided_at: null,
      order: { ticker: "000660", side: "TRIM", qty: 4, price: "230000", market: "KR" },
    };
    const rows = buildTape({
      positions: [],
      decisions: [dec(1, "000660", "KR", "TRIM")],
      riskChecks: [rc(1, true, 4)],
      orders: [],
      approvals: [ap],
      watch: [],
      dates,
      now: new Date("2026-10-23T04:55:00+00:00"),
    });
    expect(rows[0].gate).toEqual({ approved: true, text: "4주 승인 대기 · 27분 뒤 만료" });
  });
  it("어제 결정은 오늘 시세표에 넣지 않는다", () => {
    const old = { ...dec(9, "005380", "KR", "BUY"), date: "2026-10-22" };
    const rows = buildTape({ positions: [], decisions: [old], riskChecks: [], orders: [], approvals: [], watch: [], dates });
    expect(rows).toEqual([]);
  });
});
