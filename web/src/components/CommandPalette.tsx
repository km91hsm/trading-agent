// ⌘K / Ctrl+K 이동 팔레트(Phase 23 G). 화면·종목 코드·종목명 → 이동만. 쓰기 명령은 없다.
// 'g' 뒤 한 키(d t o a c p h s)로도 이동한다(입력창에 포커스가 있으면 무시).
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useApp, useNameSearch } from "../hooks";
import { chordTarget, paletteItems } from "../nav";

function isTyping(e: KeyboardEvent): boolean {
  const t = e.target as HTMLElement | null;
  return !!t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
}

export function CommandPalette() {
  const { names } = useApp();
  const nav = useNavigate();
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [idx, setIdx] = useState(0);
  const ref = useRef<HTMLDialogElement>(null);
  const chord = useRef<number | null>(null);
  const hits = useNameSearch(open ? q : "");
  const items = paletteItems(q, names, hits);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
        return;
      }
      if (open || isTyping(e) || e.metaKey || e.ctrlKey || e.altKey) return;
      if (chord.current !== null) {
        window.clearTimeout(chord.current);
        chord.current = null;
        const to = chordTarget(e.key);
        if (to) {
          e.preventDefault();
          nav(to);
        }
        return;
      }
      if (e.key === "g") {
        chord.current = window.setTimeout(() => {
          chord.current = null;
        }, 1500);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, nav]);

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) {
      setQ("");
      setIdx(0);
      d.showModal();
    } else if (!open && d.open) d.close();
  }, [open]);

  const go = (to: string) => {
    setOpen(false);
    nav(to);
  };
  return (
    <dialog ref={ref} className="dialog palette" onClose={() => setOpen(false)} aria-label="이동 팔레트">
      <input
        autoFocus
        value={q}
        placeholder="화면 이름, 종목 코드(005930·AAPL), 종목명…"
        onChange={(e) => {
          setQ(e.target.value);
          setIdx(0);
        }}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setIdx((i) => Math.min(i + 1, items.length - 1));
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setIdx((i) => Math.max(i - 1, 0));
          } else if (e.key === "Enter" && items[idx]) go(items[idx].to);
          else if (e.key === "Escape") setOpen(false);
        }}
      />
      <ul className="palette-list" role="listbox">
        {items.map((it, i) => (
          <li key={it.to + it.label} role="option" aria-selected={i === idx} className={i === idx ? "sel" : ""} onMouseEnter={() => setIdx(i)} onClick={() => go(it.to)} onKeyDown={() => {}}>
            <span>{it.label}</span>
            <small className="muted">{it.hint}</small>
          </li>
        ))}
        {!items.length && <li className="muted">일치하는 화면·종목 없음</li>}
      </ul>
      <p className="muted palette-help">↑↓ 이동 · Enter 열기 · Esc 닫기 · 어디서나 g 뒤 d/t/o/a/c/p/h/s</p>
    </dialog>
  );
}
