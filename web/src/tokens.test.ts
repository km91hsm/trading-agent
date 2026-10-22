import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// tokens.css 의 라이트·다크 두 테마에서 의미색이 바탕 위에서 WCAG AA(4.5:1) 를 넘는지 고정한다.
const css = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "styles/tokens.css"), "utf-8");

function block(selector: string): Record<string, string> {
  const start = css.indexOf(selector);
  if (start < 0) throw new Error(`블록 없음: ${selector}`);
  const open = css.indexOf("{", start);
  const close = css.indexOf("}", open);
  const vars: Record<string, string> = {};
  for (const line of css.slice(open + 1, close).split(";")) {
    const m = line.trim().match(/^(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})/);
    if (m) vars[m[1]] = m[2];
  }
  return vars;
}

function lum(hex: string): number {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  const lin = c.map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
}

function contrast(a: string, b: string): number {
  const [l1, l2] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (l1 + 0.05) / (l2 + 0.05);
}

const PAIRS: Array<[string, string, number]> = [
  ["--ink", "--paper", 7],
  ["--ink-2", "--paper", 4.5],
  ["--up", "--paper", 4.5],
  ["--dn", "--paper", 4.5],
  ["--pending", "--paper", 4.5],
  ["--real", "--paper", 4.5],
  ["--ok", "--paper", 4.5],
  ["--ink", "--panel", 7],
  ["--ink", "--sel", 7],
];

describe.each([
  ["light", ":root"],
  ["dark", '[data-theme="dark"]'],
])("%s 테마 대비", (_name, selector) => {
  const vars = block(selector);
  it.each(PAIRS)("%s / %s ≥ %s", (fg, bg, min) => {
    expect(vars[fg], fg).toBeDefined();
    expect(vars[bg], bg).toBeDefined();
    expect(contrast(vars[fg], vars[bg])).toBeGreaterThanOrEqual(min);
  });
});

it("두 테마가 같은 토큰 이름을 정의한다", () => {
  const a = Object.keys(block(":root")).sort();
  const b = Object.keys(block('[data-theme="dark"]')).sort();
  expect(b).toEqual(a);
});
