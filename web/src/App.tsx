import { Suspense, lazy } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Shell } from "./components/Shell";
import { Skeleton } from "./components/Skeleton";
import { ToastProvider } from "./components/Toast";
import { AppContext, useAppState } from "./hooks";
import { AccountPage } from "./pages/Account";
import { ComparePage } from "./pages/Compare";
import { FoPage } from "./pages/Fo";
import { Dashboard } from "./pages/Dashboard";
import { DataPage } from "./pages/Data";
import { HowItWorksPage } from "./pages/HowItWorks";
import { MlPage } from "./pages/Ml";
import { OrdersPage } from "./pages/Orders";
import { SettingsPage } from "./pages/Settings";
import { SystemPage } from "./pages/System";
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
      <ToastProvider>
        <BrowserRouter>
          <Shell>
            <Suspense fallback={<Skeleton rows={6} />}>
              <Routes>
                <Route path="/" element={<Dashboard />} />
                <Route path="/account" element={<AccountPage />} />
                <Route path="/compare" element={<ComparePage />} />
                <Route path="/fo" element={<FoPage />} />
                <Route path="/today" element={<TodayPage />} />
                <Route path="/orders" element={<OrdersPage />} />
                <Route path="/chart" element={<ChartPage />} />
                <Route path="/data" element={<DataPage />} />
                <Route path="/ml" element={<MlPage />} />
                <Route path="/performance" element={<PerformancePage />} />
                <Route path="/settings" element={<SettingsPage />} />
                <Route path="/system" element={<SystemPage />} />
                <Route path="/how" element={<HowItWorksPage />} />
                <Route path="*" element={<Dashboard />} />
              </Routes>
            </Suspense>
          </Shell>
        </BrowserRouter>
      </ToastProvider>
    </AppContext.Provider>
  );
}
