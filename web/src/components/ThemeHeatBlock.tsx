// 테마 열기 블록 (Phase 25): 오늘 상위 테마 — 감지만, 매매는 분석 세션 → 게이트.
import { heatRows, type ThemeResp } from "../themeHeat";
import { Section } from "./Section";
import { Table, Td, Th } from "./Table";

export function ThemeHeatBlock({ data, market }: { data: ThemeResp | null; market: string }) {
  if (!data || !data.enabled) return null;
  const rows = heatRows(data);
  return (
    <Section title={`테마 열기 · ${market}`} aside={data.date ? `${data.date} · 상위 ${data.top_n}개만 scout·게이트에 쓰여요` : "장중 30분마다"}>
      {rows.length === 0 ? (
        <p className="muted">아직 판정이 없어요. 데몬이 장중 30분마다 새 뉴스·거래량·수급·세이브티커 순위로 계산해요.</p>
      ) : (
        <Table dense responsive caption="테마 열기 점수">
          <thead>
            <tr>
              <Th num core>순위</Th>
              <Th core>테마</Th>
              <Th num core>점수</Th>
              <Th>근거</Th>
              <Th>촉매 뉴스</Th>
              <Th>대표 종목</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key} className={r.top ? "" : "muted"}>
                <Td num core>
                  {r.rank}
                </Td>
                <Td core>{r.top ? <strong>{r.name}</strong> : r.name}</Td>
                <Td num core>
                  {r.score}
                </Td>
                <Td>{r.drivers}</Td>
                <Td className={r.catalyst === "있음" ? "" : "pend"}>{r.catalyst}</Td>
                <Td>{r.tickers || "—"}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      <p className="note">
        점수는 새 뉴스 증가·거래량/등락률 순위·외인·기관 순매수·세이브티커 순위 상승·거래량 급증을 합친 값이에요. 상위 테마의 촉매 있는 대표 종목은 scout 후보가 될 수 있고, 테마
        종목 매수는 게이트가 당일 +10% 추격 금지·테마 합계 20%·보유 10일 이하·익절가 필수로 막아요.
      </p>
    </Section>
  );
}
