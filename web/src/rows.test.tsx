// 전체 검토 W9·W10: 좁은 화면 상세 펼침 행과 키보드로 여는 행 버튼.
import { act } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it } from "vitest";
import { DetailRow, MoreTh, RowButton } from "./components/Table";
import { OrdersTable, PositionsTable } from "./components/Tables";
import type { Order, Position } from "./types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement | null = null;
afterEach(() => {
  host?.remove();
  host = null;
});

function mount(el: React.ReactElement): HTMLDivElement {
  host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => root.render(el));
  return host;
}

describe("DetailRow (W9)", () => {
  it("상세 버튼이 aria-expanded·aria-controls 로 숨긴 값 행을 펼친다", () => {
    const el = mount(
      <table>
        <thead>
          <tr>
            <th>종목</th>
            <MoreTh />
          </tr>
        </thead>
        <tbody>
          <DetailRow label="005930" span={2} details={[{ label: "손절가", value: "90,000" }]}>
            <td className="core">005930</td>
          </DetailRow>
        </tbody>
      </table>,
    );
    const btn = el.querySelector("button")!;
    const detail = el.querySelector("tr.row-detail") as HTMLTableRowElement;
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    expect(btn.getAttribute("aria-controls")).toBe(detail.id);
    expect(btn.getAttribute("aria-label")).toBe("005930 상세");
    expect(detail.hidden).toBe(true);
    act(() => btn.click());
    expect(btn.getAttribute("aria-expanded")).toBe("true");
    expect(detail.hidden).toBe(false);
    expect(btn.textContent).toContain("상세"); // 보이는 글자가 접근 이름 안에 (WCAG 2.5.3)
    expect(detail.textContent).toContain("손절가");
    expect(detail.textContent).toContain("90,000");
  });

  it("보유 표의 상세에 수량·평균단가·손절가가, 주문 표의 상세에 수량·가격이 들어간다", () => {
    const p: Position = { ticker: "005930", market: "KR", qty: 3, avg_price: "100", current_price: "110", eval_amount: "330" };
    const pos = renderToStaticMarkup(<PositionsTable rows={[p]} nav="1000" />);
    expect(pos).toMatch(/<dt>수량<\/dt><dd>3<\/dd>/);
    expect(pos).toContain("<dt>평균단가</dt>");
    expect(pos).toContain("<dt>손절가</dt>");
    const o = { id: 7, decision_id: 1, account_id: "paper-main", ticker: "005930", market: "KR", side: "BUY", qty: 2, order_type: "LIMIT", price: "100", amount: "200", status: "accepted", order_no: "1", message: null, mode: "auto", created_at: "2026-10-26T01:00:00+00:00" } as unknown as Order;
    const ord = renderToStaticMarkup(<OrdersTable rows={[o]} />);
    expect(ord).toMatch(/<dt>수량<\/dt><dd>2<\/dd>/);
    expect(ord).toContain("<dt>가격</dt>");
  });
});

describe("RowButton (W10)", () => {
  it("버튼이라 Enter/Space 로 열리고, 행 클릭과 두 번 토글되지 않는다", () => {
    let n = 0;
    const el = mount(
      <table>
        <tbody>
          <tr onClick={() => (n += 1)}>
            <td>
              <RowButton expanded={false} onClick={() => (n += 10)}>
                005930
              </RowButton>
            </td>
          </tr>
        </tbody>
      </table>,
    );
    const btn = el.querySelector("button")!;
    expect(btn.getAttribute("type")).toBe("button");
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    act(() => btn.click()); // 키보드 Enter/Space 도 네이티브 버튼에서 click 으로 온다
    expect(n).toBe(10);
  });
  it("선택형은 aria-pressed", () => {
    const out = renderToStaticMarkup(
      <RowButton pressed onClick={() => {}}>
        v1
      </RowButton>,
    );
    expect(out).toContain('aria-pressed="true"');
    expect(out).not.toContain("aria-expanded");
  });
});
