// 아이콘 집합. 16px 격자, stroke 1.5, currentColor. 이모지·유니코드 기호 대신 전부 여기서 그린다.
// ▲▼●○☆ 는 신문 시세표의 고유 표기라 같은 굵기의 SVG 로 둔다.

export type GlyphName =
  | "up"
  | "down"
  | "flat"
  | "held"
  | "pick"
  | "watch"
  | "menu"
  | "close"
  | "check"
  | "x"
  | "sun"
  | "moon"
  | "system"
  | "refresh"
  | "alert"
  | "info"
  | "chevron"
  | "chevron-up"
  | "sort"
  | "mark";

const PATHS: Record<GlyphName, React.ReactNode> = {
  up: <path d="M8 3.5 13 12H3z" fill="currentColor" stroke="none" />,
  down: <path d="M8 12.5 3 4h10z" fill="currentColor" stroke="none" />,
  flat: <path d="M3 8h10" />,
  held: <circle cx="8" cy="8" r="4.5" fill="currentColor" stroke="none" />,
  pick: <circle cx="8" cy="8" r="4.25" />,
  watch: <path d="m8 2.5 1.7 3.6 3.9.5-2.8 2.7.7 3.9L8 11.3l-3.5 1.9.7-3.9L2.4 6.6l3.9-.5z" />,
  menu: <path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11" />,
  close: <path d="m4 4 8 8M12 4l-8 8" />,
  check: <path d="m3 8.5 3.2 3L13 4.5" />,
  x: <path d="m4 4 8 8M12 4l-8 8" />,
  sun: (
    <>
      <circle cx="8" cy="8" r="3" />
      <path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4" />
    </>
  ),
  moon: <path d="M13 9.5A5.5 5.5 0 0 1 6.5 3a5.5 5.5 0 1 0 6.5 6.5z" />,
  system: (
    <>
      <rect x="2" y="3" width="12" height="8.5" rx="1" />
      <path d="M5.5 14h5" />
    </>
  ),
  refresh: <path d="M13 8a5 5 0 1 1-1.5-3.6M13 2.5v3h-3" />,
  alert: (
    <>
      <path d="M8 2.5 14 13H2z" />
      <path d="M8 6.5v3M8 11.2v.3" />
    </>
  ),
  info: (
    <>
      <circle cx="8" cy="8" r="5.5" />
      <path d="M8 7.2v3.8M8 5v.3" />
    </>
  ),
  chevron: <path d="m5 6.5 3 3 3-3" />,
  "chevron-up": <path d="m5 9.5 3-3 3 3" />,
  sort: <path d="M5.5 3v10M5.5 13l-2-2M5.5 13l2-2M10.5 13V3M10.5 3l-2 2M10.5 3l2 2" />,
  // 브랜드 마크: 시세표의 ▲ 블록 — 미색 지면 위 먹 삼각형
  mark: (
    <>
      <rect x="1" y="1" width="14" height="14" rx="2" fill="currentColor" stroke="none" />
      <path d="M8 4.2 12.2 11H3.8z" fill="var(--paper)" stroke="none" />
    </>
  ),
};

export function Glyph({
  name,
  size = 16,
  title,
  className,
}: {
  name: GlyphName;
  size?: number;
  title?: string;
  className?: string;
}) {
  return (
    <svg
      data-glyph={name}
      className={className ? `glyph ${className}` : "glyph"}
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      role={title ? "img" : undefined}
      aria-hidden={title ? undefined : "true"}
    >
      {title ? <title>{title}</title> : null}
      {PATHS[name]}
    </svg>
  );
}
