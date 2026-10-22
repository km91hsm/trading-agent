import { describe, expect, it } from "vitest";
import { groupByMarket, labelOf, missingKeys, nameKey } from "./names";

describe("names", () => {
  it("키는 시장:코드", () => {
    expect(nameKey("KR", "005930")).toBe("KR:005930");
  });
  it("missingKeys 는 아는 것·요청한 것·중복을 뺀다", () => {
    const known = { "KR:005930": "삼성전자" };
    const req = new Set(["KR:000660"]);
    expect(missingKeys(known, req, "KR", ["005930", "000660", "035420", "035420", ""])).toEqual(["KR:035420"]);
  });
  it("groupByMarket 은 시장별 코드 목록", () => {
    expect(groupByMarket(["KR:005930", "US:AAPL", "KR:000660", "bad"])).toEqual({ KR: ["005930", "000660"], US: ["AAPL"] });
  });
  it("labelOf 는 '이름 코드', 모르면 코드만", () => {
    expect(labelOf({ "KR:005930": "삼성전자" }, "KR", "005930")).toBe("삼성전자 005930");
    expect(labelOf({}, "KR", "005930")).toBe("005930");
    expect(labelOf({ "US:AAPL": "AAPL" }, "US", "AAPL")).toBe("AAPL");
  });
});
