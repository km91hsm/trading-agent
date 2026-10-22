import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Badge } from "./components/Badge";
import { EmptyState } from "./components/EmptyState";
import { Glyph } from "./components/Glyph";
import { Delta, Mark, Num, Table, Td, Th } from "./components/Table";
import { Term } from "./components/Term";

const html = (el: React.ReactElement) => renderToStaticMarkup(el);

describe("Table 원시요소", () => {
  it("숫자 머리·셀은 num 클래스, 상승은 up + ▲ SVG", () => {
    const out = html(
      <Table>
        <thead>
          <tr>
            <Th>종목</Th>
            <Th num>현재가</Th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <Td>005930</Td>
            <Td num>
              <Num value="89400" market="KR" />
            </Td>
            <Td num>
              <Delta value={1.53} pct />
            </Td>
          </tr>
        </tbody>
      </Table>,
    );
    expect(out).toContain('class="num"');
    expect(out).toContain("89,400원");
    expect(out).toContain('class="delta up"');
    expect(out).toContain("<svg");
    expect(out).toContain("1.53%");
  });
  it("하락은 dn, 0 은 중립", () => {
    expect(html(<Delta value={-0.6} pct />)).toContain('class="delta dn"');
    expect(html(<Delta value={0} />)).toContain('class="delta flat"');
    expect(html(<Delta value={null} />)).toContain("—");
  });
  it("Mark 는 보유·선정·관심 기호를 SVG 로 그린다", () => {
    for (const kind of ["held", "pick", "watch"] as const) {
      const out = html(<Mark kind={kind} />);
      expect(out).toContain("<svg");
      expect(out).toContain(`data-mark="${kind}"`);
    }
  });
});

describe("Badge / EmptyState / Glyph / Term", () => {
  it("env 배지는 data-env 를 가진다", () => {
    expect(html(<Badge kind="env" value="real" />)).toContain('data-env="real"');
    expect(html(<Badge kind="env" value="real" />)).toContain("실전");
    expect(html(<Badge kind="env" value="paper" />)).toContain("모의");
  });
  it("status 배지는 상태값을 data-status 로 남긴다", () => {
    expect(html(<Badge kind="status" value="unknown" />)).toContain('data-status="unknown"');
    expect(html(<Badge kind="status" value="unknown" />)).toContain("상태 미확인");
  });
  it("EmptyState 는 제목·힌트를 보여 준다", () => {
    const out = html(<EmptyState title="오늘 결정이 아직 없어요" hint="scan → analyze 가 돌면 채워져요" />);
    expect(out).toContain("오늘 결정이 아직 없어요");
    expect(out).toContain("scan → analyze 가 돌면 채워져요");
  });
  it("Glyph 는 이름을 data-glyph 로, title 이 있으면 접근성 제목", () => {
    const out = html(<Glyph name="up" title="상승" />);
    expect(out).toContain('data-glyph="up"');
    expect(out).toContain("<title>상승</title>");
    expect(html(<Glyph name="menu" />)).toContain('aria-hidden="true"');
  });
  it("Term 은 라벨과 설명(title)을 단다", () => {
    const out = html(<Term k="stop_loss" />);
    expect(out).toContain("손절선");
    expect(out).toContain("title=");
  });
});

import { MemoryRouter } from "react-router-dom";
import { Shell } from "./components/Shell";
import { AppContext, type AppState } from "./hooks";

function fakeState(over: Partial<AppState> = {}): AppState {
  return {
    accounts: [
      { id: "paper-main", env: "paper", markets: ["KR", "US"], mode: "auto", yaml_mode: "auto", stance_by_market: {}, nav_by_market: {}, pending_approvals: 0 },
      { id: "real-main", env: "real", markets: ["KR"], mode: "off", yaml_mode: "off", stance_by_market: {}, nav_by_market: {}, pending_approvals: 2 },
    ],
    accountId: "paper-main",
    setAccountId: () => {},
    token: null,
    setToken: () => {},
    events: { orders: 0, approvals_pending: 2, approvals: 0, runs: 0, decisions: 0, triggers: 0, watchlist: "", paused: true },
    tick: 0,
    reloadAccounts: () => {},
    theme: "system",
    setTheme: () => {},
    ...over,
  };
}

describe("Shell", () => {
  const render = (over: Partial<AppState> = {}) =>
    html(
      <AppContext.Provider value={fakeState(over)}>
        <MemoryRouter>
          <Shell now={new Date("2026-10-23T13:41:00+09:00")}>
            <p>본문</p>
          </Shell>
        </MemoryRouter>
      </AppContext.Provider>,
    );
  it("실전 계좌를 고르면 셸에 data-env=real 표식이 붙는다", () => {
    expect(render({ accountId: "real-main" })).toContain('class="shell" data-env="real"');
    expect(render()).toContain('class="shell" data-env="paper"');
  });
  it("미승인·일시정지 배지와 조회 전용 배지, 장 상태를 보여 준다", () => {
    const out = render();
    expect(out).toContain("미승인 2");
    expect(out).toContain("일시정지");
    expect(out).toContain("조회 전용");
    expect(out).toContain("KR 정규장 · US 개장 전");
    expect(out).toContain("본문");
  });
  it("토큰이 있으면 제어 가능 배지", () => {
    expect(render({ token: "T" })).toContain("제어 가능");
  });
});
