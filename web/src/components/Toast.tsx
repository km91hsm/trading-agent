// 제어 결과 알림. 4초 뒤 사라지고 aria-live 로 읽힌다.
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

type ToastKind = "ok" | "error" | "info";
type Toast = { id: number; msg: string; kind: ToastKind };
type Push = (msg: string, kind?: ToastKind) => void;

const ToastContext = createContext<Push | null>(null);

export function useToast(): Push {
  const push = useContext(ToastContext);
  if (!push) throw new Error("ToastProvider 가 없습니다");
  return push;
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback<Push>((msg, kind = "info") => {
    const id = Date.now() + Math.random();
    setItems((xs) => [...xs, { id, msg, kind }]);
    setTimeout(() => setItems((xs) => xs.filter((t) => t.id !== id)), 4000);
  }, []);
  const value = useMemo(() => push, [push]);
  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toasts" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast toast-${t.kind}`}>
            {t.msg}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
