import { useEffect, useRef, useState } from "react";
import { LineSeries, createChart, type IChartApi, type Time } from "lightweight-charts";
import { getJson } from "../api";
import { chartOptions, chartPalette } from "../chart";
import { Badge } from "../components/Badge";
import { EmptyState } from "../components/EmptyState";
import { Section } from "../components/Section";
import { Skeleton } from "../components/Skeleton";
import { Table, Td, Th } from "../components/Table";
import { ErrorBox } from "../components/Tables";
import { Term } from "../components/Term";
import {
  STATE_HINT,
  checkDetail,
  contextRows,
  decisionDetail,
  decisionLine,
  intentLabel,
  legRows,
  modeAccountLabel,
  positionRows,
  stateBadge,
  verifiedText,
} from "../fo";
import { fmtMoney, fmtPct, fmtTime } from "../format";
import { useApp, useLoad } from "../hooks";
import type { FoAccountView, FoLeg, FoPosition, FoReport, FoScorecard, FoToday } from "../types";

const TRIGGER: Record<string, string> = {
  invalidation: "무효화 조건",
  move: "급등락",
  take_profit: "익절 근접",
  news: "새 뉴스",
  iv_spike: "변동성 급등",
  periodic: "정기 점검",
  expiry: "만기 의견",
};

const money = (v: string | null | undefined) => (v === null || v === undefined || v === "" ? "—" : fmtMoney(v, "KR"));

/** 파생 레인(Phase 26): 계좌 상태 → 시장·한도 → 오늘 결정 → 포지션 → 주문 다리 → 재평가·트리거 → 순자산·적중률. 읽기 전용. */
export function FoPage() {
  const { foAccounts, tickOf, theme } = useApp();
  const [acct, setAcct] = useState<string>("");
  useEffect(() => {
    if (!acct && foAccounts.length) setAcct(foAccounts[0].id);
  }, [acct, foAccounts]);
  const t = tickOf("fo", "control");
  const today = useLoad(() => getJson<FoToday>("/api/fo/today"), [t]);
  const view = useLoad(() => (acct ? getJson<FoAccountView>("/api/fo/account", { account_id: acct }) : null), [acct, t]);
  const pos = useLoad(() => (acct ? getJson<FoPosition[]>("/api/fo/positions", { account_id: acct }) : null), [acct, t]);
  const legs = useLoad(() => (acct ? getJson<FoLeg[]>("/api/fo/legs", { account_id: acct }) : null), [acct, t]);
  const report = useLoad(() => (acct ? getJson<FoReport>("/api/fo/report", { account_id: acct, days: "90" }) : null), [acct, t]);
  const score = useLoad(() => getJson<FoScorecard>("/api/fo/scorecard"), [t]);
  const box = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    const nav = report.data?.nav ?? [];
    if (!box.current || nav.length < 2) {
      chartRef.current?.remove();
      chartRef.current = null;
      return;
    }
    const p = chartPalette(box.current);
    const chart = createChart(box.current, chartOptions(p, 200));
    chartRef.current = chart;
    const s = chart.addSeries(LineSeries, { color: p.ink, title: "순자산", lineWidth: 2 });
    s.setData(nav.map((x) => ({ time: x.date as Time, value: parseFloat(x.nav) })));
    chart.timeScale().fitContent();
    return () => {
      chart.remove();
      chartRef.current = null;
    };
  }, [report.data, theme]);

  if (!foAccounts.length) {
    return (
      <>
        <header className="page-h">
          <h1>파생</h1>
        </header>
        <EmptyState title="파생 계좌가 없어요" hint="config/accounts.yaml 에 asset: fo 계좌(모의 선물옵션, 상품코드 03)를 더하면 이 화면이 채워져요." />
      </>
    );
  }
  const d = today.data;
  const v = view.data;
  const st = v?.status;
  const badge = st ? stateBadge(st.state) : null;
  const rows = positionRows(pos.data ?? []);
  const lrows = legRows(legs.data ?? []);
  const checks = Object.entries(d?.checks ?? {}).filter(([k]) => k.endsWith(acct) || k.includes(`_${acct}_`));
  const reevals = (d?.reevals ?? []).filter((r) => r.account_id === acct);
  const detail = decisionDetail(d?.decision ?? null);
  return (
    <>
      <header className="page-h">
        <h1>파생</h1>
        <p className="sub">코스피200 선물·옵션 방향성 레인(모의). 결정은 하루 한 번 + 장중 재평가, 판정·주문은 파이썬 게이트가 한다.</p>
      </header>
      {foAccounts.length > 1 ? (
        <p>
          <label>
            계좌{" "}
            <select value={acct} onChange={(e) => setAcct(e.target.value)}>
              {foAccounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {modeAccountLabel(a)}
                </option>
              ))}
            </select>
          </label>
        </p>
      ) : null}

      <div className="cols-2">
        <Section
          title="계좌 상태"
          aside={
            st && badge ? (
              <>
                {st.id} <Badge kind="env" value={st.env} /> <Badge kind="mode" value={st.mode} /> <Badge kind={badge.kind} value={badge.label} title={STATE_HINT[st.state]} />
              </>
            ) : null
          }
        >
          <ErrorBox error={view.error} />
          {view.loading && !v ? (
            <Skeleton rows={4} cols={2} />
          ) : st ? (
            <>
              <p className={st.state === "active" ? undefined : "note"}>{STATE_HINT[st.state]}</p>
              <Table dense caption="파생 계좌 잔고·검증">
                <tbody>
                  <tr>
                    <Th>
                      <Term k="nav" />
                    </Th>
                    <Td num>{money(st.nav)}</Td>
                  </tr>
                  <tr>
                    <Th>주문가능현금</Th>
                    <Td num>{money(st.cash)}</Td>
                  </tr>
                  <tr>
                    <Th>증거금(사용률)</Th>
                    <Td num>
                      {money(st.margin_total)}
                      {st.margin_pct !== null ? ` (${fmtPct(st.margin_pct, 0)})` : ""}
                    </Td>
                  </tr>
                  <tr>
                    <Th>추가증거금</Th>
                    <Td num className={Number(st.add_margin) > 0 ? "gate no" : undefined}>
                      {money(st.add_margin)}
                    </Td>
                  </tr>
                  <tr>
                    <Th>실기 검증</Th>
                    <Td>{verifiedText(st)}</Td>
                  </tr>
                  <tr>
                    <Th>오늘 상태</Th>
                    <Td>
                      {st.halted ? <Badge kind="danger" value="일일 손실 한도 — 신규 금지" /> : null} {st.pending_entry ? <Badge kind="pending" value="진입 창 대기" /> : null}{" "}
                      {st.paused ? <Badge kind="pending" value="일시정지" /> : null}
                      {!st.halted && !st.pending_entry && !st.paused ? <span className="muted">특이 없음</span> : null}
                    </Td>
                  </tr>
                </tbody>
              </Table>
              <p className="muted">잔고 기준 {st.nav_date ?? "—"} · 잔고는 장중 매 분, 장 밖은 30분마다 갱신</p>
            </>
          ) : null}
        </Section>

        <Section title="시장·한도" aside={v?.context.source === "inputs" ? "오늘 세션 입력 기준" : "설정값(오늘 세션 전)"}>
          {v ? (
            <Table dense caption="파생 시장·한도">
              <tbody>
                {contextRows(v.context).map(([k, val]) => (
                  <tr key={k}>
                    <Th>{k}</Th>
                    <Td>{val}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <Skeleton rows={4} cols={2} />
          )}
        </Section>
      </div>

      <Section
        title="오늘 결정"
        aside={
          <>
            {acct} {st?.halted ? <Badge kind="pending" value="일일 손실 한도 — 신규 금지" /> : null} {st?.pending_entry ? <Badge kind="pending" value="진입 창 대기" /> : null}
          </>
        }
      >
        <ErrorBox error={today.error} />
        {today.loading && !d ? (
          <Skeleton rows={2} cols={3} />
        ) : (
          <>
            <p>{decisionLine(d?.decision ?? null)}</p>
            {detail.length ? <p className="muted">{detail.join(" · ")}</p> : null}
            {d?.decision?.rationale ? <p className="why-box">{d.decision.rationale}</p> : null}
            {checks.length ? (
              <Table dense caption="계좌 판정">
                <thead>
                  <tr>
                    <Th>단계</Th>
                    <Th>판정</Th>
                    <Th>게이트 상세</Th>
                    <Th>사유·메모</Th>
                  </tr>
                </thead>
                <tbody>
                  {checks.map(([k, c]) => (
                    <tr key={k}>
                      <Td>{k.startsWith("_fo_order_") ? "주문" : "게이트"}</Td>
                      <Td>{c.error ? <Badge kind="danger" value="오류" /> : c.approved === false ? <Badge kind="danger" value="거부" /> : c.record_only ? <Badge kind="neutral" value="기록만" /> : <Badge kind="ok" value={c.status ?? "통과"} />}</Td>
                      <Td>{checkDetail(c).join(" · ") || "—"}</Td>
                      <Td>{c.error ?? ([...(c.reasons ?? []), c.note].filter(Boolean).join(" · ") || "—")}</Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            ) : null}
          </>
        )}
      </Section>

      <Section title="포지션" aside={`${rows.length}건`}>
        <ErrorBox error={pos.error} />
        {pos.loading && !pos.data ? (
          <Skeleton rows={2} cols={6} />
        ) : rows.length ? (
          <Table dense responsive caption="파생 포지션">
            <thead>
              <tr>
                <Th core>계약</Th>
                <Th core>
                  <Term k="fo.direction" /> · <Term k="fo.vehicle" />
                </Th>
                <Th core>
                  <Term k="fo.contract_side" />
                </Th>
                <Th num>평단</Th>
                <Th num core>
                  현재가
                </Th>
                <Th num core>
                  평가손익
                </Th>
                <Th num>
                  <Term k="fo.leverage" />
                </Th>
                <Th>손절·<Term k="fo.premium_stop">보호선</Term></Th>
                <Th num>선물 손절가</Th>
                <Th>익절</Th>
                <Th>만기</Th>
                <Th num>
                  <Term k="fo.lifetime_budget">예산 사용+계획/한도</Term>
                </Th>
                <Th>진입일 · 보유 기한</Th>
                <Th core>상태</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <Td core>{r.code}</Td>
                  <Td core>
                    <span className={r.dir === "flat" ? undefined : r.dir}>{r.what}</span>
                  </Td>
                  <Td core>{r.side}</Td>
                  <Td num>{r.avg}</Td>
                  <Td num core>
                    {r.current}
                  </Td>
                  <Td num core>
                    <span className={r.pnlDir === "flat" ? undefined : r.pnlDir}>{r.pnl}</span>
                  </Td>
                  <Td num>{r.leverage}</Td>
                  <Td>{r.stop}</Td>
                  <Td num>{r.stopFut}</Td>
                  <Td>{r.tp}</Td>
                  <Td>{r.daysLeft}</Td>
                  <Td num>{r.budget}</Td>
                  <Td>
                    {r.opened} · {r.horizon}
                  </Td>
                  <Td core>{r.attention ? <Badge kind="pending" value={r.status} /> : r.status}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : (
          <EmptyState title="보유 포지션이 없어요" hint="아침 파생 세션(08:20)이 진입을 결정하면 09:00 진입 창에서 다시 계산해 주문해요." />
        )}
      </Section>

      <div className="cols-2">
        <Section title="주문 다리" aside="오늘 + 살아 있는 다리">
          <ErrorBox error={legs.error} />
          {lrows.length ? (
            <Table dense responsive caption="파생 주문 다리">
              <thead>
                <tr>
                  <Th core>계약</Th>
                  <Th core>의도</Th>
                  <Th num>체결</Th>
                  <Th>가격</Th>
                  <Th>주문번호</Th>
                  <Th>전송</Th>
                  <Th core>상태</Th>
                </tr>
              </thead>
              <tbody>
                {lrows.map((l) => (
                  <tr key={l.id}>
                    <Td core>{l.code}</Td>
                    <Td core>
                      {l.intent} {l.attempt}
                    </Td>
                    <Td num>{l.progress}</Td>
                    <Td>{l.price}</Td>
                    <Td>{l.orderNo}</Td>
                    <Td>{l.sentAt}</Td>
                    <Td core>{l.live ? <Badge kind="pending" value={l.status} /> : l.status}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <EmptyState title="오늘 주문 다리가 없어요" />
          )}
        </Section>

        <Section title="재평가·트리거" aside="오늘">
          {v?.triggers.length ? (
            <Table dense caption="오늘 파생 트리거">
              <thead>
                <tr>
                  <Th>시각</Th>
                  <Th>트리거</Th>
                  <Th>계약</Th>
                  <Th>처리</Th>
                </tr>
              </thead>
              <tbody>
                {v.triggers.map((x, i) => (
                  <tr key={`${x.at}-${i}`}>
                    <Td>{fmtTime(x.at, "KR")}</Td>
                    <Td>{TRIGGER[x.kind] ?? x.kind}</Td>
                    <Td>{x.code}</Td>
                    <Td>{x.handled ? (x.session && /^(실패|보류|폐기):/.test(x.session) ? x.session.split(":")[0] : "재평가함") : <Badge kind="pending" value="대기" />}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <p className="muted">오늘 트리거 없음</p>
          )}
          {reevals.length ? (
            <ul>
              {reevals.map((r) => (
                <li key={r.id}>
                  {r.round}차 {intentLabel(r.intent)} — {decisionLine(r)}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">오늘 재평가 결정 없음</p>
          )}
        </Section>
      </div>

      <Section title="순자산 추이·적중률" aside="90일">
        {report.data?.nav.length ? (
          <p>
            <Term k="nav" /> {money(report.data.nav[report.data.nav.length - 1].nav)} · 기간 수익 {report.data.return === null ? "—" : fmtPct(report.data.return)}
          </p>
        ) : (
          <p className="muted">순자산 기록 없음(잔고를 읽으면 남겨요)</p>
        )}
        <div ref={box} className={(report.data?.nav.length ?? 0) >= 2 ? "chart chart-sm" : "chart chart-hidden"} aria-label="파생 계좌 순자산 추이" />
        {(report.data?.nav.length ?? 0) < 2 ? <p className="muted">추이는 이틀 이상 기록이 쌓이면 그려져요.</p> : null}
        <p>
          결정 {score.data?.decisions ?? 0}건 · 만기 채점 {score.data?.matured ?? 0}건 · 방향 적중률 {score.data?.hit_rate === null || score.data?.hit_rate === undefined ? "—" : fmtPct(score.data.hit_rate, 0)} · 계약 손익 {money(score.data?.contract_pnl)}
        </p>
      </Section>
    </>
  );
}
