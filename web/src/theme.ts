// 테마(라이트/다크/시스템) 저장과 적용. 저장 키는 trader.theme, 적용은 <html data-theme>.

export type Theme = "light" | "dark" | "system";
export type Resolved = "light" | "dark";

const KEY = "trader.theme";
const THEMES: Theme[] = ["light", "dark", "system"];

export const themeStore = {
  get(): Theme {
    try {
      const v = localStorage.getItem(KEY);
      return THEMES.includes(v as Theme) ? (v as Theme) : "system";
    } catch {
      return "system";
    }
  },
  set(t: Theme): void {
    try {
      localStorage.setItem(KEY, t);
    } catch {
      /* 저장 불가 환경이면 메모리 없이 진행 */
    }
  },
};

export function resolveTheme(t: Theme, prefersDark: boolean): Resolved {
  if (t === "system") return prefersDark ? "dark" : "light";
  return t;
}

export function prefersDark(): boolean {
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  } catch {
    return false;
  }
}

export function applyTheme(doc: Document, t: Theme): Resolved {
  const resolved = resolveTheme(t, prefersDark());
  doc.documentElement.dataset.theme = resolved;
  return resolved;
}

/** 시스템 선호가 바뀔 때 `system` 테마를 다시 적용한다. 해제 함수를 돌려준다. */
export function watchSystemTheme(doc: Document, current: () => Theme): () => void {
  let mq: MediaQueryList | null = null;
  try {
    mq = window.matchMedia("(prefers-color-scheme: dark)");
  } catch {
    return () => {};
  }
  const onChange = () => {
    if (current() === "system") applyTheme(doc, "system");
  };
  mq.addEventListener("change", onChange);
  return () => mq?.removeEventListener("change", onChange);
}
