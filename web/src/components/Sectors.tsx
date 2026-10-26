// 섹터 비중 표 (Phase 17 S). 시세표 열 문법 + SVG 막대 하나(바탕 --rule, 채움 --ink-2, 상한선 --ink, 초과는 --pending).
import { fmtMoney } from "../format";
import { capLinePct, sectorRows, type SectorRow } from "../sectors";
import { EmptyState } from "./EmptyState";
import { Table, Td, Th } from "./Table";

function Bar({ pct, cap, over, exempt, label }: { pct: number; cap: number; over: boolean; exempt: boolean; label: string }) {
  const fill = exempt ? "var(--ink-3)" : over ? "var(--pending)" : "var(--ink-2)";
  return (
    <svg className="bar-svg" viewBox="0 0 100 8" preserveAspectRatio="none" role="img" aria-label={label}>
      <rect x="0" y="0" width="100" height="8" fill="var(--rule)" />
      <rect x="0" y="0" width={pct} height="8" fill={fill} />
      {!exempt && <rect x={cap - 0.4} y="0" width="0.8" height="8" fill="var(--ink)" />}
    </svg>
  );
}

export function SectorTable({ rows, market, maxSectorPct, note = true }: { rows: SectorRow[]; market: string; maxSectorPct: number; note?: boolean }) {
  if (!rows.length) return <EmptyState title="업종 비중이 없어요" hint="잔고 스냅샷이 찍히면 보유 종목을 업종(반도체·바이오/제약·산업재/건설 …)으로 묶어 보여 줘요." />;
  const views = sectorRows(rows, maxSectorPct);
  const cap = capLinePct(maxSectorPct);
  return (
    <>
      <Table dense caption="섹터 비중">
        <thead>
          <tr>
            <Th>업종</Th>
            <Th num>종목</Th>
            <Th num>평가금액</Th>
            <Th num>비중</Th>
            <Th>막대 (세로선 = 상한 {Math.round(maxSectorPct * 100)}%)</Th>
          </tr>
        </thead>
        <tbody>
          {views.map((v) => (
            <tr key={v.key} data-over={v.over || undefined}>
              <Td>
                <span className="nm">
                  {v.sector}
                  {v.auto ? <span className="auto-tag" title="KIS 업종명으로 자동 분류(표시용)">자동</span> : null}
                  {v.tickers.length ? <small>{v.tickers.join(" ")}</small> : null}
                </span>
              </Td>
              <Td num>{v.count || "—"}</Td>
              <Td num>{fmtMoney(v.eval_amount, market)}</Td>
              <Td num className={v.over ? "pend" : undefined}>
                {v.pct}
                {v.over ? <small className="muted"> 상한 초과</small> : null}
              </Td>
              <Td>
                <Bar pct={v.barPct} cap={cap} over={v.over} exempt={!v.capped} label={`${v.sector} 비중 ${v.pct}${v.capped ? `, 상한 ${Math.round(maxSectorPct * 100)}%` : ""}`} />
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
      {note && <p className="note">같은 업종 합계가 상한을 넘기면 리스크 게이트가 그 업종의 신규 매수·추가 매수를 거부해요. 헤지 ETF 와 현금은 상한 대상이 아니에요.</p>}
      {views.some((v) => v.auto) && (
        <p className="note">'자동' 은 KIS 업종명으로 분류한 표시용 업종이에요. 리스크 게이트의 업종 상한은 config/sectors.yaml 기준이라, 게이트에도 반영하려면 그 파일에 종목을 추가하세요.</p>
      )}
    </>
  );
}
