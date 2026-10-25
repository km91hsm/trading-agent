// 운영 점검 특보판 (Phase 23 A). 정상이면 괘선 한 줄, 아니면 항목 표 + '조치 필요' 링크 행.
import { Link } from "react-router-dom";
import { areaLine, opsRows, type OpsResp } from "../ops";
import { Badge } from "./Badge";
import { Section } from "./Section";
import { Table, Td, Th } from "./Table";

export function Advisory({ ops }: { ops: OpsResp | null }) {
  if (!ops) return null;
  const rows = opsRows(ops);
  if (ops.level === "ok") {
    return (
      <p className="rule-line muted" title={areaLine(ops)}>
        {ops.headline}
        {ops.source === "live" ? " · 지금 판정" : ""}
      </p>
    );
  }
  return (
    <Section title={`점검 ${ops.label}`} aside={ops.headline} className={ops.level === "critical" ? "danger" : ""}>
      <p className="muted">{areaLine(ops)}</p>
      <Table>
        <thead>
          <tr>
            <Th>영역</Th>
            <Th>단계</Th>
            <Th>내용</Th>
            <Th>조치</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className={r.danger ? "danger" : ""}>
              <Td>{r.area}</Td>
              <Td>
                <Badge kind={r.danger ? "pending" : "neutral"} value={r.levelLabel} />
              </Td>
              <Td>{r.text}</Td>
              <Td>{r.link ? <Link to={r.link}>보기</Link> : <span className="muted">—</span>}</Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Section>
  );
}
