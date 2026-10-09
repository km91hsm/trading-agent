import { afterEach, describe, expect, it, vi } from "vitest";
import { AuthError, getJson, postJson, tokenStore, withAccount } from "./api";

describe("withAccount", () => {
  it("account_id 쿼리를 붙인다", () => {
    expect(withAccount("/api/orders", "paper-main")).toBe("/api/orders?account_id=paper-main");
    expect(withAccount("/api/portfolio?market=KR", "paper-main")).toBe(
      "/api/portfolio?market=KR&account_id=paper-main",
    );
  });
  it("빈 계좌 id 는 거부한다 (모의·실전 뷰가 섞이지 않게)", () => {
    expect(() => withAccount("/api/orders", "")).toThrow();
  });
});

describe("fetch helpers", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("postJson 은 401 에 AuthError 를 던지고 Bearer 헤더를 붙인다", async () => {
    const calls: Array<[string, RequestInit | undefined]> = [];
    vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
      calls.push([url, init]);
      return new Response(JSON.stringify({ detail: "토큰이 필요합니다" }), { status: 401 });
    });
    await expect(postJson("/api/control/pause", null, "T0KEN")).rejects.toBeInstanceOf(AuthError);
    expect((calls[0][1]?.headers as Record<string, string>)["Authorization"]).toBe("Bearer T0KEN");
  });

  it("getJson 은 4xx 를 detail 메시지로 던진다", async () => {
    vi.stubGlobal("fetch", async () =>
      new Response(JSON.stringify({ detail: "account_id 가 필요합니다" }), { status: 400 }),
    );
    await expect(getJson("/api/orders")).rejects.toThrow("account_id 가 필요합니다");
  });
});

describe("tokenStore", () => {
  it("localStorage 에 저장·조회한다", () => {
    tokenStore.set("abc");
    expect(tokenStore.get()).toBe("abc");
    tokenStore.set("");
    expect(tokenStore.get()).toBeNull();
  });
});
