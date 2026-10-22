// 표 원시요소 — 시세표 열 문법: 이름 셀 + 기호, 우측정렬 tabular 숫자, ▲▼ 적청, 2px 머리선.
import type { ReactNode } from "react";
import { fmtMoney, fmtPct, round2 } from "../format";
import type { Market } from "../types";
import { Glyph } from "./Glyph";

export function Table({
  children,
  dense,
  className,
  caption,
}: {
  children: ReactNode;
  dense?: boolean;
  className?: string;
  caption?: string;
}) {
  const cls = ["tape", dense ? "dense" : "", className ?? ""].filter(Boolean).join(" ");
  return (
    <div className="table-wrap">
      <table className={cls}>
        {caption ? <caption className="sr-only">{caption}</caption> : null}
        {children}
      </table>
    </div>
  );
}

export function Th({
  children,
  num,
  onSort,
  sorted,
  colSpan,
}: {
  children?: ReactNode;
  num?: boolean;
  onSort?: () => void;
  sorted?: "asc" | "desc" | null;
  colSpan?: number;
}) {
  const inner = onSort ? (
    <button type="button" className="th-sort" onClick={onSort}>
      {children}
      <Glyph name={sorted === "asc" ? "chevron-up" : sorted === "desc" ? "chevron" : "sort"} size={12} />
    </button>
  ) : (
    children
  );
  const ariaSort = sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : undefined;
  return (
    <th scope="col" className={num ? "num" : undefined} colSpan={colSpan} aria-sort={ariaSort}>
      {inner}
    </th>
  );
}

export function Td({
  children,
  num,
  className,
  colSpan,
}: {
  children?: ReactNode;
  num?: boolean;
  className?: string;
  colSpan?: number;
}) {
  const cls = [num ? "num" : "", className ?? ""].filter(Boolean).join(" ") || undefined;
  return (
    <td className={cls} colSpan={colSpan}>
      {children}
    </td>
  );
}

export function Num({ value, market }: { value: string | number | null | undefined; market: Market | string }) {
  return <>{fmtMoney(value, market)}</>;
}

/** 변화량. pct 면 value 를 퍼센트 숫자(1.53)로 받아 "1.53%" 로, 아니면 그대로. */
export function Delta({ value, pct, digits = 2 }: { value: number | null | undefined; pct?: boolean; digits?: number }) {
  if (value === null || value === undefined || Number.isNaN(value)) return <span className="delta flat">—</span>;
  const dir = value > 0 ? "up" : value < 0 ? "dn" : "flat";
  const text = pct ? fmtPct(Math.abs(value) / 100, digits) : Math.abs(round2(value)).toLocaleString("en-US");
  return (
    <span className={`delta ${dir}`}>
      <Glyph name={dir === "up" ? "up" : dir === "dn" ? "down" : "flat"} size={10} title={dir === "up" ? "상승" : dir === "dn" ? "하락" : "변동 없음"} />
      {text}
    </span>
  );
}

const MARK_TITLE = { held: "보유", pick: "오늘 선정", watch: "관심종목" } as const;

export function Mark({ kind }: { kind: "held" | "pick" | "watch" | null | undefined }) {
  if (!kind) return <span className="mark mark-none" aria-hidden="true" />;
  return (
    <span className="mark" data-mark={kind}>
      <Glyph name={kind} size={12} title={MARK_TITLE[kind]} />
    </span>
  );
}
