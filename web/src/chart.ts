// 차트 공용 헬퍼: SMA, 마커 스타일(토큰 팔레트), 세션 시각 포매터. 색은 CSS 변수에서 읽는다.
import type { Time } from "lightweight-charts";
import type { Marker } from "./types";

export type Palette = {
  ink: string;
  ink2: string;
  paper: string;
  panel: string;
  rule: string;
  up: string;
  dn: string;
  pending: string;
  ok: string;
};

/** tokens.css 라이트 값과 같다 (테스트·SSR·CSS 변수를 못 읽을 때의 폴백). */
export const DEFAULT_PALETTE: Palette = {
  ink: "#17181A",
  ink2: "#585D64",
  paper: "#F6F4EE",
  panel: "#FCFBF8",
  rule: "#DAD6CC",
  up: "#C8322B",
  dn: "#2860BF",
  pending: "#8A5A00",
  ok: "#1E6B41",
};

const VARS: Record<keyof Palette, string> = {
  ink: "--ink",
  ink2: "--ink-2",
  paper: "--paper",
  panel: "--panel",
  rule: "--rule",
  up: "--up",
  dn: "--dn",
  pending: "--pending",
  ok: "--ok",
};

export function chartPalette(el: Element | null): Palette {
  if (!el || typeof getComputedStyle !== "function") return { ...DEFAULT_PALETTE };
  const cs = getComputedStyle(el);
  const out = { ...DEFAULT_PALETTE };
  for (const k of Object.keys(VARS) as Array<keyof Palette>) {
    const v = cs.getPropertyValue(VARS[k]).trim();
    if (v) out[k] = v;
  }
  return out;
}

/** 단순이동평균: 앞 n−1 개는 값 없음(선이 n 번째 봉부터 시작). */
export function sma(closes: { time: Time; close: number }[], n: number): { time: Time; value: number }[] {
  const out: { time: Time; value: number }[] = [];
  let sum = 0;
  for (let i = 0; i < closes.length; i++) {
    sum += closes[i].close;
    if (i >= n) sum -= closes[i - n].close;
    if (i >= n - 1) out.push({ time: closes[i].time, value: sum / n });
  }
  return out;
}

export type MarkerStyle = {
  position: "aboveBar" | "belowBar";
  color: string;
  shape: "arrowUp" | "arrowDown" | "circle" | "square";
};

/** 주문: 먹색 화살표(매도·축소는 위), 결정: 청색 원, 트리거: 황색 사각형(위). */
export function markerStyle(m: Pick<Marker, "kind" | "side">, palette: Palette = DEFAULT_PALETTE): MarkerStyle {
  if (m.kind === "trigger") return { position: "aboveBar", color: palette.pending, shape: "square" };
  const above = m.side === "SELL" || m.side === "TRIM";
  if (m.kind === "order") {
    return { position: above ? "aboveBar" : "belowBar", color: palette.ink, shape: above ? "arrowDown" : "arrowUp" };
  }
  return { position: above ? "aboveBar" : "belowBar", color: palette.dn, shape: "circle" };
}

/** 확신을 고정 3단계 지름으로: <0.5 → 0(작게), <0.7 → 1, 이상 → 2. 없으면 1. */
export function markerSize(confidence: number | null | undefined): 0 | 1 | 2 {
  if (confidence === null || confidence === undefined || Number.isNaN(confidence)) return 1;
  if (confidence < 0.5) return 0;
  if (confidence < 0.7) return 1;
  return 2;
}

/** 분봉 epoch(초) 을 세션 타임존의 HH:MM 으로. 일봉 문자열은 그대로 (축·크로스헤어 포매터). */
export function formatSessionTime(t: Time | string | number, tz: string): string {
  if (typeof t !== "number") return String(t);
  return new Intl.DateTimeFormat("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false }).format(
    new Date(t * 1000),
  );
}

/** lightweight-charts 공통 옵션(배경·글자·격자·크로스헤어)을 팔레트로. */
export function chartOptions(p: Palette, height: number) {
  return {
    height,
    layout: { textColor: p.ink2, background: { color: p.panel }, fontFamily: "inherit" },
    grid: { vertLines: { color: p.rule }, horzLines: { color: p.rule } },
    crosshair: { vertLine: { color: p.ink2, labelBackgroundColor: p.ink }, horzLine: { color: p.ink2, labelBackgroundColor: p.ink } },
    rightPriceScale: { borderColor: p.rule },
    timeScale: { borderColor: p.rule },
  };
}
