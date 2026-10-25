// SSE(/api/events) 는 탭마다 열면 브라우저의 호스트당 연결 한도(HTTP/1.1 약 6개)를 먹어
// 탭이 여럿이면 모든 fetch 가 영원히 pending 된다 → **리더 탭 하나만** EventSource 를 열고
// BroadcastChannel 로 다른 탭에 스냅샷을 중계한다. 리더는 localStorage 에 하트비트를 남기고,
// 하트비트가 끊기면 다른 탭이 승계한다. 순수 함수(리더 판정)는 vitest 로 고정한다.

import type { EventSnapshot } from "./api";

export const LEADER_KEY = "trader.events.leader";
export const CHANNEL_NAME = "trader-events";
export const HEARTBEAT_MS = 2000;
export const STALE_MS = 6000;

export type LeaderRecord = { id: string; ts: number };

export function parseLeader(raw: string | null): LeaderRecord | null {
  if (!raw) return null;
  try {
    const v = JSON.parse(raw) as Partial<LeaderRecord>;
    if (typeof v.id === "string" && typeof v.ts === "number") return { id: v.id, ts: v.ts };
  } catch {
    /* 깨진 값은 없는 것으로 */
  }
  return null;
}

/** 이 탭이 리더가 될 수 있는가: 기록이 없거나 자기 것이거나 하트비트가 STALE_MS 넘게 멈췄을 때. */
export function canLead(record: LeaderRecord | null, selfId: string, now: number, staleMs = STALE_MS): boolean {
  if (!record) return true;
  if (record.id === selfId) return true;
  return now - record.ts > staleMs;
}

type Message = { type: "change"; snap: EventSnapshot } | { type: "hello" } | { type: "leader-gone"; id: string };

type ChannelLike = {
  postMessage(msg: Message): void;
  addEventListener(type: "message", fn: (e: { data: Message }) => void): void;
  close(): void;
};

type SourceLike = { close(): void };

export type EventsDeps = {
  openSource: (onSnap: (snap: EventSnapshot) => void) => SourceLike;
  channel: ChannelLike | null; // BroadcastChannel 이 없으면 null → 탭마다 직접 연다
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem"> | null;
  now: () => number;
  selfId: string;
  setInterval: (fn: () => void, ms: number) => unknown;
  clearInterval: (id: unknown) => void;
  onLeaderChange?: (leader: boolean) => void;
};

/** 리더 선출 + 중계. 반환값은 구독 해제 함수. */
export function createEventsClient(onChange: (snap: EventSnapshot) => void, deps: EventsDeps): () => void {
  if (!deps.channel || !deps.storage) {
    const src = deps.openSource(onChange);
    return () => src.close();
  }
  const { channel, storage } = deps;
  let source: SourceLike | null = null;
  let leader = false;
  let last: EventSnapshot | null = null;

  const write = () => {
    try {
      storage.setItem(LEADER_KEY, JSON.stringify({ id: deps.selfId, ts: deps.now() } satisfies LeaderRecord));
    } catch {
      /* 저장 불가(사생활 모드) → 그래도 리더로 동작 */
    }
  };
  const read = (): LeaderRecord | null => {
    try {
      return parseLeader(storage.getItem(LEADER_KEY));
    } catch {
      return null;
    }
  };
  const becomeLeader = () => {
    if (leader) return;
    leader = true;
    write();
    source = deps.openSource((snap) => {
      last = snap;
      onChange(snap);
      channel.postMessage({ type: "change", snap });
    });
    deps.onLeaderChange?.(true);
  };
  const resign = () => {
    if (!leader) return;
    leader = false;
    source?.close();
    source = null;
    const rec = read();
    if (rec && rec.id === deps.selfId) {
      try {
        storage.removeItem(LEADER_KEY);
      } catch {
        /* 무시 */
      }
    }
    channel.postMessage({ type: "leader-gone", id: deps.selfId });
    deps.onLeaderChange?.(false);
  };

  channel.addEventListener("message", (e) => {
    const msg = e.data;
    if (!msg || typeof msg !== "object") return;
    if (msg.type === "change") {
      if (!leader) {
        last = msg.snap;
        onChange(msg.snap);
      }
    } else if (msg.type === "hello") {
      if (leader && last) channel.postMessage({ type: "change", snap: last });
    } else if (msg.type === "leader-gone") {
      if (!leader && canLead(read(), deps.selfId, deps.now())) becomeLeader();
    }
  });

  const tick = () => {
    if (leader) {
      write();
      return;
    }
    if (canLead(read(), deps.selfId, deps.now())) becomeLeader();
  };
  if (canLead(read(), deps.selfId, deps.now())) becomeLeader();
  else channel.postMessage({ type: "hello" });
  const timer = deps.setInterval(tick, HEARTBEAT_MS);

  return () => {
    deps.clearInterval(timer);
    resign();
    channel.close();
  };
}

/** 브라우저 환경용 의존성. BroadcastChannel/localStorage 가 없으면 null 로 두어 직접 연결한다. */
export function browserDeps(openSource: EventsDeps["openSource"]): EventsDeps {
  let channel: ChannelLike | null = null;
  try {
    if (typeof BroadcastChannel !== "undefined") channel = new BroadcastChannel(CHANNEL_NAME) as unknown as ChannelLike;
  } catch {
    channel = null;
  }
  let storage: EventsDeps["storage"] = null;
  try {
    storage = typeof localStorage !== "undefined" ? localStorage : null;
  } catch {
    storage = null;
  }
  const selfId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  return {
    openSource,
    channel,
    storage,
    now: () => Date.now(),
    selfId,
    setInterval: (fn, ms) => window.setInterval(fn, ms),
    clearInterval: (id) => window.clearInterval(id as number),
  };
}
