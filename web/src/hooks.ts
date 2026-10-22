import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { getJson, subscribeEvents, tokenStore, type EventSnapshot } from "./api";
import { groupByMarket, missingKeys, type NameMap } from "./names";
import { applyTheme, themeStore, watchSystemTheme, type Theme } from "./theme";
import type { Account } from "./types";

export type AppState = {
  accounts: Account[];
  accountId: string;
  setAccountId: (id: string) => void;
  token: string | null;
  setToken: (t: string) => void;
  events: EventSnapshot | null;
  tick: number; // 이벤트가 올 때마다 증가 → 페이지가 다시 읽는다
  reloadAccounts: () => void;
  theme: Theme;
  setTheme: (t: Theme) => void;
  names: NameMap; // "KR:005930" → "삼성전자" (서버 /api/names, 원장·캐시만)
  ensureNames: (market: string, tickers: string[]) => void; // 모르는 종목을 모아 300ms 뒤 1회 요청
};

export const AppContext = createContext<AppState | null>(null);

export function useApp(): AppState {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("AppContext 가 없습니다");
  return ctx;
}

const ACCOUNT_KEY = "trader.accountId";

export function useAppState(): AppState {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountId, setAccountIdRaw] = useState<string>(() => {
    try {
      return localStorage.getItem(ACCOUNT_KEY) || "";
    } catch {
      return "";
    }
  });
  const [token, setTokenRaw] = useState<string | null>(() => tokenStore.get());
  const [events, setEvents] = useState<EventSnapshot | null>(null);
  const [tick, setTick] = useState(0);
  const [theme, setThemeRaw] = useState<Theme>(() => themeStore.get());
  const [names, setNames] = useState<NameMap>({});
  const namesRef = useRef<NameMap>({});
  const requested = useRef<Set<string>>(new Set());
  const pending = useRef<Set<string>>(new Set());
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mergeNames = useCallback((got: NameMap) => {
    namesRef.current = { ...namesRef.current, ...got };
    setNames(namesRef.current);
  }, []);
  const flushNames = useCallback(() => {
    timer.current = null;
    const groups = groupByMarket(pending.current);
    pending.current = new Set();
    for (const [market, tickers] of Object.entries(groups)) {
      getJson<NameMap>("/api/names", { market, tickers: tickers.join(",") })
        .then(mergeNames)
        .catch(() => {
          /* 이름은 보조 정보 — 실패해도 코드만 보인다 */
        });
    }
  }, [mergeNames]);
  const ensureNames = useCallback(
    (market: string, tickers: string[]) => {
      const keys = missingKeys(namesRef.current, requested.current, market, tickers);
      if (!keys.length) return;
      for (const k of keys) {
        requested.current.add(k);
        pending.current.add(k);
      }
      if (timer.current === null) timer.current = setTimeout(flushNames, 300);
    },
    [flushNames],
  );
  useEffect(() => {
    getJson<NameMap>("/api/names")
      .then(mergeNames)
      .catch(() => {
        /* 무시 */
      });
  }, [mergeNames]);

  const reloadAccounts = useCallback(() => {
    getJson<Account[]>("/api/accounts")
      .then((rows) => {
        setAccounts(rows);
        setAccountIdRaw((cur) => (cur && rows.some((a) => a.id === cur) ? cur : rows[0]?.id ?? ""));
      })
      .catch(() => setAccounts([]));
  }, []);

  useEffect(() => {
    reloadAccounts();
  }, [reloadAccounts]);

  useEffect(() => watchSystemTheme(document, () => themeStore.get()), []);

  useEffect(() => {
    return subscribeEvents((snap) => {
      setEvents(snap);
      setTick((t) => t + 1);
      reloadAccounts();
    });
  }, [reloadAccounts]);

  const setAccountId = (id: string) => {
    setAccountIdRaw(id);
    try {
      localStorage.setItem(ACCOUNT_KEY, id);
    } catch {
      /* 무시 */
    }
  };
  const setToken = (t: string) => {
    tokenStore.set(t);
    setTokenRaw(t || null);
  };
  const setTheme = (t: Theme) => {
    themeStore.set(t);
    setThemeRaw(t);
    applyTheme(document, t);
  };
  return { accounts, accountId, setAccountId, token, setToken, events, tick, reloadAccounts, theme, setTheme, names, ensureNames };
}

export type Loaded<T> = { data: T | null; error: string | null; loading: boolean; reload: () => void };

/** fn 이 null 을 돌려주면 호출하지 않는다 (예: 계좌 미선택). deps 가 바뀌면 다시 읽는다. */
export function useLoad<T>(fn: () => Promise<T> | null, deps: unknown[]): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [n, setN] = useState(0);
  useEffect(() => {
    const p = fn();
    if (!p) {
      // 호출 조건이 빠지면(계좌 미선택 등) 이전 결과·오류를 모두 지운다
      setData(null);
      setError(null);
      setLoading(false);
      return;
    }
    let alive = true;
    setLoading(true);
    p.then((d) => {
      if (alive) {
        setData(d);
        setError(null);
      }
    })
      .catch((e: Error) => alive && setError(e.message))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, n]);
  return { data, error, loading, reload: () => setN((x) => x + 1) };
}
