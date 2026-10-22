// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import { applyTheme, resolveTheme, themeStore } from "./theme";

describe("resolveTheme", () => {
  it("system 은 OS 선호를 따른다", () => {
    expect(resolveTheme("system", true)).toBe("dark");
    expect(resolveTheme("system", false)).toBe("light");
  });
  it("명시 값은 OS 선호를 무시한다", () => {
    expect(resolveTheme("light", true)).toBe("light");
    expect(resolveTheme("dark", false)).toBe("dark");
  });
});

describe("themeStore", () => {
  beforeEach(() => localStorage.clear());
  it("기본은 system 이고 set/get 이 왕복한다", () => {
    expect(themeStore.get()).toBe("system");
    themeStore.set("dark");
    expect(themeStore.get()).toBe("dark");
    expect(localStorage.getItem("trader.theme")).toBe("dark");
  });
  it("모르는 값은 system 으로 읽는다", () => {
    localStorage.setItem("trader.theme", "neon");
    expect(themeStore.get()).toBe("system");
  });
});

describe("applyTheme", () => {
  it("<html data-theme> 에 해석된 값을 쓴다", () => {
    applyTheme(document, "dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    applyTheme(document, "light");
    expect(document.documentElement.dataset.theme).toBe("light");
  });
});
