// FastAPI 호출 헬퍼. 조회는 토큰 없음, 제어(postJson)는 Bearer 토큰. 계좌 단위 조회는 withAccount 로만.
import { browserDeps, createEventsClient } from "./events";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export class AuthError extends ApiError {
  constructor(message = "토큰이 필요합니다") {
    super(401, message);
  }
}

const TOKEN_KEY = "trader.webToken";

export const tokenStore = {
  get(): string | null {
    try {
      return localStorage.getItem(TOKEN_KEY) || null;
    } catch {
      return null;
    }
  },
  set(token: string): void {
    try {
      if (token) localStorage.setItem(TOKEN_KEY, token);
      else localStorage.removeItem(TOKEN_KEY);
    } catch {
      /* 저장 불가 환경이면 메모리 없이 진행 */
    }
  },
};

export function withAccount(path: string, accountId: string): string {
  if (!accountId) throw new Error("계좌를 선택하세요 (account_id 필수)");
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}account_id=${encodeURIComponent(accountId)}`;
}

async function parseError(res: Response): Promise<never> {
  let detail = `${res.status} ${res.statusText}`;
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") detail = body.detail;
    else if (body.detail) detail = JSON.stringify(body.detail);
  } catch {
    /* 본문 없음 */
  }
  if (res.status === 401) throw new AuthError(detail);
  throw new ApiError(res.status, detail);
}

export async function getJson<T>(path: string, params?: Record<string, string>): Promise<T> {
  let url = path;
  if (params && Object.keys(params).length) {
    const q = new URLSearchParams(params).toString();
    url += (path.includes("?") ? "&" : "?") + q;
  }
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  if (!res.ok) await parseError(res);
  return (await res.json()) as T;
}

export async function postJson<T>(path: string, body: unknown, token: string | null): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== null && body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(path, {
    method: "POST",
    headers,
    body: body === null || body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) await parseError(res);
  return (await res.json()) as T;
}

export type EventSnapshot = {
  orders: number;
  approvals_pending: number;
  approvals: number;
  runs: number;
  decisions: number;
  triggers: number;
  watchlist: string;
  paused: boolean;
  job_requests?: number;
  jobs_active?: number;
  jobs_done?: number;
};

/** 탭 하나(리더)만 EventSource 를 열고 나머지 탭에는 BroadcastChannel 로 중계한다 (events.ts).
 * 탭마다 열면 브라우저 호스트당 연결 한도(약 6개)를 다 써 모든 fetch 가 멈춘다. */
export function subscribeEvents(onChange: (snap: EventSnapshot) => void, onStatus?: (connected: boolean) => void): () => void {
  if (typeof EventSource === "undefined") return () => {};
  const openSource = (onSnap: (snap: EventSnapshot) => void, status?: (connected: boolean) => void) => {
    const es = new EventSource("/api/events");
    es.addEventListener("change", (e) => {
      try {
        onSnap(JSON.parse((e as MessageEvent).data) as EventSnapshot);
      } catch {
        /* 잘못된 이벤트는 무시 */
      }
    });
    // 브라우저가 자동 재연결하는 동안(onerror) 끊김으로 표시하고, 다시 열리면(onopen) 지운다
    es.onopen = () => status?.(true);
    es.onerror = () => status?.(false);
    return { close: () => es.close() };
  };
  return createEventsClient(onChange, browserDeps(openSource), onStatus);
}
