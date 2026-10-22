// 체크포인트·입력 JSON 을 접을 수 있는 트리로 보여준다.
export function JsonView({ value, name, depth = 0 }: { value: unknown; name?: string; depth?: number }) {
  const label = name !== undefined ? <span className="k">{name}: </span> : null;
  if (value === null || typeof value !== "object") {
    const kind = value === null ? "object" : typeof value;
    return (
      <div className="leaf">
        {label}
        <span className={`v ${kind}`}>{typeof value === "string" ? value : JSON.stringify(value)}</span>
      </div>
    );
  }
  const entries = Array.isArray(value)
    ? value.map((v, i) => [String(i), v] as const)
    : Object.entries(value as Record<string, unknown>);
  return (
    <details open={depth < 2}>
      <summary>
        {label}
        <span className="muted">{Array.isArray(value) ? `[${entries.length}]` : `{${entries.length}}`}</span>
      </summary>
      <div className="indent">
        {entries.map(([k, v]) => (
          <JsonView key={k} name={k} value={v} depth={depth + 1} />
        ))}
      </div>
    </details>
  );
}
