// 표 원시요소 — 시세표 열 문법: 이름 셀 + 기호, 우측정렬 tabular 숫자, ▲▼ 적청, 2px 머리선.
import { useId, useState, type ReactNode } from "react";
import { fmtMoney, fmtPct, round2 } from "../format";
import type { Market } from "../types";
import { Glyph } from "./Glyph";

export function Table({
  children,
  dense,
  className,
  caption,
  responsive,
}: {
  children: ReactNode;
  dense?: boolean;
  className?: string;
  caption?: string;
  responsive?: boolean; // 좁은 화면에서 core 가 아닌 열을 숨긴다(가로 스크롤 대신, Phase 23 G)
}) {
  const cls = ["tape", dense ? "dense" : "", responsive ? "responsive" : "", className ?? ""].filter(Boolean).join(" ");
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
  core,
}: {
  children?: ReactNode;
  num?: boolean;
  onSort?: () => void;
  sorted?: "asc" | "desc" | null;
  colSpan?: number;
  core?: boolean; // responsive 표에서 좁은 화면에도 남는 열
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
  const cls = [num ? "num" : "", core ? "core" : ""].filter(Boolean).join(" ") || undefined;
  return (
    <th scope="col" className={cls} colSpan={colSpan} aria-sort={ariaSort}>
      {inner}
    </th>
  );
}

export function Td({
  children,
  num,
  className,
  colSpan,
  core,
}: {
  children?: ReactNode;
  num?: boolean;
  className?: string;
  colSpan?: number;
  core?: boolean;
}) {
  const cls = [num ? "num" : "", core ? "core" : "", className ?? ""].filter(Boolean).join(" ") || undefined;
  return (
    <td className={cls} colSpan={colSpan}>
      {children}
    </td>
  );
}

/** 행 선택·펼침 버튼(전체 검토 W10). `<tr onClick>` 만으로는 키보드로 열 수 없어 첫 셀 내용을 감싸
 * Enter/Space 로 같은 동작을 하게 한다. 상세를 펼치면 `expanded`, 다른 패널을 고르면 `pressed`.
 * 행 클릭(마우스)도 그대로 두므로 두 번 토글되지 않게 전파를 막는다. */
export function RowButton({
  children,
  onClick,
  expanded,
  pressed,
}: {
  children: ReactNode;
  onClick: () => void;
  expanded?: boolean;
  pressed?: boolean;
}) {
  return (
    <button
      type="button"
      className="row-btn"
      aria-expanded={expanded}
      aria-pressed={pressed}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
    >
      {children}
    </button>
  );
}

export type RowDetail = { label: ReactNode; value: ReactNode };

/** responsive 표의 펼침 열 머리(좁은 화면에서만 보인다). */
export function MoreTh() {
  return (
    <th scope="col" className="core row-more">
      <span className="sr-only">상세</span>
    </th>
  );
}

/** responsive 표의 한 행 + 좁은 화면 전용 상세 행(전체 검토 W9). 좁은 화면은 core 아닌 열을 숨기므로
 * 수량·가격·손절가를 볼 길이 없었다 → 끝 열의 '상세' 버튼으로 숨긴 값을 정의 목록으로 펼친다.
 * 넓은 화면에서는 버튼 열과 상세 행 모두 보이지 않는다(shell.css). `span` 은 보이는 열 수와 무관하게
 * 전체 열 수(+1)를 넘긴다 — 숨은 셀은 colSpan 계산에서 빠진다. */
export function DetailRow({
  children,
  details,
  span,
  label,
  rowProps,
}: {
  children: ReactNode;
  details: RowDetail[];
  span: number;
  label: string;
  rowProps?: Record<string, string | undefined>;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <>
      <tr {...rowProps}>
        {children}
        <td className="core row-more">
          <button type="button" className="btn btn-sm btn-ghost" aria-expanded={open} aria-controls={id} aria-label={`${label} 상세`} onClick={() => setOpen((v) => !v)}>
            {open ? "접기" : "상세"}
          </button>
        </td>
      </tr>
      <tr className="row-detail" id={id} hidden={!open}>
        <td className="core" colSpan={span}>
          <dl className="row-detail-list">
            {details.map((d, i) => (
              <div key={i}>
                <dt>{d.label}</dt>
                <dd>{d.value}</dd>
              </div>
            ))}
          </dl>
        </td>
      </tr>
    </>
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
