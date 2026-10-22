// 용어: 라벨 + 설명(title, 포커스 가능). 비개발자가 읽을 수 있게.
import { termHelp, termLabel } from "../glossary";

export function Term({ k, children }: { k: string; children?: React.ReactNode }) {
  const help = termHelp(k);
  return (
    <abbr className="term" title={help || undefined} tabIndex={help ? 0 : undefined}>
      {children ?? termLabel(k)}
    </abbr>
  );
}
