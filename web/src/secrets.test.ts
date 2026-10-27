import { describe, expect, it } from "vitest";
import { groupSecrets, secretsPayload, usesText, type SecretKey } from "./secrets";

const k = (name: string, group: string, set = false): SecretKey => ({ name, group, label: name, account: null, set });

describe("연결 키", () => {
  it("서버 순서대로 묶는다", () => {
    const g = groupSecrets([k("A", "공용"), k("B", "공용"), k("C", "알림"), k("D", "계좌 전용 · real-2")]);
    expect(g.map((x) => [x.group, x.keys.length])).toEqual([
      ["공용", 2],
      ["알림", 1],
      ["계좌 전용 · real-2", 1],
    ]);
  });

  it("빈 입력은 보내지 않고, 입력이 있으면 지우기보다 우선", () => {
    const p = secretsPayload({ A: "  v ", B: "", C: "x" }, new Set(["B", "C", "D"]));
    expect(p).toEqual({ values: { A: "v", C: "x" }, clear: ["B", "D"] });
  });

  it("계좌가 쓰는 키 설명", () => {
    expect(usesText({ id: "real-2", env: "real", uses: "전용", prefix: "KIS_REAL_2_" })).toContain("KIS_REAL_2_");
    expect(usesText({ id: "r", env: "real", uses: "없음", prefix: "KIS_REAL_" })).toContain("앱키 없음");
  });
});
