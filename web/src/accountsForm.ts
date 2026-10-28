// 설정 화면 '계좌' 섹션의 추가 폼 검증·요청 본문(순수 함수). 규칙의 정본은 서버(`accounts_io`)이고
// 여기는 보내기 전에 같은 규칙으로 먼저 알려 주는 사본이다.
import type { Account, Market } from "./types";

export type AccountDraft = {
  id: string;
  env: "paper" | "real";
  asset: "stock" | "fo";
  markets: Market[];
};

export const emptyDraft = (): AccountDraft => ({ id: "", env: "real", asset: "stock", markets: ["KR"] });

const ID_RE = /^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$/;
const RESERVED = new Set(["KIS_PAPER_", "KIS_REAL_"]);

/** 계좌 전용 키 접두사(서버 `account_prefix` 와 같은 규칙): real-2 → KIS_REAL_2_ */
export function credPrefix(id: string): string {
  return "KIS_" + id.toUpperCase().replace(/[^A-Z0-9]+/g, "_").replace(/^_+|_+$/g, "") + "_";
}

/** 문제가 있으면 사용자 문구, 없으면 null. */
export function validateDraft(d: AccountDraft, existing: Account[]): string | null {
  const id = d.id.trim();
  if (!id) return "계좌 id 를 입력하세요";
  if (!ID_RE.test(id)) return "계좌 id 는 영문·숫자와 '-' 만 쓸 수 있어요(앞뒤 '-' 금지)";
  const prefix = credPrefix(id);
  if (RESERVED.has(prefix)) return "paper·real 은 공용 키 이름과 겹쳐 쓸 수 없어요";
  const dup = existing.find((a) => a.id.toLowerCase() === id.toLowerCase() || credPrefix(a.id) === prefix);
  if (dup) return `이미 있는 계좌와 겹쳐요: ${dup.id}`;
  if (d.asset === "stock" && d.markets.length === 0) return "시장을 하나 이상 고르세요";
  return null;
}

/** 서버로 보낼 본문. 파생은 [KR] 고정, 시장은 KR·US 순서. */
export function draftPayload(d: AccountDraft): AccountDraft {
  const markets: Market[] = d.asset === "fo" ? ["KR"] : (["KR", "US"] as Market[]).filter((m) => d.markets.includes(m));
  return { id: d.id.trim(), env: d.env, asset: d.asset, markets };
}

/** 삭제 확인에 입력해야 하는 문자열 — 실전 계좌만 계좌 id 를 그대로 친다(모의는 없음). */
export function removeConfirmWord(a: Pick<Account, "id" | "env">): string | null {
  return a.env === "real" ? a.id : null;
}
