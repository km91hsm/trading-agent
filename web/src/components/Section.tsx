// 절 제목 + 2px 괘선. aside 는 제목 줄 오른쪽 보조문(건수·기준).
import type { ReactNode } from "react";

export function Section({
  title,
  aside,
  children,
  className,
}: {
  title: string;
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={className ? `sec ${className}` : "sec"}>
      <h2 className="sec-h">
        <span>{title}</span>
        {aside ? <span className="sec-aside">{aside}</span> : null}
      </h2>
      {children}
    </section>
  );
}
