import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { getJson, subscribeEvents, tokenStore, type EventSnapshot } from "./api";
import { changedKeys } from "./events";
import { groupByMarket, missingKeys, type NameMap } from "./names";
import { applyTheme, themeStore, watchSystemTheme, type Theme } from "./theme";
import { ALL_ACCOUNTS } from "./overview";
import type { Account } from "./types";

export type AppState = {
  accounts: Account[];
  /** 고른 계좌 id. '전체' 를 고르면 "" (계좌 단위 API 는 부르지 않는다). */
  accountId: string;
  /** 계좌 선택기 값: "all"(전체 합산) 또는 계좌 id (Phase 24). */
  selection: string;
  setAccountId: (id: string) => void;
  token: string | null;
  setToken: (t: string) => void;
  events: EventSnapshot | null;
  connected: boolean; // SSE 가 살아 있는가. 끊기면 셸 배지 + 60초 liveness 폴링이 tick 을 올린다
  tick: number; // 이벤트가 올 때마다 증가 → 페이지가 다시 읽는다
  ticks: Record<string, number>; // 스냅샷 키별 변경 횟수 → 바뀐 표만 다시 읽는다 (tickOf)
  tickOf: (...keys: string[]) => number;
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
export const LIVENESS_MS = 60_000;

export function useAppState(): AppState {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountId, setAccountIdRaw] = useState<string>(() => {
    try {
      return localStorage.getItem(ACCOUNT_KEY) || ALL_ACCOUNTS;
    } catch {
      return ALL_ACCOUNTS;
    }
  });
  const [token, setTokenRaw] = useState<string | null>(() => tokenStore.get());
  const [events, setEvents] = useState<EventSnapshot | null>(null);
  const [connected, setConnected] = useState(true);
  const [tick, setTick] = useState(0);
  const [ticks, setTicks] = useState<Record<string, number>>({});
  const prevSnap = useRef<EventSnapshot | null>(null);
  const tickOf = useCallback(
    (...keys: string[]) => keys.reduce((s, k) => s + (ticks[k] ?? 0), 0),
    [ticks],
  );
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
        // 기본은 '전체'. 사라진 계좌를 고른 채였으면 전체로 돌린다
        setAccountIdRaw((cur) => (cur === ALL_ACCOUNTS || rows.some((a) => a.id === cur) ? cur : ALL_ACCOUNTS));
      })
      .catch(() => setAccounts([]));
  }, []);

  useEffect(() => {
    reloadAccounts();
  }, [reloadAccounts]);

  useEffect(() => watchSystemTheme(document, () => themeStore.get()), []);

  useEffect(() => {
    return subscribeEvents(
      (snap) => {
        const changed = changedKeys(prevSnap.current, snap);
        prevSnap.current = snap;
        setEvents(snap);
        setConnected(true);
        setTick((t) => t + 1);
        if (changed.length)
          setTicks((cur) => {
            const next = { ...cur };
            for (const k of changed) next[k] = (next[k] ?? 0) + 1;
            return next;
          });
        if (changed.includes("paused") || changed.includes("runs")) reloadAccounts();
      },
      (ok) => setConnected(ok),
    );
  }, [reloadAccounts]);

  // SSE 가 끊긴 동안은 60초마다 /api/system/health 로 서버 생존을 확인하고, 살아 있으면 tick 을 올려
  // 페이지가 다시 읽게 한다(폴링으로 강등). 연결이 돌아오면 멈춘다.
  useEffect(() => {
    if (connected) return;
    const id = window.setInterval(() => {
      getJson<{ ok: boolean }>("/api/system/health")
        .then(() => setTick((t) => t + 1))
        .catch(() => {
          /* 서버가 없으면 끊김 표시 유지 */
        });
    }, LIVENESS_MS);
    return () => window.clearInterval(id);
  }, [connected]);

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
  return {
    ticks,
    tickOf,
    accounts,
    accountId: accountId === ALL_ACCOUNTS ? "" : accountId,
    selection: accountId,
    setAccountId, token, setToken, events, connected, tick, reloadAccounts, theme, setTheme, names, ensureNames };
}

export type Loaded<T> = { data: T | null; error: string | null; loading: boolean; reload: () => void };

/** fn 이 null 을 돌려주면 호출하지 않는다 (예: 계좌 미선택). deps 가 바뀌면 다시 읽는다. */
export function useLoad<T>(fn: () => Promise<T> | null, deps: unknown[]): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [n, setN] = useState(0);
  const stale = useRef(false);
  // 가려진 탭은 다시 읽지 않고(탭이 돌아오면 즉시), 전역 'trader:refresh' 이벤트로도 다시 읽는다 (Phase 23 G)
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible" && stale.current) {
        stale.current = false;
        setN((x) => x + 1);
      }
    };
    const onRefresh = () => setN((x) => x + 1);
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("trader:refresh", onRefresh);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("trader:refresh", onRefresh);
    };
  }, []);
  useEffect(() => {
    if (typeof document !== "undefined" && document.visibilityState === "hidden" && n > 0) {
      stale.current = true; // 돌아오면 한 번에 읽는다
      return;
    }
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
