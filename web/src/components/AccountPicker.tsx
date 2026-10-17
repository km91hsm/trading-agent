import { useApp } from "../hooks";

export function AccountPicker() {
  const { accounts, accountId, setAccountId } = useApp();
  return (
    <label className="picker">
      계좌
      <select value={accountId} onChange={(e) => setAccountId(e.target.value)}>
        {accounts.map((a) => (
          <option key={a.id} value={a.id}>
            {a.id} ({a.env === "real" ? "실전" : "모의"} · {a.mode})
          </option>
        ))}
      </select>
    </label>
  );
}
