// 용어: 설명이 있는 용어는 눌러서(또는 Enter·Space) 한 줄 설명을 여는 버튼이다. 예전 `<abbr title>` 은
// '?' 커서만 보이고 클릭·터치에 아무 반응이 없었다. 바깥 클릭·Esc·스크롤로 닫히고 한 번에 하나만 열린다.
// 설명이 없는 키는 밑줄 없는 일반 글자.
import { useEffect, useId, useRef, useState, type CSSProperties, type MouseEvent, type ReactNode } from "react";
import { termHelp, termLabel } from "../glossary";
import { popoverPosition } from "../termPopover";

const OPEN_EVENT = "trader:term-open";

export function Term({ k, children }: { k: string; children?: ReactNode }) {
  const help = termHelp(k);
  const id = useId();
  const [pos, setPos] = useState<CSSProperties | null>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const label = children ?? termLabel(k);

  useEffect(() => {
    if (!pos) return;
    const close = () => setPos(null);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setPos(null);
        btn.current?.focus();
      }
    };
    const onDown = (e: Event) => {
      if (!btn.current?.parentElement?.contains(e.target as Node)) close();
    };
    const onOther = (e: Event) => (e as CustomEvent<string>).detail !== id && close();
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onDown);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    window.addEventListener(OPEN_EVENT, onOther);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onDown);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
      window.removeEventListener(OPEN_EVENT, onOther);
    };
  }, [pos, id]);

  if (!help) return <span className="term-plain">{label}</span>;

  const toggle = (e: MouseEvent) => {
    e.stopPropagation(); // 표 행 선택 같은 바깥 클릭 동작과 겹치지 않게
    if (pos) return setPos(null);
    const r = btn.current?.getBoundingClientRect();
    if (!r) return;
    window.dispatchEvent(new CustomEvent(OPEN_EVENT, { detail: id }));
    setPos(popoverPosition(r, { width: window.innerWidth, height: window.innerHeight }));
  };

  return (
    <span className="term-wrap">
      <button
        ref={btn}
        type="button"
        className="term"
        aria-expanded={pos !== null}
        aria-controls={pos ? id : undefined}
        aria-describedby={pos ? id : undefined}
        onClick={toggle}
      >
        {label}
      </button>
      {pos && (
        <span id={id} role="tooltip" className="term-pop" style={pos}>
          <b>{termLabel(k)}</b> {help}
        </span>
      )}
    </span>
  );
}
