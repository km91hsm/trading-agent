import { describe, expect, it } from "vitest";
import { decisionLine, legRows, positionRows, splitAccounts } from "./fo";
import type { Account, FoDecisionRow, FoLeg, FoPosition } from "./types";

const pos = (over: Partial<FoPosition> = {}): FoPosition => ({
  id: 1, code: "105W11", kind: "FUTURES", contract_side: "LONG", direction: "LONG", vehicle: "FUTURES",
  status: "held", qty_held: 2, qty_target: 2, leverage_used: 0.35, stop_pt: "345", stop_fut: "345.4",
  tp_pt: null, premium_stop: null, entry_premium: null, approved_mult: null, last_trade_date: "2026-11-12",
  horizon_until: null, opened_at: null, avg: "350", current: "350.5", eval: "50000", budget_used: "120000",
  budget_limit: "2000000", live_legs: 0, note: null, ...over,
});

describe("파생 화면 변환 (Phase 26)", () => {
  it("계좌를 현물·파생으로 나눈다(asset 없으면 현물)", () => {
    const rows = [{ id: "a" }, { id: "b", asset: "fo" }, { id: "c", asset: "stock" }] as Account[];
    const { stock, fo } = splitAccounts(rows);
    expect(stock.map((a) => a.id)).toEqual(["a", "c"]);
    expect(fo.map((a) => a.id)).toEqual(["b"]);
  });
  it("positionRows: 방향·수단·보유 방향·배수·예산 문자열", () => {
    const [r] = positionRows([pos()]);
    expect(r).toMatchObject({ what: "롱 · 선물", side: "매수 2계약", dir: "up", leverage: "0.35배", stop: "지수 345.00", budget: "12만 / 200만", status: "보유", attention: false });
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
});
