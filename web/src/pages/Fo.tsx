import { useEffect, useState } from "react";
import { getJson } from "../api";
import { Badge } from "../components/Badge";
import { EmptyState } from "../components/EmptyState";
import { Section } from "../components/Section";
import { Skeleton } from "../components/Skeleton";
import { Table, Td, Th } from "../components/Table";
import { ErrorBox } from "../components/Tables";
import { Term } from "../components/Term";
import { decisionLine, intentLabel, legRows, positionRows } from "../fo";
import { fmtMoney, fmtPct } from "../format";
import { useApp, useLoad } from "../hooks";
import type { FoLeg, FoPosition, FoReport, FoToday } from "../types";

/** 파생 레인(Phase 26): 오늘 결정·계좌 상태 → 포지션 → 주문 다리 → 재평가·NAV. 읽기 전용. */
export function FoPage() {
  const { foAccounts, tickOf } = useApp();
  const [acct, setAcct] = useState<string>("");
  useEffect(() => {
    if (!acct && foAccounts.length) setAcct(foAccounts[0].id);
  }, [acct, foAccounts]);
  const t = tickOf("fo", "control");
  const today = useLoad(() => getJson<FoToday>("/api/fo/today"), [t]);
  const pos = useLoad(() => (acct ? getJson<FoPosition[]>("/api/fo/positions", { account_id: acct }) : null), [acct, t]);
  const legs = useLoad(() => (acct ? getJson<FoLeg[]>("/api/fo/legs", { account_id: acct }) : null), [acct, t]);
  const report = useLoad(() => (acct ? getJson<FoReport>("/api/fo/report", { account_id: acct, days: "30" }) : null), [acct, t]);

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
  const st = d?.accounts.find((a) => a.id === acct);
  const rows = positionRows(pos.data ?? []);
  const lrows = legRows(legs.data ?? []);
  const checks = Object.entries(d?.checks ?? {}).filter(([k]) => k.endsWith(acct) || k.includes(`_${acct}_`));
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
                  {a.id}
                </option>
              ))}
            </select>
          </label>
        </p>
      ) : null}
      <Section
        title="오늘 결정"
        aside={
          <>
            {acct} {st?.halted ? <Badge kind="pending" value="일일 손실 한도 — 신규 금지" /> : null}{" "}
            {st?.pending_entry ? <Badge kind="pending" value="진입 창 대기" /> : null}
          </>
        }
      >
        <ErrorBox error={today.error} />
        {today.loading && !d ? (
          <Skeleton rows={2} cols={3} />
        ) : (
          <>
            <p>{decisionLine(d?.decision ?? null)}</p>
            {d?.decision?.rationale ? <p className="why-box">{d.decision.rationale}</p> : null}
            {checks.length ? (
              <Table dense caption="계좌 판정">
                <thead>
                  <tr>
                    <Th>단계</Th>
                    <Th>판정</Th>
                    <Th>사유·메모</Th>
                  </tr>
                </thead>
                <tbody>
                  {checks.map(([k, c]) => (
                    <tr key={k}>
                      <Td>{k.startsWith("_fo_order_") ? "주문" : "게이트"}</Td>
                      <Td>{c.error ? <Badge kind="danger" value="오류" /> : c.approved === false ? <Badge kind="danger" value="거부" /> : c.record_only ? <Badge kind="neutral" value="기록만" /> : <Badge kind="ok" value={c.status ?? "통과"} />}</Td>
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
                <Th num>
                  <Term k="fo.leverage" />
                </Th>
                <Th>손절·<Term k="fo.premium_stop">보호선</Term></Th>
                <Th num>
                  <Term k="fo.lifetime_budget">예산 사용/한도</Term>
                </Th>
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
                  <Td num>{r.leverage}</Td>
                  <Td>{r.stop}</Td>
                  <Td num>{r.budget}</Td>
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
                    <Td core>{l.live ? <Badge kind="pending" value={l.status} /> : l.status}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <EmptyState title="오늘 주문 다리가 없어요" />
          )}
        </Section>
        <Section title="재평가·순자산" aside="30일">
          {d?.reevals.length ? (
            <ul>
              {d.reevals.map((r) => (
                <li key={r.id}>
                  {r.round}차 {intentLabel(r.intent)} — {decisionLine(r)}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">오늘 재평가 없음</p>
          )}
          {report.data?.nav.length ? (
            <p>
              <Term k="nav" /> {fmtMoney(report.data.nav[report.data.nav.length - 1].nav, "KR")} · 30일 {report.data.return === null ? "—" : fmtPct(report.data.return)}
            </p>
          ) : (
            <p className="muted">순자산 기록 없음(모니터가 장중 매 분 남겨요)</p>
          )}
        </Section>
      </div>
    </>
  );
}
