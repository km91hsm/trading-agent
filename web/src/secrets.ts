// 연결 키(secrets/.env) 화면용 순수 함수 — 서버는 값을 돌려주지 않고 '설정됨' 여부만 준다.

export type SecretKey = { name: string; group: string; label: string; account: string | null; set: boolean };
export type SecretAccount = { id: string; env: string; uses: "전용" | "공용" | "없음"; prefix: string };
export type SecretStatus = { keys: SecretKey[]; accounts: SecretAccount[] };

export type SecretGroup = { group: string; keys: SecretKey[] };

/** 묶음 순서를 서버 목록 순서 그대로 지키며 묶는다(공용 → 알림 → 데이터 → 계좌 전용). */
export function groupSecrets(keys: SecretKey[]): SecretGroup[] {
  const out: SecretGroup[] = [];
  for (const k of keys) {
    const last = out[out.length - 1];
    if (last && last.group === k.group) last.keys.push(k);
    else out.push({ group: k.group, keys: [k] });
  }
  return out;
}

/** 입력값·지우기 표시 → 서버로 보낼 본문. 빈 입력은 '바꾸지 않음', 입력이 있으면 지우기보다 우선. */
export function secretsPayload(
  drafts: Record<string, string>,
  clears: Set<string>,
): { values: Record<string, string>; clear: string[] } {
  const values: Record<string, string> = {};
  for (const [k, v] of Object.entries(drafts)) {
    const t = v.trim();
    if (t) values[k] = t;
  }
  const clear = [...clears].filter((k) => !(k in values)).sort();
  return { values, clear };
}

/** 계좌가 지금 쓰는 키 한 줄 설명. */
export function usesText(a: SecretAccount): string {
  if (a.uses === "전용") return `전용 키 ${a.prefix}*`;
  if (a.uses === "공용") return `공용 키 ${a.prefix}* — 같은 명의 다른 계좌가 있으면 계좌 전용 키를 넣으세요`;
  return "앱키 없음 — 공용 또는 계좌 전용 키를 넣으세요";
}
