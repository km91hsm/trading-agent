// 카드별 기준 시각과 '오래됨' 표시(Stock D-096): 적재 잡이 멈춰도 화면은 멀쩡해 보인다 — 값마다
// 무엇을 언제 기준으로 보여 주는지 적고, 기준보다 오래되면 황색(대기·주의 토큰) 꼬리표를 단다.

/** 'YYYY-MM-DD' 또는 ISO 시각 → {text, stale}. maxAgeMs 를 넘으면 stale. 값이 없으면 null. */
export function asOfInfo(value: string | null | undefined, now: Date, maxAgeMs: number, label = "기준"): { text: string; stale: boolean } | null {
  if (!value) return null;
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(value);
  // 날짜만이면 그날 끝(한국 시각 23:59)까지는 그날 값으로 본다
  const at = dateOnly ? new Date(`${value}T23:59:59+09:00`) : new Date(value);
  if (Number.isNaN(at.getTime())) return null;
  const stale = now.getTime() - at.getTime() > maxAgeMs;
  const text = dateOnly ? `${label} ${value}` : `${label} ${value.slice(0, 16).replace("T", " ")}`;
  return { text, stale };
}

export const DAY_MS = 24 * 60 * 60 * 1000;
