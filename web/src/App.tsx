import { Suspense, lazy } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Nav } from "./components/Nav";
import { AppContext, useAppState } from "./hooks";
import { AccountPage } from "./pages/Account";
import { ComparePage } from "./pages/Compare";
import { Dashboard } from "./pages/Dashboard";
import { DataPage } from "./pages/Data";
import { MlPage } from "./pages/Ml";
import { OrdersPage } from "./pages/Orders";
import { SettingsPage } from "./pages/Settings";
import { TodayPage } from "./pages/Today";

// lightweight-charts 를 쓰는 두 페이지만 별도 청크로 (첫 로드 번들에서 제외)
const ChartPage = lazy(() => import("./pages/Chart").then((m) => ({ default: m.ChartPage })));
const PerformancePage = lazy(() =>
  import("./pages/Performance").then((m) => ({ default: m.PerformancePage })),
);

export default function App() {
  const state = useAppState();
  return (
    <AppContext.Provider value={state}>
      <BrowserRouter>
        <Nav />
        <main>
          <Suspense fallback={<p className="muted">불러오는 중…</p>}>
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/account" element={<AccountPage />} />
              <Route path="/compare" element={<ComparePage />} />
              <Route path="/today" element={<TodayPage />} />
              <Route path="/orders" element={<OrdersPage />} />
              <Route path="/chart" element={<ChartPage />} />
              <Route path="/data" element={<DataPage />} />
              <Route path="/ml" element={<MlPage />} />
              <Route path="/performance" element={<PerformancePage />} />
              <Route path="/settings" element={<SettingsPage />} />
              <Route path="*" element={<Dashboard />} />
            </Routes>
          </Suspense>
        </main>
      </BrowserRouter>
    </AppContext.Provider>
  );
}
