// 포트폴리오 위험 카드(Stock D-088): VaR·CVaR·베타 + 위험 기여 표 + 가장 높은 상관 한 줄. 표시 전용.
import { topCorrelation, riskSummary, type RiskResp } from "../riskCard";
import { EmptyState } from "./EmptyState";
import { Table, Td, Th } from "./Table";
import { Ticker } from "./Ticker";

export function RiskCard({ data, market }: { data: RiskResp; market: string }) {
  if (data.insufficient)
    return (
      <EmptyState
        title="위험 지표를 아직 못 내요"
        hint={`${data.insufficient}${data.missing?.length ? ` · 종가 없는 종목 ${data.missing.join(", ")}` : ""} — 피처 스토어 종가가 20일 넘게 쌓이면 보여요.`}
      />
    );
  const summary = riskSummary(data);
  const top = topCorrelation(data);
  const missing = data.missing ?? [];
  return (
    <>
      {missing.length > 0 && (
        <p className="note" role="note">
          <span className="badge badge-pending">부분 추정</span> 종가 이력이 부족한 보유 종목 {missing.length}개(
          {missing.map((t, i) => (
            <span key={t}>
              {i ? ", " : ""}
              <Ticker market={market} ticker={t} />
            </span>
          ))}
          )를 빼고 계산했어요 — 실제 위험은 이보다 클 수 있어요.
        </p>
      )}
      <dl className="kv">
        {summary.map((s) => (
          <div key={s.label} className="kv-row">
            <dt>{s.label}</dt>
            <dd className="num-l">
              <strong>{s.value}</strong> <span className="muted">{s.note}</span>
            </dd>
          </div>
        ))}
      </dl>
      {top && (
        <p className="note">
          가장 비슷하게 움직이는 쌍: <Ticker market={market} ticker={top.a} /> · <Ticker market={market} ticker={top.b} /> (상관{" "}
          {top.rho.toFixed(2)}){top.rho >= 0.8 ? " — 사실상 한 종목처럼 움직여요" : ""}
        </p>
      )}
      <Table dense caption="위험 기여">
        <thead>
          <tr>
            <Th>종목</Th>
            <Th num>비중</Th>
            <Th num>위험 기여</Th>
          </tr>
        </thead>
        <tbody>
          {(data.contributions ?? []).map((c) => (
            <tr key={c.ticker}>
              <Td>
                <Ticker market={market} ticker={c.ticker} />
              </Td>
              <Td num>{(c.weight * 100).toFixed(1)}%</Td>
              <Td num>{(c.contribution * 100).toFixed(1)}%</Td>
            </tr>
          ))}
        </tbody>
      </Table>
      <p className="note">
        최근 {data.n_obs}거래일 수익에 지금 비중을 적용한 값이에요(기준 {data.asof}). 주문·게이트에는 쓰지 않아요.
      </p>
    </>
  );
}
