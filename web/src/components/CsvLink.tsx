// 표 내려받기(Phase 23 G): 같은 API 에 ?format=csv 를 붙인 링크. 서버가 CSV 를 만든다.
export function CsvLink({ href, label = "CSV" }: { href: string; label?: string }) {
  const url = href.includes("?") ? `${href}&format=csv` : `${href}?format=csv`;
  return (
    <a className="btn btn-sm btn-ghost" href={url} download title="이 표를 CSV 로 내려받기">
      {label}
    </a>
  );
}
