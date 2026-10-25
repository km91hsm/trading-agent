import { describe, expect, it } from "vitest";
import type { EventSnapshot } from "./api";
import { canLead, createEventsClient, HEARTBEAT_MS, LEADER_KEY, parseLeader, STALE_MS, type EventsDeps } from "./events";

const snap = (n: number): EventSnapshot =>
  ({ orders: n, approvals_pending: 0, approvals: 0, runs: 0, decisions: 0, triggers: 0, watchlist: "", paused: false }) as EventSnapshot;

class FakeBus {
  channels: FakeChannel[] = [];
  make(): FakeChannel {
    const c = new FakeChannel(this);
    this.channels.push(c);
    return c;
  }
}
class FakeChannel {
  listeners: ((e: { data: unknown }) => void)[] = [];
  closed = false;
  bus: FakeBus;
  constructor(bus: FakeBus) {
    this.bus = bus;
  }
  postMessage(msg: unknown) {
    for (const c of this.bus.channels) if (c !== this && !c.closed) for (const l of c.listeners) l({ data: msg });
  }
  addEventListener(_t: "message", fn: (e: { data: unknown }) => void) {
    this.listeners.push(fn);
  }
  close() {
    this.closed = true;
  }
}

function harness() {
  const store = new Map<string, string>();
  const storage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  };
  let now = 1_000_000;
  const bus = new FakeBus();
  const opened: { onSnap: (s: EventSnapshot) => void; closed: boolean }[] = [];
  const timers: { fn: () => void }[] = [];
  const mk = (id: string, onChange: (s: EventSnapshot) => void) => {
    const deps: EventsDeps = {
      openSource: (onSnap) => {
        const rec = { onSnap, closed: false };
        opened.push(rec);
        return { close: () => void (rec.closed = true) };
      },
      channel: bus.make() as never,
      storage,
      now: () => now,
      selfId: id,
      setInterval: (fn) => {
        timers.push({ fn });
        return timers.length - 1;
      },
      clearInterval: () => {},
    };
    return createEventsClient(onChange, deps);
  };
  return { mk, opened, timers, advance: (ms: number) => (now += ms), store, bus };
}

describe("SSE 리더 탭", () => {
  it("리더 판정: 기록 없음·자기 것·오래된 하트비트만 리더 가능", () => {
    expect(canLead(null, "a", 0)).toBe(true);
    expect(canLead({ id: "a", ts: 0 }, "a", 100)).toBe(true);
    expect(canLead({ id: "b", ts: 0 }, "a", STALE_MS)).toBe(false);
    expect(canLead({ id: "b", ts: 0 }, "a", STALE_MS + 1)).toBe(true);
    expect(parseLeader("{bad")).toBeNull();
    expect(parseLeader(JSON.stringify({ id: "x", ts: 5 }))).toEqual({ id: "x", ts: 5 });
  });

  it("탭 둘이면 EventSource 는 하나만 열리고 스냅샷은 중계된다", () => {
    const h = harness();
    const got: Record<string, EventSnapshot[]> = { a: [], b: [] };
    h.mk("a", (s) => got.a.push(s));
    h.mk("b", (s) => got.b.push(s));
    expect(h.opened.length).toBe(1);
    h.opened[0].onSnap(snap(1));
    expect(got.a.map((s) => s.orders)).toEqual([1]);
    expect(got.b.map((s) => s.orders)).toEqual([1]);
    expect(parseLeader(h.store.get(LEADER_KEY) ?? null)?.id).toBe("a");
  });

  it("리더가 떠나면 다른 탭이 승계해 새 EventSource 를 연다", () => {
    const h = harness();
    const stopA = h.mk("a", () => {});
    h.mk("b", () => {});
    expect(h.opened.length).toBe(1);
    stopA();
    expect(h.opened[0].closed).toBe(true);
    expect(h.opened.length).toBe(2);
    expect(parseLeader(h.store.get(LEADER_KEY) ?? null)?.id).toBe("b");
  });

  it("리더 하트비트가 멈추면(탭 강제 종료) 다음 틱에 승계한다", () => {
    const h = harness();
    h.mk("a", () => {});
    h.mk("b", () => {});
    h.advance(STALE_MS + HEARTBEAT_MS);
    h.timers[1].fn(); // b 의 틱
    expect(h.opened.length).toBe(2);
    expect(parseLeader(h.store.get(LEADER_KEY) ?? null)?.id).toBe("b");
  });

  it("BroadcastChannel 이 없으면 탭마다 직접 연다", () => {
    let opened = 0;
    const stop = createEventsClient(() => {}, {
      openSource: () => {
        opened += 1;
        return { close: () => {} };
      },
      channel: null,
      storage: null,
      now: () => 0,
      selfId: "x",
      setInterval: () => 0,
      clearInterval: () => {},
    });
    expect(opened).toBe(1);
    stop();
  });
});
