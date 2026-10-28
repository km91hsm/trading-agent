import { describe, expect, it } from "vitest";
import { credPrefix, draftPayload, emptyDraft, removeConfirmWord, validateDraft, type AccountDraft } from "./accountsForm";
import type { Account } from "./types";

const acct = (id: string, env: "paper" | "real" = "paper"): Account => ({
  id,
  env,
  markets: ["KR"],
  mode: "off",
  yaml_mode: "off",
  stance_by_market: {},
  nav_by_market: {},
  pending_approvals: 0,
});
const d = (over: Partial<AccountDraft>): AccountDraft => ({ ...emptyDraft(), ...over });
const existing = [acct("paper-main"), acct("real-main", "real")];

describe("계좌 추가 폼", () => {
  it("올바른 id 는 통과", () => {
    expect(validateDraft(d({ id: "real-2" }), existing)).toBeNull();
  });

  it.each(["", "  ", "real main", "real_2", "-x", "x-", "실전"])("형식 오류: %s", (id) => {
    expect(validateDraft(d({ id }), existing)).not.toBeNull();
  });

  it("공용 키와 겹치는 id 거부", () => {
    expect(validateDraft(d({ id: "paper" }), existing)).toMatch("공용");
    expect(validateDraft(d({ id: "REAL" }), existing)).toMatch("공용");
  });

  it("대소문자만 다른 중복·키 접두사 중복 거부", () => {
    expect(validateDraft(d({ id: "PAPER-MAIN" }), existing)).toMatch("paper-main");
  });

  it("현물은 시장이 하나 이상, 파생은 시장 무관", () => {
    expect(validateDraft(d({ id: "x1", markets: [] }), existing)).toMatch("시장");
    expect(validateDraft(d({ id: "x1", asset: "fo", markets: [] }), existing)).toBeNull();
  });

  it("본문: 파생은 KR 고정, 시장은 KR·US 순서, id 앞뒤 공백 제거", () => {
    expect(draftPayload(d({ id: " fo-2 ", asset: "fo", markets: ["US"] }))).toEqual({
      id: "fo-2",
      env: "real",
      asset: "fo",
      markets: ["KR"],
    });
    expect(draftPayload(d({ id: "r2", markets: ["US", "KR"] })).markets).toEqual(["KR", "US"]);
  });

  it("키 접두사 안내", () => {
    expect(credPrefix("real-2")).toBe("KIS_REAL_2_");
  });

  it("삭제 확인 문자열은 실전만", () => {
    expect(removeConfirmWord(acct("real-main", "real"))).toBe("real-main");
    expect(removeConfirmWord(acct("paper-main"))).toBeNull();
  });
});
