// 의미 배지. env(모의/실전)·mode(자동/승인/끔)·status(주문·승인 상태)·action(결정)·pending(대기/주의 문구).
import { termLabel } from "../glossary";

type Kind = "env" | "mode" | "status" | "action" | "pending" | "danger" | "ok" | "neutral";

const ENV_LABEL: Record<string, string> = { paper: "모의", real: "실전" };

export function Badge({ kind, value, title }: { kind: Kind; value: string; title?: string }) {
  let label = value;
  const data: Record<string, string> = {};
  if (kind === "env") {
    label = ENV_LABEL[value] ?? value;
    data["data-env"] = value;
  } else if (kind === "mode") {
    label = termLabel(`mode.${value}`);
    data["data-mode"] = value;
  } else if (kind === "status") {
    label = termLabel(`status.${value}`);
    data["data-status"] = value;
  } else if (kind === "action") {
    label = termLabel(`action.${value}`);
    data["data-action"] = value;
  }
  return (
    <span className={`badge badge-${kind}`} title={title} {...data}>
      {label}
    </span>
  );
}
