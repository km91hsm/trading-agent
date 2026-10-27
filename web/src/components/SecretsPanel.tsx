// 연결 키(secrets/.env) 입력 — 값은 화면·서버 응답 어디에도 나오지 않고 '설정됨' 여부만 보인다.
// 토큰 + 이 PC 에서 직접 연 화면에서만 동작(서버가 강제). 바꾼 키는 데몬·앱 재시작 뒤 적용.
import { useEffect, useState } from "react";
import { postJson } from "../api";
import { groupSecrets, secretsPayload, usesText, type SecretStatus } from "../secrets";
import { Badge } from "./Badge";
import { Section } from "./Section";
import { ErrorBox } from "./Tables";
import { Table, Td, Th } from "./Table";
import { useToast } from "./Toast";

export function SecretsPanel({ token }: { token: string | null }) {
  const toast = useToast();
  const [status, setStatus] = useState<SecretStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [clears, setClears] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [restart, setRestart] = useState(false);

  const load = async () => {
    if (!token) return;
    try {
      setStatus(await postJson<SecretStatus>("/api/control/secrets/status", null, token));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  useEffect(() => {
    void load();
  }, [token]);

  const payload = secretsPayload(drafts, clears);
  const dirty = Object.keys(payload.values).length + payload.clear.length > 0;
  const save = async () => {
    setBusy(true);
    try {
      const r = await postJson<{ updated: string[]; cleared: string[]; backup: string | null; env_overridden: string[] }>(
        "/api/control/secrets",
        payload,
        token,
      );
      toast(`저장했어요: 설정 ${r.updated.length} · 지움 ${r.cleared.length}${r.backup ? ` (백업 ${r.backup})` : ""}`, "ok");
      if (r.env_overridden.length) toast(`환경변수가 우선이라 적용되지 않는 키: ${r.env_overridden.join(", ")}`, "error");
      setDrafts({});
      setClears(new Set());
      setRestart(true);
      await load();
    } catch (e) {
      toast(`실패: ${(e as Error).message}`, "error");
    } finally {
      setBusy(false);
    }
  };
  const toggleClear = (name: string) =>
    setClears((s) => {
      const n = new Set(s);
      if (n.has(name)) n.delete(name);
      else n.add(name);
      return n;
    });

  return (
    <Section
      title="연결 키"
      aside={restart ? <Badge kind="pending" value="재시작해야 적용" /> : "secrets/.env · 값은 표시하지 않음"}
    >
      <p className="note">
        한국투자증권 앱키·계좌번호·HTS ID 와 알림·데이터 키를 여기서 넣는다. 저장한 값은 다시 보이지 않고 '설정됨'만 표시된다. 이 PC 에서 직접 연 화면에서만 저장되고(폰·원격 접속은
        거부), 바꾸기 전 파일은 백업된다. 실행 중인 데몬·앱은 시작할 때 읽은 키를 쓰므로 저장 뒤 다시 시작해야 적용된다.
      </p>
      <p className="note">
        같은 명의 계좌가 여럿이면 계좌마다 앱키를 따로 발급받아 '계좌 전용' 칸에 넣는다(한국투자증권은 앱키를 계좌별로 발급). HTS ID 는 명의당 하나라 공용 칸 하나면 그 명의 모든 계좌의
        체결통보를 받는다.
      </p>
      {!token && <p className="note">제어 토큰이 없어 열 수 없다 — 위 '제어 토큰'을 먼저 저장하세요.</p>}
      <ErrorBox error={error} />
      {status && (
        <>
          <h3 className="sub-h">계좌별 사용 키</h3>
          <Table dense caption="계좌별 사용 키">
            <thead>
              <tr>
                <Th>계좌</Th>
                <Th>쓰는 키</Th>
              </tr>
            </thead>
            <tbody>
              {status.accounts.map((a) => (
                <tr key={a.id}>
                  <Td>
                    <Badge kind="env" value={a.env} /> {a.id}
                  </Td>
                  <Td>{usesText(a)}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
          {groupSecrets(status.keys).map((g) => (
            <div key={g.group}>
              <h3 className="sub-h">{g.group}</h3>
              <Table dense caption={g.group}>
                <thead>
                  <tr>
                    <Th>항목</Th>
                    <Th>상태</Th>
                    <Th>새 값</Th>
                    <Th>지우기</Th>
                  </tr>
                </thead>
                <tbody>
                  {g.keys.map((k) => (
                    <tr key={k.name}>
                      <Td>
                        {k.label}
                        <div className="note">
                          <code>{k.name}</code>
                        </div>
                      </Td>
                      <Td>
                        {clears.has(k.name) ? <Badge kind="danger" value="지울 예정" /> : k.set ? <Badge kind="ok" value="설정됨" /> : <Badge kind="neutral" value="없음" />}
                        {k.env && <Badge kind="pending" value="환경변수 우선" title="이 키는 환경변수가 .env 값을 덮어씁니다 — 여기서 바꿔도 환경변수를 지우기 전까지 적용되지 않습니다" />}
                      </Td>
                      <Td>
                        <input
                          type="password"
                          autoComplete="off"
                          spellCheck={false}
                          aria-label={`${k.label} 새 값`}
                          placeholder={k.set ? "바꿀 때만 입력" : "입력"}
                          value={drafts[k.name] ?? ""}
                          onChange={(e) => setDrafts((d) => ({ ...d, [k.name]: e.target.value }))}
                          size={22}
                        />
                      </Td>
                      <Td>
                        <input
                          type="checkbox"
                          aria-label={`${k.label} 지우기`}
                          disabled={!k.set}
                          checked={clears.has(k.name)}
                          onChange={() => toggleClear(k.name)}
                        />
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </div>
          ))}
          <div className="row">
            <button type="button" className="btn btn-primary" disabled={!token || !dirty || busy} onClick={() => void save()}>
              저장
            </button>
            <button
              type="button"
              className="btn"
              disabled={!dirty || busy}
              onClick={() => {
                setDrafts({});
                setClears(new Set());
              }}
            >
              되돌리기
            </button>
          </div>
        </>
      )}
    </Section>
  );
}
