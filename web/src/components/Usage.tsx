// Claude 사용량 블록: 구독 창(5시간·7일)의 마지막 관측값 막대 + 이 저장소 세션 집계.
import { Table, Td, Th } from "./Table";
import { agoText, barPct, observationStale, resetsIn, sourceText, throttleText, usageLevel, type LimitWindow, type UsageResp } from "../usage";

function Meter({ label, w, now }: { label: string; w: LimitWindow; now: Date }) {
  const pct = w?.used_pct ?? null;
  const level = usageLevel(pct);
  const reset = resetsIn(w?.resets_at, now);
  return (
    <div className="meter" data-level={level}>
      <div className="meter-h">
        <span>{label}</span>
        <b>{pct === null ? "—" : `${Math.round(pct)}%`}</b>
      </div>
      <div className="meter-bar" role="progressbar" aria-label={`${label} 사용률`} aria-valuenow={pct ?? 0} aria-valuemin={0} aria-valuemax={100}>
        <span style={{ width: `${barPct(pct)}%` }} />
      </div>
      {reset && <small className="muted">{reset}</small>}
    </div>
  );
}

export function UsageBlock({ data, now }: { data: UsageResp | null; now: Date }) {
  const rl = data?.rate_limits ?? null;
  const w = data?.windows;
  const stale = observationStale(rl?.age_s);
  return (
    <div className="usage">
      {rl ? (
        <>
          <div className="meters">
            <Meter label="5시간 창" w={rl.five_hour} now={now} />
            <Meter label="7일 창" w={rl.seven_day} now={now} />
          </div>
          <p className={stale ? "note err" : "note"}>
            구독 사용률 · {sourceText(rl.source)} · {agoText(rl.age_s)}
            {stale ? " — 오래된 값이에요. 다음 분석 세션이 돌거나 터미널에서 claude 를 열면 갱신돼요." : ""}
          </p>
          {throttleText(data?.throttle) && (
            <p className="note pend" title={data?.throttle?.reason ?? undefined}>
              {throttleText(data?.throttle)}
            </p>
          )}
        </>
      ) : (
        <p className="note">구독 사용률 관측값이 아직 없어요. 분석 세션이 한 번 돌면 claude 가 알려 준 값이 쌓여요.</p>
      )}
      {w && (
        <Table dense caption="이 저장소의 세션">
          <thead>
            <tr>
              <Th>창</Th>
              <Th num>세션</Th>
              <Th num>비용</Th>
              <Th num>평균 소요</Th>
              <Th num>오류</Th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <Td>오늘</Td>
              <Td num>
                {w.today.sessions} / {w.today.cap}
              </Td>
              <Td num>—</Td>
              <Td num>—</Td>
              <Td num>—</Td>
            </tr>
            {(["h5", "d7"] as const).map((k) => (
              <tr key={k}>
                <Td>{k === "h5" ? "5시간" : "7일"}</Td>
                <Td num>{w[k].sessions}</Td>
                <Td num>${w[k].cost_usd.toFixed(2)}</Td>
                <Td num>{w[k].avg_duration_s === null ? "—" : `${Math.round(w[k].avg_duration_s)}초`}</Td>
                <Td num className={w[k].errors ? "dn" : ""}>
                  {w[k].errors}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      {data?.note && <p className="note">{data.note}</p>}
    </div>
  );
}
